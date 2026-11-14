"""리믹스 번역 릴스 후보 찾기 (pipeline/reels.py) — 가짜 Graph API 로만."""

import pytest

from pipeline import reels


def media(mid, caption="", likes=None, comments=0, kind="VIDEO", product="REELS"):
    m = {"id": mid, "caption": caption, "media_type": kind, "media_product_type": product,
         "permalink": f"https://www.instagram.com/reel/{mid}/", "comments_count": comments, "timestamp": "2026-11-01T10:00:00+0000"}
    if likes is not None:
        m["like_count"] = likes
    return m


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1789")
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    calls = []
    feeds = {"리쥬란": [], "리쥬란힐러": [], "리쥬란후기": []}
    recent = {}

    def fake(path, params):
        calls.append((path, params))
        if path == "ig_hashtag_search":
            return {"data": [{"id": f"tag-{params['q']}"}]}
        if path.endswith("/recent_media"):
            return {"data": recent.get(path.split("/")[0].removeprefix("tag-"), [])}
        if path.endswith("/top_media"):
            assert params["user_id"] == "1789" and "like_count" in params["fields"]
            return {"data": feeds[path.split("/")[0].removeprefix("tag-")]}
        if path == "1789":
            return {"username": "skinboundkorea", "id": "1789"}
        raise AssertionError(path)

    reels.set_http(fake)
    yield feeds, calls
    reels.set_http(None)


@pytest.mark.parametrize("caption,kind", [
    ("리쥬란 맞고 왔어요 #협찬", "sponsored"),
    ("체험단으로 다녀왔습니다", "sponsored"),
    ("시술을 제공받아 작성했어요", "sponsored"),
    ("#제품제공 이게 진짜 화장품이면", "sponsored"),
    ("경제적 이해관계가 있음", "sponsored"),
    ("thanks! #ad", "sponsored"),
    ("강남아름다운피부과에서 리쥬란", "clinic"),
    ("원장님이 직접 시술", "clinic"),
    ("예약은 프로필 링크", "clinic"),
    ("문의 02-1234-5678", "clinic"),
    ("@gangnam_derma_clinic 감사해요", "clinic"),
    ("리쥬란 전후 사진 비교", "before_after"),
])
def test_exclusion_reasons_finds_signals(caption, kind):
    assert kind in reels.exclusion_reasons(caption, reels.settings()["exclude"])


@pytest.mark.parametrize("caption", [
    "좋아요 누르시구 댓글 남겨주시면 바로 디엠가요",
    "궁금하면 DM 주세요",
    "문의는 DM으로 주세요",            # 뒤에 조사가 붙어도 (\b 로는 못 잡았다)
    "dm 주세요 #리쥬란",
    '병원정보가 궁금하신분들은 댓글로 "정보"라고 달아주세요',
    "(광고•협찬•수수료❌) 나만 정착할 수 없지 댓글 ㄱㄱ",
])
def test_dm_funnel_is_a_warning_not_an_exclusion(caption):
    """2026-11-13 운영자 결정: DM 유도는 후보에서 빼지 않고 ⚠️ 만 (광고인지 아닌지는 운영자도 알 수 없다)."""
    assert "dm_funnel" not in reels.exclusion_reasons(caption, reels.settings()["exclude"])
    assert "dm_funnel" in reels.exclusion_reasons(caption, reels.settings()["warn"])


@pytest.mark.parametrize("text", [
    "그 다음에 셀르디엠, 사실 셀리디엠은 성분이 높고",   # 제품명 속 '디엠' (2026-11-12 실제 영상 오탐)
    "admin 계정으로 로그인",
    "리쥬란 3회차 후기",
])
def test_dm_funnel_ignores_lookalikes(text):
    assert "dm_funnel" not in reels.exclusion_reasons(text, reels.settings()["warn"])


@pytest.mark.parametrize("caption", [
    "내돈내산 리쥬란 후기, 협찬 아님!",
    "광고X 솔직 후기 — 생각보다 아팠어요",
    "피부과에서 리쥬란 3회차, 붓기는 이틀",
    "Rejuran healer review, no ad",
])
def test_exclusion_reasons_keeps_plain_reviews(caption):
    assert reels.exclusion_reasons(caption, reels.settings()["exclude"]) == []


def test_denial_list_is_not_counted_as_sponsored():
    assert "sponsored" not in reels.exclusion_reasons("(광고•협찬•수수료❌) 솔직 후기", reels.settings()["exclude"])



def test_find_filters_ranks_by_likes_and_remembers(api):
    feeds, calls = api
    feeds["리쥬란"] = [
        media("a", "리쥬란 솔직 후기", likes=500),
        media("b", "리쥬란 #협찬", likes=9000),             # 협찬 → 제외
        media("c", "photo", likes=8000, kind="IMAGE", product="FEED"),  # 사진 → 제외
        media("d", "리쥬란 3회차", likes=None, comments=40),   # 좋아요 숨김 → 뒤로
    ]
    feeds["리쥬란힐러"] = [media("a", "리쥬란 솔직 후기", likes=500),  # 다른 해시태그에 같은 글 → 한 번만
                         media("e", "힐러 맞고 붓기 기록", likes=1200)]
    feeds["리쥬란후기"] = [media("f", "○○의원 원장님 최고", likes=3000)]  # 병원 → 제외
    last = reels.find("rejuran")
    assert [i["id"] for i in last["items"]] == ["e", "a", "d"]
    assert last["dropped"] == {"협찬·체험단": 1, "병원·의사": 1, "영상 아님": 1}
    text = reels.message(last)
    assert "❤ 1,200" in text and "❤ 숨김" in text and "https://www.instagram.com/reel/e/" in text
    assert "제외:" in text and "리믹스 허용" in text
    # 두 번째 실행: 이미 보낸 글은 다시 보내지 않고, 해시태그 ID 는 다시 찾지 않는다
    searches = sum(1 for p, _ in calls if p == "ig_hashtag_search")
    again = reels.find("rejuran")
    assert again["items"] == [] and again["dropped"]["이미 보냄"] == 3
    assert sum(1 for p, _ in calls if p == "ig_hashtag_search") == searches


def test_find_keeps_only_short_caption(api):
    feeds, _ = api
    feeds["리쥬란"] = [media("a", "가" * 500, likes=1)]
    item = reels.find("rejuran")["items"][0]
    assert len(item["caption"]) <= reels.settings()["caption_excerpt"] + 1
    assert "가" * 500 not in reels._state_file().read_text(encoding="utf-8")  # 캡션 전문은 저장하지 않는다


def test_weekly_hashtag_limit(api, monkeypatch):
    cfg = {**reels.settings(), "weekly_hashtag_limit": 2}
    monkeypatch.setattr(reels, "settings", lambda: cfg)
    with pytest.raises(reels.ReelsError, match="한도"):
        reels.find("rejuran")  # 해시태그 3개 > 한도 2


def test_unknown_topic(api):
    with pytest.raises(reels.ReelsError, match="주제 없음"):
        reels.find("nope")


def test_check_reports_each_step(api):
    feeds, _ = api
    feeds["리쥬란"] = [media("a", likes=1), media("b", kind="IMAGE", product="FEED")]
    out = reels.check()
    assert "@skinboundkorea" in out and "#리쥬란" in out and "인기 게시물 2개 (영상 1개)" in out


def test_missing_env_is_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.delenv("INSTAGRAM_USER_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    assert reels.main(["check"]) == 2


def test_graph_error_explains_code():
    body = '{"error": {"code": 10, "message": "Application does not have permission"}}'
    assert "앱 심사" in reels._explain(body)
    assert "토큰 만료" in reels._explain('{"error": {"code": 190, "message": "expired"}}')


def test_promo_is_warned_not_excluded(api):
    feeds, _ = api
    feeds["리쥬란"] = [media("a", "리쥬란 다운타임 기록 + 이번 주 크림 공구 오픈", likes=10)]
    last = reels.find("rejuran")
    assert [i["id"] for i in last["items"]] == ["a"] and last["items"][0]["warn"] == ["promo"]
    assert "⚠️ 공구·할인 홍보" in reels.message(last)


def test_filters_old_posts_and_low_likes(api, monkeypatch):
    feeds, _ = api
    cfg = {**reels.settings(), "min_likes": 100}
    monkeypatch.setattr(reels, "settings", lambda: cfg)
    old = media("old", "리쥬란 후기", likes=5000)
    old["timestamp"] = "2024-01-01T10:00:00+0000"
    feeds["리쥬란"] = [old, media("low", "리쥬란 후기", likes=20), media("ok", "리쥬란 후기", likes=300),
                     media("hidden", "리쥬란 후기", comments=5)]
    last = reels.find("rejuran")
    assert [i["id"] for i in last["items"]] == ["ok", "hidden"]
    assert last["dropped"] == {"1년 넘음": 1, "좋아요 적음": 1}


def test_top_media_follows_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1")
    pages = {None: {"data": [{"id": "a"}], "paging": {"cursors": {"after": "c1"}, "next": "x"}},
             "c1": {"data": [{"id": "b"}], "paging": {"cursors": {"after": "c2"}, "next": "y"}},
             "c2": {"data": [{"id": "c"}]}}
    reels.set_http(lambda path, params: pages[params.get("after")])
    try:
        assert [m["id"] for m in reels.top_media("t", pages=2)] == ["a", "b"]
        assert [m["id"] for m in reels.top_media("t", pages=5)] == ["a", "b", "c"]
    finally:
        reels.set_http(None)


def test_graph_error_hashtag_not_found():
    body = ('{"error": {"message": "The requested resource does not exist", "code": 24, "error_subcode": 2207024, '
            '"error_user_msg": "리쥬란vlog 해시태그가 존재하지 않아"}}')
    text = reels._explain(body)
    assert "해시태그를 찾을 수 없음" in text and "한도" not in text and "2207024" in text


def test_top_media_shrinks_page_on_too_much_data(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1")
    sizes = []

    def fake(path, params):
        sizes.append(params["limit"])
        if params["limit"] > 12:
            raise reels.ReelsError("GET top_media → 500: [1] Please reduce the amount of data", status=500, code=1)
        return {"data": [{"id": "a"}]}

    reels.set_http(fake)
    try:
        assert [m["id"] for m in reels.top_media("t", page_size=50)] == ["a"]
        assert sizes == [50, 25, 12]
    finally:
        reels.set_http(None)


def test_top_media_keeps_first_page_when_later_page_fails(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1")

    def fake(path, params):
        if params.get("after"):
            raise reels.ReelsError("GET top_media → 400: [100] bad cursor", status=400, code=100)
        return {"data": [{"id": "a"}, {"id": "b"}], "paging": {"cursors": {"after": "c1"}, "next": "x"}}

    reels.set_http(fake)
    try:
        assert [m["id"] for m in reels.top_media("t", pages=3)] == ["a", "b"]
    finally:
        reels.set_http(None)


def test_top_media_first_page_error_still_raises(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1")

    def fake(path, params):
        raise reels.ReelsError("GET top_media → 400: [190] expired", status=400, code=190)

    reels.set_http(fake)
    try:
        with pytest.raises(reels.ReelsError, match="expired"):
            reels.top_media("t", pages=3)
    finally:
        reels.set_http(None)


def test_find_adds_recent_media_and_marks_it(api, monkeypatch):
    feeds, calls = api
    feeds["리쥬란"] = [media("old_top", "리쥬란", likes=100)]
    # 같은 fixture 의 recent 사전을 직접 채운다
    import inspect
    fake = reels._http_impl
    recent = inspect.getclosurevars(fake).nonlocals["recent"]
    recent["리쥬란"] = [media("new", "리쥬란 어제 맞음", likes=5)]
    last = reels.find("rejuran")
    ids = [i["id"] for i in last["items"]]
    assert ids == ["old_top", "new"]
    assert last["items"][1]["fresh"] is True and "🆕 최근 24시간" in reels.message(last)
    assert any(p.endswith("/recent_media") for p, _ in calls)


def test_recent_failure_keeps_top_candidates(api, monkeypatch):
    feeds, _ = api
    feeds["리쥬란"] = [media("a", "리쥬란", likes=1)]
    original = reels._http_impl

    def flaky(path, params):
        if path.endswith("/recent_media"):
            raise reels.ReelsError("GET recent_media → 400: [10] no permission", status=400, code=10)
        return original(path, params)

    reels.set_http(flaky)
    assert [i["id"] for i in reels.find("rejuran")["items"]] == ["a"]
