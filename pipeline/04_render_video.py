"""4. 숏폼 영상 렌더링 — 승인된 초안(approved)의 대본으로 음성(TTS) + 자막 세로 영상(1080x1920)을 만들고 rendered 로 옮긴다.

  - 음성: Google Cloud Text-to-Speech (환경변수 GOOGLE_TTS_API_KEY, 목소리는 config/voice.yaml — 채널 전체 같은 목소리)
  - 영상: ffmpeg (libass) — 브랜드 배경 + 줄마다 큰 자막 + 화면 하단에 계속 떠 있는 표시 줄
          ("AI-generated content", 스폰서 글이면 광고 표시 — 02_draft 가 넣은 on_screen_disclosure 그대로)
  - b-roll(대본의 visual)은 아직 자동으로 붙이지 않는다 → render.json 에 줄별 시간과 함께 남겨 사람이 편집할 때 쓴다
결과: video.mp4, captions.srt (플랫폼 자막 업로드용), voice.mp3, render.json → 폴더째 rendered/ 로 이동.
사람이 영상을 보고 "게시 OK" (03_review --stage rendered) 하면 ready_to_publish → 06_publish.

    python -m pipeline.04_render_video [--id <draft_id>] [--limit 3]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from pipeline.common import content_dir, draft_dirs, get_logger, load_draft, load_yaml, move_draft, now_iso, run_cli, save_json

log = get_logger("04_render_video")

ALLOWED = {("approved", "rendered")}
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
WIDTH, HEIGHT = 1080, 1920
BACKGROUND = "0x0b6e4f"    # 사이트 강조색 (site.py --accent)
GAP_SEC = 0.25             # 줄 사이 쉼


class RenderError(RuntimeError):
    pass


# ---------------- 음성 ----------------

def tts_configured() -> bool:
    return bool(os.environ.get("GOOGLE_TTS_API_KEY"))


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


_tts = _google_tts


def set_tts(fn) -> None:
    """테스트용: TTS 함수 교체 (None 이면 Google TTS)."""
    global _tts
    _tts = fn or _google_tts


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


def build_ass(timeline: list[dict], total: float, disclosure: str, handle: str) -> str:
    events = [f"Dialogue: 0,{_ts_ass(t['start'])},{_ts_ass(t['end'])},Caption,,0,0,0,,{_ass_text(t['caption'])}"
              for t in timeline]
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
Style: Caption,DejaVu Sans,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,0,5,90,90,0,1
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
    _run_in(path, ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                   "-i", f"color=c={BACKGROUND}:s={WIDTH}x{HEIGHT}:r=30:d={total:.2f}", "-i", "voice.mp3",
                   "-vf", "ass=captions.ass", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "video.mp4"])
    save_json(path / "render.json", {"rendered_at": now_iso(), "voice": voice.get("voice"), "seconds": total,
                                     "size": f"{WIDTH}x{HEIGHT}", "timeline": timeline,
                                     "note": "b-roll 은 timeline 의 visual 을 참고해 편집 (자동 합성 전)"})
    shutil.rmtree(work, ignore_errors=True)
    return move_draft(path.name, "approved", "rendered", ALLOWED)


def _run_in(cwd: Path, cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900, check=False)
    if proc.returncode != 0:
        raise RenderError(f"{cmd[0]} 실패: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout


def pending(limit: int | None = None) -> list[Path]:
    paths = [p for p in draft_dirs("approved") if (p / "draft.json").exists()]
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
    parser = argparse.ArgumentParser(description="숏폼 영상 렌더링 (approved → rendered)")
    parser.add_argument("--id", help="특정 초안만")
    parser.add_argument("--limit", type=int, default=3, help="한 번에 렌더링할 최대 건수 (TTS 비용·시간 제한)")
    args = parser.parse_args(argv)
    if not tts_configured():
        log.info("GOOGLE_TTS_API_KEY 미설정 — 렌더링 건너뜀")
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
        print(f"🎬 영상 렌더링 {len(done)}건: " + ", ".join(done))
    for f in failed:
        print(f"⚠️ 렌더링 실패 {f}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    run_cli("04_render_video", main)
