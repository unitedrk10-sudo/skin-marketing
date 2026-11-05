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

    def fake(path, params):
        calls.append((path, params))
        if path == "ig_hashtag_search":
            return {"data": [{"id": f"tag-{params['q']}"}]}
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
    ("경제적 이해관계가 있음", "sponsored"),
    ("thanks! #ad", "sponsored"),
    ("강남아름다운피부과에서 리쥬란", "clinic"),
    ("원장님이 직접 시술", "clinic"),
    ("예약은 프로필 링크", "clinic"),
    ("문의 02-1234-5678", "clinic"),
    ("@gangnam_derma_clinic 감사해요", "clinic"),
    ("좋아요 누르시구 댓글 남겨주시면 바로 디엠가요", "dm_funnel"),
    ("궁금하면 DM 주세요", "dm_funnel"),
    ('병원정보가 궁금하신분들은 댓글로 "정보"라고 달아주세요', "dm_funnel"),
    ("(광고•협찬•수수료❌) 나만 정착할 수 없지 댓글 ㄱㄱ", "dm_funnel"),
    ("리쥬란 전후 사진 비교", "before_after"),
])
def test_exclusion_reasons_finds_signals(caption, kind):
    assert kind in reels.exclusion_reasons(caption, reels.settings()["exclude"])


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
    assert last["dropped"] == {"협찬·체험단": 1, "병원·의사": 1}
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
