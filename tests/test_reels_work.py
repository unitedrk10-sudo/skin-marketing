"""리믹스 번역 릴스 — 받아쓰기·번역·자막 (pipeline/reels_work.py) 와 해시태그 점검. 가짜 API·LLM·Whisper 로만."""

import json

import pytest

from pipeline import llm, reels, reels_work
from pipeline.llm import LLMResult

URL = "https://scontent-ssn1-1.cdninstagram.com/o1/v/t16/f2/m86/abc.mp4?oh=sig&oe=1"


def media(mid, caption="", likes=None, media_url=URL):
    m = {"id": mid, "caption": caption, "media_type": "VIDEO", "media_product_type": "REELS",
         "permalink": f"https://www.instagram.com/reel/{mid}/", "comments_count": 0, "timestamp": "2026-11-09T10:00:00+0000"}
    if likes is not None:
        m["like_count"] = likes
    if media_url:
        m["media_url"] = media_url
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1789")
    feeds = {"top": [], "recent": []}

    def fake_http(path, params):
        if path == "ig_hashtag_search":
            return {"data": [{"id": f"tag-{params['q']}"}]}
        if path.endswith("/top_media"):
            assert "media_url" in params["fields"]
            return {"data": feeds["top"] if path.startswith("tag-리쥬란/") else []}
        if path.endswith("/recent_media"):
            return {"data": feeds["recent"] if path.startswith("tag-리쥬란/") else []}
        raise AssertionError(path)

    responses, calls = {}, []

    def fake_llm(stage, cfg, system, prompt):
        calls.append((stage, prompt, cfg.get("files")))
        return LLMResult(text=json.dumps(responses[stage], ensure_ascii=False), model="fake", stage=stage)

    fetched = []
    reels.set_http(fake_http)
    llm.set_backend(fake_llm)
    monkeypatch.setattr(reels_work, "_fetch_impl", lambda url, dest: fetched.append(url) or dest.write_bytes(b"mp4"))
    speech = [{"start": 0.0, "end": 3.0, "text": "리쥬란 맞은 지 3일째예요"},
              {"start": 3.0, "end": 6.0, "text": "저는 생각보다 안 아팠어요"}]
    monkeypatch.setattr(reels_work, "_whisper_impl", lambda video, c: list(speech))

    def frames(video, folder, every, max_frames):
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / "f00_000.0s.jpg"
        f.write_bytes(b"jpg")
        return [f], 8.0

    monkeypatch.setattr(reels_work, "extract_frames", frames)
    responses.update({
        "reels_screen": {"texts": [{"t": 0.0, "ko": "리쥬란 3일차"}], "flags": []},
        "reels_translate": {"cues": [
            {"start": 0.0, "end": 3.0, "source": "speech", "ko": "리쥬란 맞은 지 3일째예요", "en": "It's day 3 since my Rejuran."},
            {"start": 3.0, "end": 6.0, "source": "speech", "ko": "저는 생각보다 안 아팠어요", "en": "For me, it hurt less than I expected."}]},
        "reels_review": {"ok": True, "issues": [], "original_overclaims": []},
    })
    yield {"feeds": feeds, "responses": responses, "calls": calls, "speech": speech, "fetched": fetched, "tmp": tmp_path}
    reels.set_http(None)
    llm.set_backend(None)


def _find(env, *items):
    env["feeds"]["top"] = list(items)
    return reels.find("rejuran")


def test_find_keeps_media_url_only_in_state(env):
    last = _find(env, media("a", "리쥬란 후기", likes=10), media("b", "리쥬란 3회차", likes=5, media_url=None))
    text = reels.message(last)
    assert "cdninstagram" not in text and "oh=sig" not in text  # 서명된 영상 주소는 목록·메시지에 내보내지 않는다
    assert [line for line in text.splitlines() if "📼 녹화 필요" in line] == [
        next(line for line in text.splitlines() if line.startswith("2. "))]
    state = reels.load_state()
    assert state["media"]["a"]["media_url"] == URL and "media_url" not in state["media"]["b"]
    assert all("media_url" not in it for it in last["items"])


@pytest.mark.parametrize("url,ok", [
    (URL, True),
    ("https://video.xx.fbcdn.net/v/t42/x.mp4", True),
    ("http://scontent.cdninstagram.com/x.mp4", False),
    ("https://cdninstagram.com.evil.example/x.mp4", False),
    ("https://www.instagram.com/reel/abc/", False),
])
def test_only_instagram_cdn_is_downloaded(url, ok):
    assert reels_work.allowed_media_url(url) is ok


def test_transcribe_makes_subtitles_caption_and_cleans_up(env):
    _find(env, media("a", "리쥬란 후기", likes=10))
    result = reels_work.run("1")
    folder = reels_work.work_dir("a")
    assert result["verdict"] == "ok" and env["fetched"] == [URL]
    srt = (folder / "subtitles.en.srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:03,000\nIt's day 3 since my Rejuran.")
    cap = (folder / "caption.txt").read_text(encoding="utf-8")
    assert "https://www.instagram.com/reel/a/" in cap and "Personal experience — results vary" in cap
    assert "Our take on Rejuran → link in bio" in cap and "AI" in cap
    checklist = (folder / "checklist.md").read_text(encoding="utf-8")
    assert "✅" in checklist and "Translated by Skinbound · more info in bio" in checklist and "리믹스 허용" in checklist
    assert not (folder / "video.mp4").exists() and not (folder / "frames").exists()  # 작업용 영상·프레임은 지운다
    assert "media_url" not in reels.load_state()["media"]["a"]  # 다시 받을 일 없게 주소도 지운다
    stages = [c[0] for c in env["calls"]]
    assert stages == ["reels_screen", "reels_translate", "reels_review"]
    translate_prompt = env["calls"][1][1]
    assert "REMIX TRANSLATION RULES" in translate_prompt and "리쥬란 맞은 지 3일째예요" in translate_prompt
    assert env["calls"][0][2] and env["calls"][0][2][0].endswith(".jpg")  # 프레임을 Claude 에게 파일로


def test_sponsored_speech_blocks_before_translation(env):
    env["speech"].append({"start": 6.0, "end": 8.0, "text": "이 시술은 협찬받아서 했어요"})
    _find(env, media("a", "리쥬란 후기", likes=10))
    result = reels_work.run("1")
    assert result["verdict"] == "blocked" and any("협찬" in b for b in result["check"]["blocks"])
    assert [c[0] for c in env["calls"]] == ["reels_screen"]
    assert not (reels_work.work_dir("a") / "subtitles.en.srt").exists()
    assert "⛔" in (reels_work.work_dir("a") / "checklist.md").read_text(encoding="utf-8")


def test_everyday_clinic_words_in_speech_do_not_block(env):
    env["speech"].append({"start": 6.0, "end": 8.0, "text": "예약하고 상담받고 갔어요"})
    _find(env, media("a", "리쥬란 후기", likes=10))
    assert reels_work.run("1")["verdict"] == "ok"


def test_visual_flags_block_or_warn(env):
    env["responses"]["reels_screen"] = {"texts": [], "flags": [
        {"kind": "other_faces", "t": 2.0, "what": "친구 얼굴"}, {"kind": "needle", "t": 4.0, "what": "주사 클로즈업"}]}
    _find(env, media("a", "리쥬란 후기", likes=10))
    result = reels_work.run("1")
    assert result["verdict"] == "blocked"
    assert any("주사" in b for b in result["check"]["blocks"]) and any("얼굴" in w for w in result["check"]["warns"])


def test_original_overclaim_means_do_not_pick(env):
    env["responses"]["reels_review"] = {"ok": True, "issues": [],
                                        "original_overclaims": [{"cue": 1, "ko": "한 번에 10년 젊어져요", "why": "효과 단정"}]}
    _find(env, media("a", "리쥬란 후기", likes=10))
    result = reels_work.run("1")
    assert result["verdict"] == "overclaim"
    assert "원문 과장" in reels_work.summary(result, reels_work.work_dir("a"))


def test_no_media_url_asks_for_screen_recording(env):
    _find(env, media("a", "리쥬란 후기", likes=10, media_url=None))
    with pytest.raises(reels_work.WorkError, match="--file"):
        reels_work.run("1")
    assert env["fetched"] == []


def test_screen_recording_file(env, tmp_path):
    rec = tmp_path / "rec.mp4"
    rec.write_bytes(b"mp4")
    result = reels_work.run(file=str(rec), permalink="https://www.instagram.com/reel/XYZ_1/", about="Juvelook")
    assert result["id"] == "XYZ_1" and result["verdict"] == "ok"
    assert "Our take on Juvelook" in (reels_work.work_dir("XYZ_1") / "caption.txt").read_text(encoding="utf-8")
    assert rec.exists() and not (reels_work.work_dir("XYZ_1") / "video.mp4").exists()  # 원본 녹화는 운영자 것, 복사본만 지움


def test_long_cues_split_into_two_line_subtitles():
    cue = {"start": 0.0, "end": 6.0, "en": " ".join(["word"] * 30)}
    subs = reels_work.subtitle_cues([cue], width=20)
    assert len(subs) > 1 and all(s["text"].count("\n") <= 1 for s in subs)
    assert subs[0]["start"] == 0.0 and abs(subs[-1]["end"] - 6.0) < 1e-6


def test_clean_cues_sorts_and_removes_overlap():
    cues = reels_work.clean_cues([{"start": 2, "end": 5, "en": "b"}, {"start": 0, "end": 3, "en": "a"},
                                  {"start": 1, "end": 2, "en": ""}, {"start": 7, "end": 99, "en": "c"}], duration=8.0)
    assert [c["en"] for c in cues] == ["a", "b", "c"]
    assert cues[0]["end"] == 2 and cues[2]["end"] == 8.0


def test_extract_frames_from_real_video(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "v.mp4"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        import numpy as np
        for i in range(50):  # 5초
            frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), i * 5, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    frames, duration = reels_work.extract_frames(path, tmp_path / "frames", every=2, max_frames=30)
    assert len(frames) == 3 and 4.5 <= duration <= 5.5
    assert frames[1].name.startswith("f01_002.0s")


@pytest.mark.parametrize("caption,kind", [
    ("드디어 최신 리쥬란 장비가 저희 병원에 도착했습니다", "clinic"),
    ("DOCTORS GONGDEOK", "clinic"),
])
def test_new_caption_signals(caption, kind):
    assert kind in reels.exclusion_reasons(caption, reels.settings()["exclude"])


def test_comment_funnel_phrase_is_warned():
    assert "dm_funnel" in reels.exclusion_reasons("더 궁금한 점이 있다면 댓글로 남겨주세요!", reels.settings()["warn"])


def test_dm_line_is_dropped_from_subtitles_but_video_is_used(env):
    """DM 안내가 있어도 영상은 쓴다 — 자막·캡션에서 그 줄만 빼고 뺀 줄을 checklist 에 남긴다. 제품명 속 '디엠' 은 그대로."""
    env["speech"][:] = [{"start": 0.0, "end": 3.0, "text": "셀리디엠은 성분이 높아요"},
                        {"start": 3.0, "end": 6.0, "text": "궁금하면 DM으로 문의 주세요"}]
    env["responses"]["reels_translate"] = {"cues": [
        {"start": 0.0, "end": 3.0, "source": "speech", "ko": "셀리디엠은 성분이 높아요", "en": "Cellidim has a higher content."},
        {"start": 3.0, "end": 6.0, "source": "speech", "ko": "궁금하면 DM으로 문의 주세요", "en": "DM me if you're curious."}]}
    _find(env, media("a", "리쥬란 후기", likes=10))
    result = reels_work.run("1")
    folder = reels_work.work_dir("a")
    assert result["verdict"] == "ok" and result["check"]["blocks"] == []
    assert any("DM 유도" in w for w in result["check"]["warns"])        # 경고는 남는다
    assert [c["ko"] for c in result["cues"]] == ["셀리디엠은 성분이 높아요"]
    assert [c["ko"] for c in result["removed_cues"]] == ["궁금하면 DM으로 문의 주세요"]
    srt = (folder / "subtitles.en.srt").read_text(encoding="utf-8")
    assert "Cellidim" in srt and "DM me" not in srt
    assert "DM me" not in (folder / "caption.txt").read_text(encoding="utf-8")
    checklist = (folder / "checklist.md").read_text(encoding="utf-8")
    assert "자막에서 뺀 줄" in checklist and "궁금하면 DM으로 문의 주세요" in checklist and "스티커" in checklist
    review_prompt = env["calls"][2][1]
    assert "Cellidim" in review_prompt and "DM me" not in review_prompt   # 검수에도 뺀 줄은 안 넘긴다
    assert "자막에서 뺌" in reels_work.summary(result, folder)


def test_only_dm_lines_means_nothing_to_translate(env):
    env["responses"]["reels_translate"] = {"cues": [
        {"start": 0.0, "end": 3.0, "source": "speech", "ko": "DM 주세요", "en": "DM me."}]}
    _find(env, media("a", "리쥬란 후기", likes=10))
    with pytest.raises(reels_work.WorkError, match="DM"):
        reels_work.run("1")


def test_clinic_voice_is_warned():
    assert "clinic_voice" in reels.exclusion_reasons("이번 영상에서는 블랙헤드 제거가 필요한지 알려드립니다",
                                                     reels.settings()["warn"])


def test_probe_counts_without_marking_seen(env):
    env["feeds"]["top"] = [media("a", "리쥬란", likes=1), {**media("p"), "media_type": "IMAGE", "media_product_type": "FEED"}]
    env["feeds"]["recent"] = [media("c", "강남피부과 리쥬란")]
    rows = reels.probe(["리쥬란", "없는태그"])
    assert rows[0]["top_media"] == {"posts": 2, "videos": 1, "within_age": 1, "clean": 1}
    assert rows[0]["recent_media"]["clean"] == 0
    assert "#리쥬란: 인기 2/1/1/1" in reels.probe_message(rows)
    assert reels.load_state().get("seen", {}) == {}


def test_split_words_at_punctuation_and_length():
    words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "word": w} for i, w in enumerate(
        [" 리투오", " 한", " 달", " 간격", " 3번", " 이상,", " 울쎄라", " 1년에", " 1번", " 이상,", " 피부가", " 좋아지고",
         " 싶다면", " 팔로우", " 좋아요", " 그리고", " 또", " 하나", " 더"])]
    segs = reels_work.split_words(words, max_sec=2.0)
    assert segs[0]["text"] == "리투오 한 달 간격" and segs[1]["text"] == "3번 이상,"
    assert all(s["end"] - s["start"] <= 2.0 for s in segs)
    assert " ".join(s["text"] for s in segs).replace("  ", " ").startswith("리투오 한 달 간격 3번 이상, 울쎄라")


def test_decode_audio_returns_16k_mono(tmp_path):
    av = pytest.importorskip("av")
    import numpy as np
    path = tmp_path / "a.mp4"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("aac", rate=44100)
        stream.layout = "mono"
        for i in range(44):  # 약 1초
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 1024), dtype=np.float32), format="fltp", layout="mono")
            frame.sample_rate, frame.pts = 44100, i * 1024
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    audio = reels_work.decode_audio(path)
    assert audio.dtype == np.float32 and 14000 <= len(audio) <= 18000
