"""영상 화면(b-roll) — 대본 줄마다 실제 영상 클립 후보를 모으고, Claude 가 썸네일 묶음을 보고 고른다 (04_render_video).

후보 출처 (2026-10-07):
  ① 직접 모은 영상 content/library/videos/<단어-단어>/*.mp4 — 폴더 이름의 단어가 줄의 검색어·장면 설명에 모두 있으면 후보.
     (유료 영상 사이트는 개인용 자동 다운로드 API 가 사실상 없어서, 구독해 내려받은 클립도 여기에 넣어 쓴다)
  ② Pixabay 영상 API (PIXABAY_API_KEY, 무료·상업적 사용 가능, 출처 표기 의무 없음)
고르기: 줄마다 후보 썸네일을 번호 붙인 한 장으로 묶어 Claude(pick_visuals, 구독 CLI)에게 한 번에 보낸다.
  얼굴·환자처럼 보이는 사람·병원 내부·주사·전후 비교·로고는 고르지 않는다 (태그로 1차 제외 + Claude 가 그림으로 2차 확인).
  Claude 를 쓸 수 없으면(한도 등) 태그 순서대로 쓴다 — 렌더링은 멈추지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline import llm
from pipeline.common import content_dir, get_logger

log = get_logger("broll")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
PER_LINE = 8                      # 줄마다 보여 줄 후보 수
THUMB = (216, 384)                # 썸네일 (9:16)
NO_TAGS = {"woman", "man", "girl", "boy", "face", "portrait", "people", "person", "model", "lady", "beauty", "makeup",
           "doctor", "patient", "nurse", "surgery", "injection", "syringe", "needle", "hospital", "clinic", "blood",
           "selfie", "child", "baby"}
UA = {"User-Agent": "skin-marketing/1.0"}  # Pixabay(Cloudflare)는 파이썬 기본 User-Agent 를 막는다 (403, 1010)
PICK_RULES = """You are choosing background video clips for a vertical short video (center-cropped to 9:16) that explains
dermatology / skincare information for travelers to Korea. A neutral AI voice reads each line.

For each line below there is an image file with numbered candidate thumbnails (0, 1, 2, …). Read every image file.
Pick, for each line, the clips that best show what the line is about — a real, calm, good-looking scene
(Seoul streets and sights, nature, skincare products, hands applying cream, sunlight, calendars, everyday objects).

Never pick: a recognizable face or anyone who looks like a patient; before/after; clinic or hospital interiors;
needles, injections, surgery, blood; readable logos or brand names; cartoons or 3D renders; anything unrelated.
Prefer different clips for different lines, and clips that look good when cropped to the vertical center.
If nothing fits a line, give an empty list for it.

Return ONLY JSON: {"lines": [{"n": 1, "picks": [3, 0]}, …]} — up to the number of clips each line asks for, best first."""


def library_dir() -> Path:
    return content_dir() / "library" / "videos"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, check=True, timeout=300)


def query_for(line: dict) -> str:
    """대본 작성 단계가 적은 검색어(stock_query). 예전 초안은 장면 설명 앞부분."""
    q = (line.get("stock_query") or "").strip()
    if q:
        return q
    words = re.findall(r"[A-Za-z]+", line.get("visual") or "")
    return " ".join(w for w in words if len(w) > 3)[:60]


def library_candidates(line: dict) -> list[dict]:
    root = library_dir()
    if not root.is_dir():
        return []
    want = _words(query_for(line) + " " + (line.get("visual") or ""))
    out = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if _words(folder.name) and _words(folder.name) <= want:
            out += [{"id": f"lib:{folder.name}/{f.name}", "source": "library", "file": str(f), "tags": folder.name}
                    for f in sorted(folder.iterdir()) if f.suffix.lower() in VIDEO_EXT]
    return out


def _get(url: str, timeout: int):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


_pixabay_cache: dict[str, list] = {}


def pixabay_candidates(query: str) -> list[dict]:
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query:
        return []
    if query not in _pixabay_cache:
        url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
            {"key": key, "q": query, "safesearch": "true", "per_page": 30})
        try:
            _pixabay_cache[query] = json.load(_get(url, 30)).get("hits", [])
        except Exception as e:  # noqa: BLE001 — 주소에 키가 있어 오류 종류만 남긴다
            log.warning("Pixabay 검색 실패 (%s): %s", query, type(e).__name__)
            _pixabay_cache[query] = []
    out = []
    for h in _pixabay_cache[query]:
        if {t.strip() for t in h.get("tags", "").split(",")} & NO_TAGS or h.get("duration", 0) < 3:
            continue
        v = h["videos"]
        best = v.get("large") if (v.get("large") or {}).get("url") else v.get("medium")
        out.append({"id": f"px:{h['id']}", "source": "pixabay", "url": best["url"], "tags": h.get("tags", ""),
                    "thumb_url": (v.get("tiny") or {}).get("thumbnail") or (v.get("small") or {}).get("thumbnail"),
                    "page": h.get("pageURL", ""), "user": h.get("user", "")})
    return out


def candidates(line: dict, used: set[str]) -> list[dict]:
    pool = library_candidates(line) + pixabay_candidates(query_for(line))
    return [c for c in pool if c["id"] not in used][:PER_LINE]


def _thumb(cand: dict, work: Path):
    from PIL import Image, ImageOps
    out = work / f"thumb_{re.sub(r'[^a-z0-9]+', '_', cand['id'].lower())}.jpg"
    if not out.exists():
        if cand["source"] == "library":
            _ffmpeg(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", cand["file"], "-frames:v", "1", str(out)])
        elif cand.get("thumb_url"):
            out.write_bytes(_get(cand["thumb_url"], 30).read())
        else:
            return None
    return ImageOps.fit(Image.open(out).convert("RGB"), THUMB)


def contact_sheet(n: int, cands: list[dict], work: Path) -> Path | None:
    """줄 n 의 후보를 번호 붙인 한 장으로 (4열)."""
    from PIL import Image, ImageDraw
    from pipeline.carousel import _font
    cols, pad = 4, 8
    rows = (len(cands) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (THUMB[0] + pad) + pad, rows * (THUMB[1] + pad) + pad), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    for i, c in enumerate(cands):
        try:
            img = _thumb(c, work)
        except Exception as e:  # noqa: BLE001 — 썸네일 하나 실패는 건너뛴다
            log.warning("썸네일 실패 %s: %s", c["id"], type(e).__name__)
            img = None
        x, y = pad + (i % cols) * (THUMB[0] + pad), pad + (i // cols) * (THUMB[1] + pad)
        if img:
            sheet.paste(img, (x, y))
        draw.rectangle((x, y, x + 52, y + 46), fill=(0, 0, 0))
        draw.text((x + 10, y + 4), str(i), font=_font(34, True), fill=(255, 220, 0))
    out = work / f"line{n:02d}_candidates.jpg"
    sheet.save(out, "JPEG", quality=80)
    return out


def choose(lines: list[dict], pools: list[list[dict]], need: list[int], work: Path) -> list[list[dict]]:
    """줄마다 고른 클립 목록. Claude 를 못 쓰면 후보 순서대로."""
    sheets, asks = [], []
    for i, (line, pool) in enumerate(zip(lines, pools), 1):
        if not pool:
            continue
        sheet = contact_sheet(i, pool, work)
        sheets.append(sheet)
        asks.append(f"Line {i} (needs {need[i - 1]} clip(s)) — image file {sheet.name}\n"
                    f"  voice: {line.get('voice', '')}\n  scene idea: {line.get('visual', '')}")
    picks: dict[int, list[int]] = {}
    if sheets:
        try:
            data, _ = llm.generate_json("pick_visuals", PICK_RULES + "\n\n" + "\n\n".join(asks), files=sheets)
            for item in (data or {}).get("lines", []):
                picks[int(item["n"])] = [int(p) for p in item.get("picks", []) if isinstance(p, (int, str)) and str(p).isdigit()]
        except (llm.LLMError, KeyError, TypeError, ValueError) as e:  # RateLimited 포함 — 렌더링은 계속
            log.warning("화면 고르기 실패, 후보 순서대로: %s", e)
    out = []
    for i, pool in enumerate(pools, 1):
        chosen = [pool[p] for p in picks.get(i, []) if 0 <= p < len(pool)] if i in picks else pool
        out.append(chosen[: need[i - 1]])
    return out


def fetch(cand: dict, work: Path) -> Path:
    if cand["source"] == "library":
        return Path(cand["file"])
    out = work / f"{cand['id'].replace(':', '_')}.mp4"
    if not out.exists():
        out.write_bytes(_get(cand["url"], 180).read())
    return out
