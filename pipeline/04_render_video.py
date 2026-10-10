"""4. 숏폼 렌더링 — 승인된 초안(approved)을 게시물로 만들고 rendered 로 옮긴다.

형식은 config/channels.yaml shortform.format:
  - video (2026-10-07 기본): TTS + 실제 영상 클립(2~3초 컷, pipeline/broll.py 가 후보를 모으고 Claude 가 고름)
           + 위 장면 제목·아래 단어 강조 자막 + 배경 음악(content/library/music/ 에 있으면). 주 video_per_week 편까지.
  - carousel: 사진 넘기기형 슬라이드 (pipeline/carousel.py) — TTS·영상 없음
`--refresh <id>`: 이미 만든 게시물(rendered·ready_to_publish·published)의 슬라이드만 다시 만든다 (상태 이동 없음) —
직접 찍은 사진을 content/library/photos/ 에 넣은 뒤 표지를 바꿀 때.

  - 음성: Gemini TTS (llm.speech, GEMINI_API_KEY 그대로, 모델은 models.yaml tts, 목소리는 config/voice.yaml — 채널 전체 같은
          목소리 Kore). voice.yaml provider: google 이면 예전 Google Cloud TTS (GOOGLE_TTS_API_KEY — API 키를 받지 않아 현재 미사용)
  - 영상: ffmpeg (libass) — 실제 영상 클립(없으면 브랜드 색) + 자막 + 화면 하단에 계속 떠 있는 표시 줄
          ("AI-generated content", 스폰서 글이면 광고 표시 — 02_draft 가 넣은 on_screen_disclosure 그대로)
  - 쓴 클립의 출처는 render.json clips 에 남는다
결과: video.mp4, captions.srt (플랫폼 자막 업로드용), voice.mp3, render.json → 폴더째 rendered/ 로 이동.
사람이 영상을 보고 "게시 OK" (03_review --stage rendered) 하면 ready_to_publish → 06_publish.

    python -m pipeline.04_render_video [--id <draft_id>] [--limit 3]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import broll, carousel, llm
from pipeline.common import (
    content_dir,
    draft_dirs,
    get_logger,
    load_draft,
    load_json,
    load_yaml,
    move_draft,
    now_iso,
    run_cli,
    save_json,
)

log = get_logger("04_render_video")

ALLOWED = {("approved", "rendered")}
REFRESH_STAGES = ("rendered", "ready_to_publish", "published")
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
WIDTH, HEIGHT = 1080, 1920
BACKGROUND = "0x0b6e4f"    # 사이트 강조색 (site.py --accent)
GAP_SEC = 0.25             # 줄 사이 쉼
FRESH_DAYS = 7            # 영상은 승인 후 일주일 안의 글만 (그보다 오래된 글은 블로그로만)
CUT_SEC = 2.6             # 화면 바뀌는 간격 (쇼츠는 2~3초마다 바뀌어야 넘기지 않는다)
MUSIC_VOLUME = 0.12        # 배경 음악은 목소리 아래로


class RenderError(RuntimeError):
    pass


# ---------------- 음성 ----------------

def render_format() -> str:
    return (load_yaml("channels.yaml").get("shortform") or {}).get("format", "video")


def tts_configured() -> bool:
    provider = (load_yaml("voice.yaml") or {}).get("provider", "gemini")
    return bool(os.environ.get("GEMINI_API_KEY" if provider == "gemini" else "GOOGLE_TTS_API_KEY"))


def ready() -> bool:
    """렌더링할 수 있는가 — 사진 넘기기형은 키가 필요 없다 (Pixabay 표지 사진은 키가 있을 때만). format: off 면 숏폼 중단."""
    if render_format() == "off":
        return False
    return render_format() == "carousel" or tts_configured()


def _gemini_tts(text: str, voice: dict) -> bytes:
    """Gemini TTS (llm.speech) → mp3. 응답은 WAV 또는 원시 PCM(24kHz 16bit mono)이라 ffmpeg 로 변환한다."""
    try:
        audio, mime = llm.speech("tts", text, voice["voice"])
    except llm.LLMError as e:
        raise RenderError(f"TTS 실패: {e}") from e
    rate = float(voice.get("speaking_rate", 1.0))
    with tempfile.TemporaryDirectory(prefix="skin-tts-") as tmp:
        src, out = Path(tmp) / "in", Path(tmp) / "out.mp3"
        src.write_bytes(audio)
        fmt = [] if "wav" in mime or audio[:4] == b"RIFF" else ["-f", "s16le", "-ar", _pcm_rate(mime), "-ac", "1"]
        tempo = ["-filter:a", f"atempo={rate}"] if rate != 1.0 else []
        _run(["ffmpeg", "-y", "-v", "error", *fmt, "-i", str(src), *tempo, str(out)])
        return out.read_bytes()


def _pcm_rate(mime: str) -> str:
    m = re.search(r"rate=(\d+)", mime)
    return m.group(1) if m else "24000"


def _google_tts(text: str, voice: dict) -> bytes:
    audio = {"audioEncoding": "MP3"}
    if float(voice.get("speaking_rate", 1.0)) != 1.0:
        audio["speakingRate"] = float(voice["speaking_rate"])
    body = {"input": {"text": text}, "voice": {"languageCode": voice["language"], "name": voice["voice"]},
            "audioConfig": audio}
    req = urllib.request.Request(f"{TTS_URL}?key={os.environ['GOOGLE_TTS_API_KEY']}", data=json.dumps(body).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return base64.b64decode(json.loads(resp.read().decode())["audioContent"])
    except urllib.error.HTTPError as e:
        raise RenderError(f"TTS 실패 {e.code}: {e.read().decode(errors='replace')[:200]}") from e
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        raise RenderError(f"TTS 실패: {e}") from e


def _default_tts(text: str, voice: dict) -> bytes:
    return (_gemini_tts if voice.get("provider", "gemini") == "gemini" else _google_tts)(text, voice)


_tts = _default_tts


def set_tts(fn) -> None:
    """테스트용: TTS 함수 교체 (None 이면 voice.yaml 의 provider)."""
    global _tts
    _tts = fn or _default_tts


# ---------------- ffmpeg ----------------

def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False)
    if proc.returncode != 0:
        raise RenderError(f"{cmd[0]} 실패: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout


def duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def _ts_ass(sec: float) -> str:
    cs = round(sec * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ts_srt(sec: float) -> str:
    ms = round(sec * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


HIGHLIGHT = "&H00D7FF&"  # 지금 읽는 단어 (노랑, ASS 는 BGR)
WORDS_PER_CHUNK = 4


def word_events(t: dict) -> list[str]:
    """아래 자막: 말하는 문장을 4단어씩, 지금 읽는 단어를 강조. 단어 시간 = 줄 시간 × 글자 수 비율 (TTS 단어 시각이 없어서)."""
    words = _ass_text(t["voice"]).split()
    if not words:
        return []
    span, total_chars = t["end"] - t["start"], sum(len(w) + 1 for w in words)
    events, clock = [], t["start"]
    for c in range(0, len(words), WORDS_PER_CHUNK):
        chunk = words[c:c + WORDS_PER_CHUNK]
        for i, w in enumerate(chunk):
            dur = span * (len(w) + 1) / total_chars
            text = " ".join("{\\c" + HIGHLIGHT + "}" + x + "{\\c&HFFFFFF&}" if j == i else x for j, x in enumerate(chunk))
            events.append(f"Dialogue: 0,{_ts_ass(clock)},{_ts_ass(clock + dur)},Sub,,0,0,0,,{text}")
            clock += dur
    return events


def build_ass(timeline: list[dict], total: float, disclosure: str, handle: str) -> str:
    events = [f"Dialogue: 0,{_ts_ass(t['start'])},{_ts_ass(t['end'])},Caption,,0,0,0,,{_ass_text(t['caption'])}"
              for t in timeline]
    events += [e for t in timeline for e in word_events(t)]
    events.append(f"Dialogue: 1,{_ts_ass(0)},{_ts_ass(total)},Disclosure,,0,0,0,,{_ass_text(disclosure)}")
    if handle:
        events.append(f"Dialogue: 1,{_ts_ass(0)},{_ts_ass(total)},Handle,,0,0,0,,{_ass_text(handle)}")
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,1,0,0,0,100,100,0,0,3,22,0,8,80,80,330,1
Style: Sub,DejaVu Sans,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,5,2,2,80,80,560,1
Style: Disclosure,DejaVu Sans,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,3,2,0,2,60,60,430,1
Style: Handle,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,8,60,60,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"


def build_srt(timeline: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts_srt(t['start'])} --> {_ts_srt(t['end'])}\n{t['voice']}\n"
                     for i, t in enumerate(timeline, 1))


# ---------------- 렌더링 ----------------

def render(path: Path) -> Path:
    """approved/<id> 를 렌더링하고 rendered/<id> 로 옮긴다. 반환: 새 경로."""
    if render_format() == "carousel":
        try:
            carousel.build(path)
        except carousel.CarouselError as e:
            raise RenderError(str(e)) from e
        return move_draft(path.name, "approved", "rendered", ALLOWED)
    return render_video(path)


def refresh(draft_id: str) -> Path:
    """이미 만든 사진 넘기기형 게시물의 슬라이드를 다시 만든다 (표지 사진 교체 등). 상태는 그대로."""
    for stage in REFRESH_STAGES:
        path = content_dir(stage) / draft_id
        if (path / "draft.json").exists():
            try:
                carousel.build(path)
            except carousel.CarouselError as e:
                raise RenderError(str(e)) from e
            return path
    raise RenderError(f"{draft_id}: {', '.join(REFRESH_STAGES)} 에 없습니다")


def render_video(path: Path) -> Path:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RenderError("ffmpeg/ffprobe 가 없습니다 (설치: winget install ffmpeg / brew install ffmpeg / apt install ffmpeg)")
    draft = load_draft(path)
    sf = draft.get("shortform") or {}
    lines = [ln for ln in sf.get("lines", []) if (ln.get("voice") or "").strip()]
    if not lines:
        raise RenderError(f"{path.name}: 대본 줄이 없습니다")
    voice = load_yaml("voice.yaml")
    work = path / "render"
    work.mkdir(exist_ok=True)

    timeline, clock, parts = [], 0.0, []
    for i, ln in enumerate(lines, 1):
        clip = work / f"line{i:02d}.mp3"
        clip.write_bytes(_tts(ln["voice"], voice))
        length = duration(clip)
        timeline.append({"n": i, "start": round(clock, 2), "end": round(clock + length, 2), "voice": ln["voice"],
                         "caption": ln.get("caption") or ln["voice"], "visual": ln.get("visual", "")})
        parts.append(clip)
        clock += length + GAP_SEC
    total = round(clock, 2)

    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\nduration {timeline[i]['end'] - timeline[i]['start'] + GAP_SEC:.2f}\n"
                               for i, p in enumerate(parts)), encoding="utf-8")
    voice_mp3 = path / "voice.mp3"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-af", "aresample=async=1:first_pts=0,apad",
          "-t", f"{total:.2f}", str(voice_mp3)])

    site = load_yaml("site.yaml")
    handle = site.get("domain") or site.get("name", "")
    # 표시 줄은 화면 아래 22% 위에 둔다 — 틱톡·릴스·쇼츠 하단 UI(설명·버튼)에 가리지 않게
    ass = path / "captions.ass"
    ass.write_text(build_ass(timeline, total, sf.get("on_screen_disclosure") or "AI-generated content", handle), encoding="utf-8")
    (path / "captions.srt").write_text(build_srt(timeline), encoding="utf-8")

    segments, clips_meta = plan_segments(lines, timeline, total, work)
    music = pick_music(path.name)
    _run_in(path, compose_cmd(segments, music, total))
    save_json(path / "render.json", {"rendered_at": now_iso(), "voice": voice.get("voice"), "seconds": total,
                                     "size": f"{WIDTH}x{HEIGHT}", "timeline": timeline, "clips": clips_meta,
                                     "music": music.name if music else None})
    shutil.rmtree(work, ignore_errors=True)
    return move_draft(path.name, "approved", "rendered", ALLOWED)


# ---------------- 화면 구성 ----------------

def plan_segments(lines: list[dict], timeline: list[dict], total: float, work: Path) -> tuple[list[tuple], list[dict]]:
    """줄마다 CUT_SEC 안팎으로 잘라 클립을 배정 → [(클립 경로 | None, 초)]. 클립이 없으면 앞 줄 클립, 그것도 없으면 브랜드 색."""
    bounds = [t["start"] for t in timeline] + [total]
    spans = [bounds[i + 1] - bounds[i] for i in range(len(timeline))]
    need = [max(1, round(s / CUT_SEC)) for s in spans]
    used: set[str] = set()
    pools = []
    for ln in lines:
        pool = broll.candidates(ln, used)
        pools.append(pool)
    chosen = broll.choose(lines, pools, need, work)
    segments, meta, last = [], [], []
    for i, (span, n, picks) in enumerate(zip(spans, need, chosen), 1):
        files = []
        for c in picks:
            try:
                files.append(broll.fetch(c, work))
                meta.append({"line": i, "id": c["id"], "source": c["source"], "page": c.get("page", ""),
                             "user": c.get("user", "")})
            except Exception as e:  # noqa: BLE001 — 내려받기 실패한 클립은 건너뛴다
                log.warning("클립 내려받기 실패 %s: %s", c["id"], type(e).__name__)
        files = files or last
        last = files or last
        for k in range(n):
            segments.append((files[k % len(files)] if files else None, span / n))
    return segments, meta


def pick_music(draft_id: str) -> Path | None:
    """배경 음악: content/library/music/ 의 파일 (직접 고른 무료·라이선스 음악만). 글마다 다르게, 없으면 음악 없이."""
    root = content_dir() / "library" / "music"
    tracks = sorted(p for p in root.glob("*") if p.suffix.lower() in {".mp3", ".m4a", ".wav"}) if root.is_dir() else []
    return tracks[sum(draft_id.encode()) % len(tracks)] if tracks else None


def compose_cmd(segments: list[tuple], music: Path | None, total: float) -> list[str]:
    """클립은 확대 효과 없이 9:16 으로 채운다 (움직이는 영상에 확대를 더하면 프레임마다 크기가 정수로 바뀌며 떨린다)."""
    inputs, chains = [], []
    for i, (clip, dur) in enumerate(segments):
        if clip:
            inputs += ["-stream_loop", "-1", "-ss", "0.3", "-t", f"{dur:.3f}", "-i", str(clip)]
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c={BACKGROUND}:s={WIDTH}x{HEIGHT}:r=30"]
        chains.append(f"[{i}:v]fps=30,scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
                      f"setsar=1,eq=brightness=-0.05,trim=duration={dur:.3f},setpts=PTS-STARTPTS[v{i}]")
    n = len(segments)
    graph = chains + ["".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[bg]", "[bg]ass=captions.ass[v]"]
    inputs += ["-i", "voice.mp3"]
    audio = f"{n}:a"
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        graph.append(f"[{n + 1}:a]volume={MUSIC_VOLUME},afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
                     f"[{n}:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        audio = "[a]"
    return ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", audio,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac",
            "-t", f"{total:.2f}", "video.mp4"]


def _run_in(cwd: Path, cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900, check=False)
    if proc.returncode != 0:
        raise RenderError(f"{cmd[0]} 실패: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout


def _moved_at(path: Path, to: str) -> datetime | None:
    f = path / "history.json"
    events = [e for e in (load_json(f) if f.exists() else []) if e.get("to") == to]
    return datetime.fromisoformat(events[-1]["at"]) if events else None


def videos_this_week(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    week = now.isocalendar()[:2]
    return sum(1 for stage in REFRESH_STAGES for p in draft_dirs(stage)
               if (at := _moved_at(p, "rendered")) and at.isocalendar()[:2] == week and not (p / "carousel.json").exists())


def pending(limit: int | None = None, now: datetime | None = None) -> list[Path]:
    """렌더링할 승인 초안. 영상 형식은 주 video_per_week 편까지만 — 그 주에 승인된 글 중 수요 점수가 높은 순.
    나머지 승인 글은 블로그로만 나간다 (블로그는 approved 이후 상태면 게시)."""
    if render_format() == "off":
        return []
    paths = [p for p in draft_dirs("approved") if (p / "draft.json").exists()]
    if render_format() == "video":
        now = now or datetime.now(timezone.utc)
        cap = int((load_yaml("channels.yaml").get("shortform") or {}).get("video_per_week", 2))
        left = max(0, cap - videos_this_week(now))
        fresh = [p for p in paths if (at := _moved_at(p, "approved")) is None or now - at <= timedelta(days=FRESH_DAYS)]
        score = lambda p: ((load_draft(p).get("topic") or {}).get("demand") or {}).get("score", 0)  # noqa: E731
        paths = sorted(fresh, key=score, reverse=True)[:left]
    return paths[:limit] if limit else paths


def render_pending(limit: int | None = None) -> tuple[list[str], list[str]]:
    """반환: (렌더링된 id, 실패 메시지). 한 건 실패가 나머지를 막지 않는다."""
    done, failed = [], []
    for path in pending(limit):
        try:
            render(path)
            done.append(path.name)
        except (RenderError, OSError, ValueError) as e:
            failed.append(f"{path.name}: {e}")
            log.error("렌더링 실패 %s: %s", path.name, e)
    return done, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="숏폼 렌더링 (approved → rendered) — 사진 넘기기형 또는 영상")
    parser.add_argument("--id", help="특정 초안만")
    parser.add_argument("--limit", type=int, default=3, help="한 번에 렌더링할 최대 건수 (TTS 비용·시간 제한)")
    parser.add_argument("--refresh", metavar="ID", help="이미 만든 게시물의 슬라이드만 다시 (상태 이동 없음)")
    args = parser.parse_args(argv)
    if args.refresh:
        path = refresh(args.refresh)
        stage = path.parent.name
        again = " — 이미 게시된 글이면 새 이미지로 직접 다시 올려 주세요" if stage == "published" else ""
        print(f"🖼 슬라이드 다시 만듦: {args.refresh} ({stage}){again}")
        return 0
    if not ready():
        log.info("TTS 키 미설정 (voice.yaml provider: gemini → GEMINI_API_KEY) — 렌더링 건너뜀")
        return 0
    if args.id:
        path = content_dir("approved") / args.id
        if not (path / "draft.json").exists():
            log.error("approved/%s 없음", args.id)
            return 1
        render(path)
        print(f"🎬 렌더링 완료: {args.id} → 확인 후 '게시 OK' (03_review list --stage rendered)")
        return 0
    done, failed = render_pending(args.limit)
    if done:
        what = "사진 넘기기형" if render_format() == "carousel" else "영상"
        print(f"🎬 {what} 렌더링 {len(done)}건: " + ", ".join(done))
    for f in failed:
        print(f"⚠️ 렌더링 실패 {f}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    run_cli("04_render_video", main)
