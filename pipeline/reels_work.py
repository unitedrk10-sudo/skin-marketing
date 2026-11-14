"""리믹스 번역 릴스 — 고른 영상의 받아쓰기·번역·자막·캡션 (2026-11-10, docs/shortform-ops-handoff.md "리믹스 번역 릴스").

    python -m pipeline.reels transcribe <후보 번호|media id> [--about Rejuran] [--keep] [--force]
    python -m pipeline.reels transcribe --file 녹화.mp4 --permalink https://www.instagram.com/reel/... [--about ...]

흐름: 영상 확보 → 말소리 받아쓰기(로컬 faster-whisper, LLM 아님) → 화면 글자·장면 읽기(Claude, 프레임 이미지)
→ 코드 선별 검사(협찬·병원·DM 유도·전후 비교 + 장면 신호) → Claude 번역(원문 그대로) → Gemini 검수(원문 대조·원문 과장)
→ content/reels/work/<id>/ 에 영어 자막(SRT)·캡션·체크리스트.

영상은 공식 API 해시태그 결과의 media_url 로, **고른 후보만** 받는다. 번역 작업용이고 게시에 쓰지 않으며 작업 뒤 지운다
(--keep 이 아니면 영상·프레임 삭제). media_url 이 없으면(음원 저작권 등으로 Instagram 이 주지 않음) 운영자가 화면을
녹화해 --file 로 넣는다. 리믹스·게시는 사람이 앱에서 한다.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline import llm
from pipeline.common import content_dir, get_logger, now_iso, prompt, read_prompt, render, save_json

log = get_logger("reels_work")

MEDIA_HOSTS = ("cdninstagram.com", "fbcdn.net")  # Instagram 공식 CDN 만 받는다
MAX_BYTES = 300 * 1024 * 1024
VISUAL_LABEL = {"needle": "시술 장면(주사·바늘·피·상처 클로즈업)", "before_after": "전후 비교", "clinic": "병원 간판·이름·의료진",
                "other_faces": "원작자 외 얼굴", "overclaim": "화면 글자 과장"}
VISUAL_BLOCK = {"needle", "before_after", "clinic", "overclaim"}  # 나머지는 ⚠️ (사람이 앱에서 판단)

_whisper_impl = None  # 테스트: fn(video_path, cfg) -> [{"start","end","text"}]
_fetch_impl = None    # 테스트: fn(url, dest) -> None


class WorkError(RuntimeError):
    pass


def reels():
    import importlib
    return importlib.import_module("pipeline.reels")


def cfg() -> dict:
    return reels().settings().get("transcribe") or {}


def work_dir(media_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", media_id)[:80]
    return content_dir() / "reels" / "work" / safe


# ── 영상 확보 ───────────────────────────────────────────────────────────────

def allowed_media_url(url: str) -> bool:
    p = urllib.parse.urlparse(url or "")
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


def download(url: str, dest: Path) -> Path:
    """공식 API 가 준 media_url 만 받는다 (Instagram CDN, https). 주소는 로그에 남기지 않는다 (서명 포함)."""
    if not allowed_media_url(url):
        raise WorkError("media_url 이 Instagram CDN 주소가 아님 — 받지 않음")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _fetch_impl is not None:
        _fetch_impl(url, dest)
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "skin-reels/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            total = 0
            while chunk := resp.read(1 << 20):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise WorkError("영상이 너무 큼 (300MB 초과)")
                out.write(chunk)
    except WorkError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001 — 만료된 서명 주소(403) 등
        dest.unlink(missing_ok=True)
        raise WorkError(f"영상 받기 실패 ({type(e).__name__}: {getattr(e, 'code', '') or e}) — 주소가 만료됐으면 "
                        "find 를 다시 하거나 화면 녹화를 --file 로") from None
    return dest


def candidate(ref: str) -> dict:
    """후보 번호(마지막 목록) 또는 media id → 저장해 둔 후보 정보 (media_url 포함)."""
    state = reels().load_state()
    media = state.get("media") or {}
    items = (state.get("last") or {}).get("items") or []
    if ref.isdigit() and int(ref) <= len(items) and ref not in media:
        ref = items[int(ref) - 1]["id"]
    if ref not in media:
        raise WorkError(f"후보 없음: {ref} — python -m pipeline.reels list 의 번호나 media id")
    return {"id": ref, **media[ref]}


# ── 받아쓰기·프레임 ─────────────────────────────────────────────────────────

def transcribe_audio(video: Path, c: dict) -> list[dict]:
    """로컬 faster-whisper (LLM 아님). 시술·제품 이름 힌트를 initial_prompt 로."""
    if _whisper_impl is not None:
        return _whisper_impl(video, c)
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise WorkError("faster-whisper 가 없습니다 — .venv\\Scripts\\python.exe -m pip install faster-whisper") from e
    model = WhisperModel(c.get("whisper_model", "large-v3-turbo"), device=c.get("device", "cpu"),
                         compute_type=c.get("compute_type", "int8"))
    hints = ", ".join(c.get("hints") or [])
    audio = decode_audio(video)
    if audio is None:
        log.warning("소리 트랙 없음 — 화면 글자만")
        return []
    try:
        segments, _ = model.transcribe(audio, language="ko", vad_filter=True, beam_size=5,
                                       initial_prompt=hints or None, word_timestamps=True)
        out = []
        for s in segments:
            words = [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]
            out += split_words(words, float(c.get("max_cue_sec", 4))) if words else (
                [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}] if s.text.strip() else [])
        return out
    except (IndexError, ValueError, RuntimeError) as e:  # 소리 트랙 없음 등 — 화면 글자만으로 진행
        log.warning("받아쓰기 실패 — 화면 글자만: %s", e)
        return []


def split_words(words: list[dict], max_sec: float = 4.0) -> list[dict]:
    """Whisper 단어 시각으로 구간을 나눈다 — 문장부호(. , ? !)에서, 아니면 max_sec 를 넘기 전에.
    VAD 가 24초를 한 구간으로 묶어 자막 시간이 추정값이 됐던 문제 (2026-11-10 실제 영상)."""
    out, cur = [], []
    for w in words:
        if cur and w["end"] - cur[0]["start"] > max_sec:
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.,?!。]\s*$", w["word"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": round(g[0]["start"], 2), "end": round(g[-1]["end"], 2), "text": "".join(w["word"] for w in g).strip()}
            for g in out if "".join(w["word"] for w in g).strip()]


def decode_audio(video: Path):
    """16kHz 모노 float32 배열 (Whisper 입력). faster-whisper 1.2 의 자체 디코더는 PyAV 15+ 에서 없는 인자
    (metadata_errors)를 써서 실패하므로 직접 푼다 (2026-11-10). 소리 트랙이 없으면 None."""
    import av
    import numpy as np
    with av.open(str(video)) as container:
        if not container.streams.audio:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = []
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks).astype(np.float32) / 32768.0 if chunks else None


def extract_frames(video: Path, folder: Path, every: float, max_frames: int) -> tuple[list[Path], float]:
    """every 초마다 한 장 (최대 max_frames). 파일 이름에 시각을 넣어 Claude 가 시점을 안다. 반환: (프레임, 길이 초)."""
    import av
    folder.mkdir(parents=True, exist_ok=True)
    frames, next_t, last_t = [], 0.0, 0.0
    with av.open(str(video)) as container:
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        stream = container.streams.video[0]
        if duration and max_frames and duration / every > max_frames:
            every = duration / max_frames
        for frame in container.decode(stream):
            t = float(frame.time or 0.0)
            last_t = max(last_t, t)
            if t + 1e-6 < next_t:
                continue
            img = frame.to_image()
            img.thumbnail((540, 960))
            path = folder / f"f{len(frames):02d}_{t:05.1f}s.jpg"
            img.save(path, quality=80)
            frames.append(path)
            next_t = t + every
            if len(frames) >= max_frames:
                break
    return frames, duration or last_t


# ── 검사 ───────────────────────────────────────────────────────────────────

def speech_rules() -> dict:
    """말·화면 글자 검사 규칙 = 캡션 제외 규칙에서 개인 후기에 흔한 말(예약·상담 등, reels.yaml transcribe.speech_ignore)을 뺀 것."""
    ignore = set(cfg().get("speech_ignore") or [])
    return {k: [p for p in v if p not in ignore] for k, v in (reels().settings().get("exclude") or {}).items()}


def check(segments: list[dict], screen: dict) -> dict:
    """코드 선별 검사. blocks = 고르지 않는 이유, warns = 사람이 볼 것."""
    R = reels()
    rules = speech_rules()
    blocks, warns = [], []
    for where, texts in (("말", [s.get("text", "") for s in segments]),
                         ("화면 글자", [t.get("ko", "") for t in screen.get("texts") or []])):
        for text in texts:
            for kind in R.exclusion_reasons(text, rules):
                blocks.append(f"{where}: {R.REASON_LABEL.get(kind, kind)} — “{text[:60]}”")
            for kind in R.exclusion_reasons(text, R.settings().get("warn") or {}):
                warns.append(f"{where}: {R.REASON_LABEL.get(kind, kind)} — “{text[:60]}”")
    for f in screen.get("flags") or []:
        kind = f.get("kind", "")
        line = f"장면 {f.get('t', '?')}초: {VISUAL_LABEL.get(kind, kind)} — {str(f.get('what', ''))[:80]}"
        (blocks if kind in VISUAL_BLOCK else warns).append(line)
    return {"blocks": blocks, "warns": warns}


# ── LLM 단계 ───────────────────────────────────────────────────────────────

def read_screen(frames: list[Path]) -> dict:
    """Claude 가 프레임을 보고 화면 글자를 옮기고 장면 신호를 알린다 (stage reels_screen)."""
    if not frames:
        return {"texts": [], "flags": []}
    data, _ = llm.generate_json("reels_screen", render(read_prompt("reels_screen"),
                                                        files="\n".join(f.name for f in frames)), files=frames)
    if not isinstance(data, dict):
        raise WorkError("화면 읽기 결과가 JSON 객체가 아님")
    return {"texts": [t for t in data.get("texts") or [] if isinstance(t, dict) and t.get("ko")],
            "flags": [f for f in data.get("flags") or [] if isinstance(f, dict) and f.get("kind")]}


def translate(segments: list[dict], screen: dict, duration: float) -> list[dict]:
    """Claude 번역 (stage reels_translate) — 원문 그대로. 반환: 시간순 자막 cue [{start,end,ko,en,source}]."""
    payload = {"duration": round(duration, 1),
               "speech": [{"i": i, **s} for i, s in enumerate(segments, 1)],
               "screen": screen.get("texts") or []}
    data, _ = llm.generate_json("reels_translate", prompt("reels_translate", rules=read_prompt("_reels_rules"),
                                                          hints=", ".join(cfg().get("hints") or []),
                                                          payload=json.dumps(payload, ensure_ascii=False, indent=1)))
    cues = data.get("cues") if isinstance(data, dict) else None
    return clean_cues(cues or [], duration)


def clean_cues(cues: list, duration: float) -> list[dict]:
    """시간 정리: 숫자로, 길이 안으로, 시간순, 겹치면 앞 cue 를 줄인다. 영어가 빈 cue 는 버린다."""
    out = []
    for c in cues:
        if not isinstance(c, dict) or not str(c.get("en", "")).strip():
            continue
        try:
            start, end = float(c.get("start", 0)), float(c.get("end", 0))
        except (TypeError, ValueError):
            continue
        if duration:
            start, end = min(start, duration), min(end, duration)
        if end <= start:
            end = start + 1.5 if not duration else min(duration, start + 1.5)
        if end <= start:
            continue
        out.append({"start": round(start, 2), "end": round(end, 2), "ko": str(c.get("ko", "")).strip(),
                    "en": " ".join(str(c["en"]).split()), "source": c.get("source", "speech")})
    out.sort(key=lambda c: c["start"])
    for a, b in zip(out, out[1:]):
        if a["end"] > b["start"]:
            a["end"] = max(a["start"] + 0.3, b["start"])
    return out


def drop_dm_cues(cues: list[dict]) -> tuple[list[dict], list[dict]]:
    """DM·댓글 유도 줄은 영어 자막에서 뺀다 (2026-11-13 운영자 결정 — 정보가 아니고, 우리가 못 지킬 약속이 된다).
    반환: (남긴 cue, 뺀 cue). 뺀 줄은 checklist.md 에 남겨 운영자가 화면 글자를 스티커로 가릴 수 있게 한다."""
    rules = {"dm_funnel": ((reels().settings().get("warn") or {}).get("dm_funnel")) or []}
    kept, removed = [], []
    for c in cues:
        (removed if reels().exclusion_reasons(c.get("ko", ""), rules) else kept).append(c)
    return kept, removed


def review(cues: list[dict]) -> dict:
    """Gemini 검수 (stage reels_review, 쓴 회사와 다른 모델): 원문 대조 + 원문 자체의 과장."""
    rows = [{"n": n, "ko": c["ko"], "en": c["en"]} for n, c in enumerate(cues, 1)]
    data, _ = llm.generate_json("reels_review", render(read_prompt("reels_review"),
                                                        cues=json.dumps(rows, ensure_ascii=False, indent=1)))
    if not isinstance(data, dict):
        raise WorkError("검수 결과가 JSON 객체가 아님")
    return {"ok": data.get("ok") is True, "issues": data.get("issues") or [],
            "original_overclaims": data.get("original_overclaims") or []}


# ── 결과물 ─────────────────────────────────────────────────────────────────

def wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


def subtitle_cues(cues: list[dict], width: int = 42, max_lines: int = 2) -> list[dict]:
    """긴 영어는 두 줄씩 나눠 시간을 글자 수 비율로 나눈다."""
    out = []
    for c in cues:
        lines = wrap(c["en"], width)
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)] or [[c["en"]]]
        total = sum(len(" ".join(ch)) for ch in chunks) or 1
        t = c["start"]
        for ch in chunks:
            span = (c["end"] - c["start"]) * len(" ".join(ch)) / total
            out.append({"start": t, "end": t + span, "text": "\n".join(ch)})
            t += span
    return out


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(cues: list[dict]) -> str:
    return "\n".join(f"{n}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n" for n, c in enumerate(subtitle_cues(cues), 1))


def overlays(about: str) -> dict:
    c = cfg()
    return {"top": c.get("overlay_top", "Translated by Skinbound · more info in bio"),
            "end": render(c.get("overlay_end", "Our take on {{about}} → link in bio"), about=about)}


def caption(about: str, permalink: str) -> str:
    c = cfg()
    lines = [render(line, about=about, permalink=permalink) for line in c.get("caption_lines") or [
        "🎥 Original: @<원작자> — {{permalink}}",
        "Korean → English translation of the original, word for word.",
        "Personal experience — results vary. Not medical advice.",
        "Translated with AI and checked by a person.",
        "Our take on {{about}} → link in bio"]]
    return "\n".join(lines)


def checklist(result: dict) -> str:
    lines = [f"# 릴스 번역 — {result['id']}", "", f"원본: {result.get('permalink') or '(주소 없음)'}",
             f"길이 {result.get('duration', 0):.1f}초 · 말 {len(result.get('segments') or [])}구간 · "
             f"화면 글자 {len((result.get('screen') or {}).get('texts') or [])}개 · 자막 {len(result.get('cues') or [])}개", ""]
    verdict = result.get("verdict")
    lines.append({"blocked": "⛔ 고르지 않음 — 아래 이유", "overclaim": "⛔ 고르지 않음 — 원문 과장",
                  "review": "⚠️ 번역 검수 지적 — 고친 뒤 사용", "ok": "✅ 검사 통과 — 앱에서 확인 후 리믹스"}.get(verdict, verdict))
    for key, head in (("blocks", "⛔ 선별 검사"), ("warns", "⚠️ 사람 확인")):
        if result.get("check", {}).get(key):
            lines += ["", f"## {head}"] + [f"- {x}" for x in result["check"][key]]
    rv = result.get("review") or {}
    if rv.get("original_overclaims"):
        lines += ["", "## ⛔ 원문 과장 (번역해도 퍼뜨리는 것)"] + [f"- {o.get('ko', '')} — {o.get('why', '')}" for o in rv["original_overclaims"]]
    if rv.get("issues"):
        lines += ["", "## ⚠️ 번역 검수"] + [f"- {i.get('cue', '?')}번: {i.get('problem', '')} → {i.get('suggest', '')}" for i in rv["issues"]]
    if result.get("removed_cues"):
        lines += ["", "## 자막에서 뺀 줄 (DM·댓글 유도 — 영어 자막·캡션에 넣지 않음)"]
        lines += [f"- [{c['start']:.1f}–{c['end']:.1f}s · {'말' if c['source'] == 'speech' else '화면 글자'}] {c['ko']}"
                  for c in result["removed_cues"]]
        lines += ["- 화면에 박힌 글자는 영상에 그대로 남는다 → 앱에서 스티커로 가린다 (원작자 말을 다른 말로 덮어쓰지 않기). 음성은 그대로 나온다"]
    if result.get("cues"):
        lines += ["", "## 자막 (한국어 원문 → 영어)"]
        lines += [f"{n}. [{c['start']:.1f}–{c['end']:.1f}s · {'말' if c['source'] == 'speech' else '화면'}] {c['ko']}\n   → {c['en']}"
                  for n, c in enumerate(result["cues"], 1)]
    ov = result.get("overlays") or {}
    lines += ["", "## 화면 글자 (우리 표시)", f"- 영상 내내 위쪽 가운데, 작게·반투명: {ov.get('top', '')}",
              f"- 마지막 2~3초, 조금 크게: {ov.get('end', '')}",
              "- 아래쪽 약 20%·오른쪽 끝(버튼)은 피하고, 원작자 얼굴·자막·워터마크는 가리지 않는다",
              "", "## 캡션", "```", result.get("caption", ""), "```",
              "", "## 앱에서 확인",
              "- [ ] 리믹스 허용돼 있음", "- [ ] 원작자 계정이 병원·의사 계정이 아님 · 캡션의 @<원작자> 채움",
              "- [ ] 고정 댓글에 광고·DM 유도가 있어도 우리 글에는 옮기지 않음", "- [ ] 병원 간판·이름·위치, 주사·피 클로즈업, 전후 비교 없음",
              "- [ ] 원작자 외 얼굴 없음", "- [ ] 'link in bio' 의 프로필 링크가 스폰서 글·광고 카드 붙은 글이 아님",
              "- [ ] 게시 뒤 이 폴더의 영상·프레임이 지워졌는지 (작업용, 게시에 쓰지 않음)"]
    return "\n".join(lines) + "\n"


# ── 실행 ───────────────────────────────────────────────────────────────────

def run(ref: str | None = None, file: str | None = None, permalink: str = "", about: str | None = None,
        keep: bool = False, force: bool = False) -> dict:
    c = cfg()
    if file:
        src = Path(file)
        if not src.is_file():
            raise WorkError(f"파일 없음: {file}")
        media_id = (re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", permalink or "") or re.match(r"(.*)", src.stem)).group(1)
        info = {"id": media_id, "permalink": permalink, "topic": None}
    else:
        info = candidate(ref or "")
        media_id = info["id"]
    folder = work_dir(media_id)
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "video.mp4"
    frames_dir = folder / "frames"
    try:
        if file:
            shutil.copy(src, video)
        elif not info.get("media_url") and info.get("done"):
            raise WorkError(f"이미 작업한 후보라 영상 주소를 지웠습니다 ({info['done'][:16]}, 결과: {folder}) — 다시 하려면 "
                            f"화면 녹화를 --file 로: --permalink {info.get('permalink', '')}")
        elif not info.get("media_url"):
            raise WorkError("이 후보는 Instagram 이 영상 주소(media_url)를 주지 않았습니다 (음원 저작권 등) — "
                            f"앱에서 화면 녹화 후: python -m pipeline.reels transcribe --file 녹화.mp4 --permalink {info.get('permalink', '')}")
        else:
            download(info["media_url"], video)
        frames, duration = extract_frames(video, frames_dir, float(c.get("frame_every_sec", 2)), int(c.get("max_frames", 30)))
        segments = transcribe_audio(video, c)
        screen = read_screen(frames)
        result = {"id": media_id, "permalink": info.get("permalink", ""), "at": now_iso(), "duration": duration,
                  "segments": segments, "screen": screen, "check": check(segments, screen)}
        topic = (reels().settings().get("topics") or {}).get(info.get("topic") or "", {})
        about = about or topic.get("en") or "this treatment"
        result.update(overlays=overlays(about), caption=caption(about, result["permalink"]), about=about)
        if result["check"]["blocks"] and not force:
            result["verdict"] = "blocked"
            (folder / "subtitles.en.srt").unlink(missing_ok=True)  # 이전 --force 실행의 자막이 남아 쓰이지 않게
        elif not segments and not screen["texts"]:
            raise WorkError("말소리도 화면 글자도 없습니다 — 번역할 것이 없음")
        else:
            result["cues"], result["removed_cues"] = drop_dm_cues(translate(segments, screen, duration))
            if not result["cues"]:
                raise WorkError("번역 결과 자막이 비었습니다" + (" (DM 안내 줄만 있어 모두 뺐음)" if result["removed_cues"] else ""))
            result["review"] = review(result["cues"])
            result["verdict"] = ("overclaim" if result["review"]["original_overclaims"] else
                                 "review" if not result["review"]["ok"] or result["review"]["issues"] else
                                 "blocked" if result["check"]["blocks"] else "ok")
            (folder / "subtitles.en.srt").write_text(srt(result["cues"]), encoding="utf-8")
        (folder / "caption.txt").write_text(result["caption"] + "\n", encoding="utf-8")
        (folder / "checklist.md").write_text(checklist(result), encoding="utf-8")
        save_json(folder / "result.json", result)
        _forget_media_url(media_id)
        return result
    finally:
        if not keep:  # 작업용 영상·프레임은 남기지 않는다 (게시에 쓰지 않음)
            video.unlink(missing_ok=True)
            shutil.rmtree(frames_dir, ignore_errors=True)


def _forget_media_url(media_id: str) -> None:
    """작업이 끝난 후보의 서명된 영상 주소는 지운다 (다시 받을 일이 없게)."""
    R = reels()
    state = R.load_state()
    if media_id in (state.get("media") or {}):
        state["media"][media_id].pop("media_url", None)
        state["media"][media_id]["done"] = now_iso()
        R.save_state(state)


def summary(result: dict, folder: Path) -> str:
    head = {"blocked": "⛔ 고르지 않음", "overclaim": "⛔ 원문 과장 — 고르지 않음", "review": "⚠️ 번역 검수 지적",
            "ok": "✅ 통과"}.get(result.get("verdict"), "?")
    lines = [f"[릴스 번역] {result['id']} — {head}", f"폴더: {folder}"]
    for x in (result.get("check") or {}).get("blocks", [])[:5]:
        lines.append(f"⛔ {x}")
    for x in (result.get("check") or {}).get("warns", [])[:5]:
        lines.append(f"⚠️ {x}")
    for o in (result.get("review") or {}).get("original_overclaims", [])[:3]:
        lines.append(f"⛔ 원문 과장: {o.get('ko', '')}")
    for i in (result.get("review") or {}).get("issues", [])[:5]:
        lines.append(f"⚠️ {i.get('cue', '?')}번: {i.get('problem', '')}")
    if result.get("removed_cues"):
        lines.append(f"DM·댓글 유도 줄 {len(result['removed_cues'])}개는 자막에서 뺌 — 화면 글자면 스티커로 가리기 (checklist.md)")
    if result.get("cues"):
        lines.append(f"자막 {len(result['cues'])}개 → subtitles.en.srt · caption.txt · checklist.md")
    return "\n".join(lines)
