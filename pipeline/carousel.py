"""사진 넘기기형 게시물(캐러셀) — 승인된 초안의 대본을 슬라이드 이미지로 만든다 (04_render_video 가 부른다).

2026-10-07 결정: TTS + 무료 영상 짜깁기 영상은 보기에 어색하고 유튜브의 "대량 생산·반복 콘텐츠" 수익화 제외 정책에
가장 가까운 형태라, 숏폼은 우선 사진 넘기기형(인스타그램 캐러셀·틱톡 사진 모드)으로 낸다.

  - 문구는 검수를 마친 대본에서만 가져온다 (새 문장을 만들지 않는다 → 추가 LLM 호출·추가 검수 없음)
      표지 = hook, 슬라이드 = 대본 줄마다 caption(제목) + voice(본문) + 출처 도메인, 마지막 = 의사 상담 안내·저장 유도
  - 모든 슬라이드에 표시 줄 (AI 생성 표시, 스폰서 글이면 02_draft 가 넣은 광고 표시 그대로)
  - 표지 사진: ① 직접 찍은 사진 content/library/photos/<이름>/ (폴더 이름의 단어가 글 주제·키워드에 모두 있으면 사용)
              ② Pixabay 사진 (PIXABAY_API_KEY, 사람 태그 제외) ③ 없으면 브랜드 색
    나중에 사진을 찍어 폴더에 넣고 `04_render_video --refresh <id>` 하면 상태 이동 없이 슬라이드만 다시 만든다.
  - 크기: 인스타그램 1080x1350 (ig_*.png), 틱톡 1080x1920 (tt_*.png) + 텔레그램 확인용 preview.jpg
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from pipeline.common import PRACTICE_KIND, content_dir, get_logger, load_draft, load_yaml, now_iso, save_json

log = get_logger("carousel")

SIZES = {"ig": (1080, 1350), "tt": (1080, 1920)}
GREEN = (11, 110, 79)       # 사이트 강조색 #0b6e4f
CREAM = (247, 243, 235)
INK = (24, 38, 33)
MUTED = (96, 112, 106)
ORANGE = (226, 120, 52)
WHITE = (255, 255, 255)
MARGIN = 84
# 틱톡(세로 9:16)은 아래 20% 에 설명·음악 표시, 오른쪽에 버튼이 겹친다 → 그 영역에는 글자를 두지 않는다
TALL_BOTTOM, TALL_RIGHT = 380, 190
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# 표지 사진에서 빼는 태그 — 사람이 환자·체험담·전후 비교로 보일 수 있고, 병원·시술 장면은 쓰지 않는다
NO_TAGS = {"woman", "man", "girl", "boy", "face", "portrait", "people", "person", "model", "lady", "beauty", "makeup",
           "doctor", "patient", "nurse", "surgery", "injection", "syringe", "needle", "hospital", "clinic", "blood"}
UA = {"User-Agent": "skin-marketing/1.0"}  # Pixabay(Cloudflare)는 파이썬 기본 User-Agent 를 막는다 (403, 1010)


class CarouselError(RuntimeError):
    pass


# ---------------- 슬라이드 내용 ----------------

def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _sources(draft: dict, ids: list[str]) -> list[str]:
    facts = {f["id"]: f for f in draft.get("facts", [])}
    out = []
    for i in ids:
        f = facts.get(i)
        if not f:
            continue
        # 병원 공통 실무 정보는 블로그처럼 병원 주소 없이
        out.append("clinic websites" if f.get("kind") == PRACTICE_KIND else _host(f.get("url", "")))
    return list(dict.fromkeys(h for h in out if h))


def slides(draft: dict) -> list[dict]:
    sf = draft.get("shortform") or {}
    lines = [ln for ln in sf.get("lines", []) if (ln.get("voice") or "").strip()]
    if not lines:
        raise CarouselError(f"{draft.get('id')}: 대본 줄이 없습니다")
    cover = {"kind": "cover", "headline": sf.get("hook") or sf.get("title") or draft["topic"]["title"],
             "body": sf.get("title", "") if sf.get("hook") else ""}
    body = [{"kind": "point", "headline": ln.get("caption") or "", "body": ln["voice"].strip(),
             "sources": _sources(draft, ln.get("fact_ids") or [])} for ln in lines]
    # 마지막 대본 줄이 "팔로우·저장" 안내면 마지막 슬라이드가 대신한다
    if len(body) > 1 and re.search(r"\b(follow|save)\b", body[-1]["body"], re.I) and not body[-1]["sources"]:
        body.pop()
    # 마지막 슬라이드: 저장 유도 + 블로그 안내. 대본에 의사 상담 문장이 없을 때만 여기에 넣는다 (같은 말 반복 방지)
    told = any(re.search(r"\b(doctor|dermatologist|physician)\b", s["body"], re.I) for s in body)
    end = {"kind": "end", "headline": "Save this for your Korea trip",
           "body": "Full guide with every source: link in bio."
                   + ("" if told else " Results and side effects vary — check with a licensed doctor.")}
    return [cover, *body, end]


# ---------------- 그리기 ----------------

FONTS = {True: ["segoeuib.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"],
         False: ["segoeui.ttf", "DejaVuSans.ttf", "arial.ttf", "Arial.ttf"]}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for name in FONTS[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for w in text.split():
        test = f"{line} {w}".strip()
        if line and draw.textlength(test, font=font) > width:
            lines.append(line)
            line = w
        else:
            line = test
    return lines + ([line] if line else [])


def _fit(draw, text: str, width: int, height: int, size: int, bold: bool, min_size: int = 30):
    """상자에 들어가는 가장 큰 글꼴 → (font, lines, line_height)."""
    while True:
        font = _font(size, bold)
        lines = _wrap(draw, text, font, width)
        lh = int(size * 1.25)
        if len(lines) * lh <= height or size <= min_size:
            return font, lines, lh
        size -= 4


def _text(draw, xy: tuple[int, int], lines: list[str], font, lh: int, fill) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += lh
    return y


def _footer(draw, w: int, h: int, disclosure: str, page: str, color) -> None:
    font = _font(28)
    h = _bottom(h)
    draw.text((MARGIN, h - 78), disclosure, font=font, fill=color)
    if page:
        draw.text((w - MARGIN - draw.textlength(page, font=font), h - 78), page, font=font, fill=color)


def _bottom(h: int) -> int:
    return h - TALL_BOTTOM if h > 1500 else h


def _cover_bg(size: tuple[int, int], photo: Path | None):
    from PIL import Image, ImageOps
    w, h = size
    if not photo:
        return Image.new("RGB", size, GREEN)
    img = ImageOps.fit(ImageOps.exif_transpose(Image.open(photo)).convert("RGB"), size, Image.LANCZOS)
    # 아래쪽을 어둡게 — 흰 글씨가 어떤 사진 위에서도 읽히게
    shade = Image.linear_gradient("L").resize((w, h)).point(lambda v: int(max(0, v - 70) * 1.25))
    return Image.composite(Image.new("RGB", size, (0, 0, 0)), img, shade)


def draw_slide(slide: dict, size: tuple[int, int], n: int, total: int, disclosure: str, handle: str,
               photo: Path | None = None):
    from PIL import Image, ImageDraw
    w, h = size
    tall = h > 1500
    page = f"{n}/{total}"
    tw = w - MARGIN - (TALL_RIGHT if tall else MARGIN)  # 글자 폭
    bottom = _bottom(h)
    if slide["kind"] == "cover":
        img = _cover_bg(size, photo)
        draw = ImageDraw.Draw(img)
        draw.text((MARGIN, 90 if not tall else 150), handle, font=_font(34, True), fill=WHITE)
        top = int(h * ((0.36 if tall else 0.48) if photo else 0.30))
        font, lines, lh = _fit(draw, slide["headline"], tw, int(h * 0.30), 104, True)
        y = _text(draw, (MARGIN, top), lines, font, lh, WHITE)
        if slide.get("body"):
            f2, l2, lh2 = _fit(draw, slide["body"], tw, int(h * 0.12), 44, False)
            _text(draw, (MARGIN, y + 24), l2, f2, lh2, (230, 236, 233))
        swipe = "Swipe  →"
        f3 = _font(36, True)
        draw.text((w - MARGIN - draw.textlength(swipe, font=f3), bottom - 140), swipe, font=f3, fill=WHITE)
        _footer(draw, w, h, disclosure, "", (220, 226, 223))
        return img
    if slide["kind"] == "end":
        img = Image.new("RGB", size, GREEN)
        draw = ImageDraw.Draw(img)
        top = int(h * 0.26)
        font, lines, lh = _fit(draw, slide["headline"], tw, int(h * 0.2), 92, True)
        y = _text(draw, (MARGIN, top), lines, font, lh, WHITE)
        f2, l2, lh2 = _fit(draw, slide["body"], tw, int(h * 0.3), 50, False)
        y = _text(draw, (MARGIN, y + 40), l2, f2, lh2, (225, 240, 233))
        draw.text((MARGIN, y + 60), handle, font=_font(40, True), fill=WHITE)
        _footer(draw, w, h, disclosure, page, (200, 228, 216))
        return img
    img = Image.new("RGB", size, CREAM)
    draw = ImageDraw.Draw(img)
    y = 110 if tall else 84
    draw.text((MARGIN, y), f"{n - 1:02d}", font=_font(64, True), fill=ORANGE)
    f_handle = _font(28)
    draw.text((w - MARGIN - draw.textlength(handle, font=f_handle), y + 26), handle, font=f_handle, fill=MUTED)
    y = int(h * (0.25 if tall else 0.2))
    font, lines, lh = _fit(draw, slide["headline"], tw, int(h * 0.22), 100, True)
    y = _text(draw, (MARGIN, y), lines, font, lh, INK)
    draw.rectangle((MARGIN, y + 30, MARGIN + 110, y + 40), fill=GREEN)
    f2, l2, lh2 = _fit(draw, slide["body"], tw, int(h * 0.38), 68, False)
    _text(draw, (MARGIN, y + 84), l2, f2, lh2, INK)
    if slide.get("sources"):
        src = "Source: " + " · ".join(slide["sources"])
        f3, l3, lh3 = _fit(draw, src, tw, 90, 30, False, min_size=24)
        _text(draw, (MARGIN, bottom - 120 - len(l3) * lh3), l3, f3, lh3, MUTED)
    _footer(draw, w, h, disclosure, page, MUTED)
    return img


# ---------------- 표지 사진 ----------------

def library_dir() -> Path:
    return content_dir() / "library" / "photos"


def _words(draft: dict) -> set[str]:
    topic, sf = draft.get("topic") or {}, draft.get("shortform") or {}
    text = " ".join([topic.get("title", ""), " ".join(topic.get("keywords") or []), sf.get("title", ""),
                     sf.get("cover_photo_query", ""), (draft.get("blog") or {}).get("title", "")])
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def library_photo(draft: dict) -> Path | None:
    """직접 찍은 사진: 폴더 이름(예: bukchon, autumn-leaves)의 단어가 모두 글에 나오면 그 폴더 사진 중 하나 (글마다 다르게)."""
    root = library_dir()
    if not root.is_dir():
        return None
    words = _words(draft)
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        need = set(re.findall(r"[a-z0-9]+", folder.name.lower()))
        photos = sorted(p for p in folder.iterdir() if p.suffix.lower() in PHOTO_EXT)
        if need and need <= words and photos:
            pick = int(hashlib.sha1(draft["id"].encode()).hexdigest(), 16) % len(photos)
            return photos[pick]
    return None


def _get(url: str, timeout: int):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def pixabay_photo(query: str, out: Path) -> dict | None:
    """무료 사진 (Pixabay 라이선스: 상업적 사용 가능, 출처 표기 의무 없음). 키·주소는 로그에 남기지 않는다."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query.strip():
        return None
    url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
        {"key": key, "q": query, "image_type": "photo", "orientation": "vertical", "safesearch": "true", "per_page": 30})
    try:
        hits = json.load(_get(url, 30)).get("hits", [])
        for hit in hits:
            if {t.strip() for t in hit.get("tags", "").split(",")} & NO_TAGS:
                continue
            out.write_bytes(_get(hit["largeImageURL"], 60).read())
            return {"source": "pixabay", "query": query, "id": hit["id"], "page": hit.get("pageURL", ""),
                    "user": hit.get("user", "")}
    except Exception as e:  # noqa: BLE001 — 사진이 없어도 슬라이드는 만든다
        log.warning("Pixabay 사진 실패 (%s): %s", query, type(e).__name__)
    return None


MEDICAL_WORDS = {"laser", "treatment", "treatments", "procedure", "injection", "injections", "clinic", "clinics",
                 "dermatology", "dermatologist", "toning", "booster", "boosters", "aftercare", "recovery", "downtime"}


def _place_keyword(draft: dict) -> str:
    """예전 초안(cover_photo_query 없음): 시술 단어가 없는 키워드(장소·계절)만 사진 검색어로. 없으면 "" → 브랜드 색 표지.
    시술 이름으로 사진을 찾으면 엉뚱한 사진(마사지·주사 장면)이 나와서다."""
    procs = load_yaml("procedures.yaml")["procedures"]
    medical = MEDICAL_WORDS | {w for k, p in procs.items() if k not in ("skincare", "travel")
                               for kw in p["keywords"] for w in str(kw).lower().split()}
    for kw in (draft.get("topic") or {}).get("keywords") or []:
        if not set(re.findall(r"[a-z0-9]+", kw.lower())) & medical:
            return kw
    return ""


def cover_photo(draft: dict, work: Path) -> tuple[Path | None, dict]:
    own = library_photo(draft)
    if own:
        return own, {"source": "library", "file": own.name, "folder": own.parent.name}
    query = (draft.get("shortform") or {}).get("cover_photo_query") or _place_keyword(draft)
    out = work / "cover_photo.jpg"
    credit = pixabay_photo(query, out)
    return (out, credit) if credit else (None, {"source": "none"})


# ---------------- 만들기 ----------------

def build(path: Path) -> dict:
    """path/carousel/ 에 슬라이드를 만든다 (상태 이동은 하지 않는다). 반환: carousel.json 내용."""
    try:
        from PIL import Image
    except ImportError as e:
        raise CarouselError("Pillow 가 없습니다 (pip install -r requirements.txt)") from e
    draft = load_draft(path)
    items = slides(draft)
    sf = draft.get("shortform") or {}
    disclosure = sf.get("on_screen_disclosure") or "AI-generated content"
    site = load_yaml("site.yaml")
    handle = site.get("domain") or site.get("name", "")
    out_dir = path / "carousel"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("*"):
        if old.is_file() and old.name != "cover_photo.jpg":
            old.unlink()
    photo, credit = cover_photo(draft, out_dir)
    files: dict[str, list[str]] = {}
    for key, size in SIZES.items():
        files[key] = []
        for n, slide in enumerate(items, 1):
            img = draw_slide(slide, size, n, len(items), disclosure, handle, photo if slide["kind"] == "cover" else None)
            name = f"{key}_{n:02d}.png"
            img.save(out_dir / name, "PNG", optimize=True)
            files[key].append(f"carousel/{name}")
    # 텔레그램 확인용 한 장 (인스타그램 크기 슬라이드를 4열로)
    thumbs = [Image.open(path / f).resize((270, 338)) for f in files["ig"]]
    cols = 4
    sheet = Image.new("RGB", (cols * 270 + (cols + 1) * 10, ((len(thumbs) + cols - 1) // cols) * 348 + 10), (40, 40, 40))
    for i, t in enumerate(thumbs):
        sheet.paste(t, (10 + (i % cols) * 280, 10 + (i // cols) * 348))
    sheet.save(out_dir / "preview.jpg", "JPEG", quality=85)
    prev = path / "carousel.json"
    version = (json.loads(prev.read_text(encoding="utf-8")).get("version", 0) if prev.exists() else 0) + 1
    meta = {"rendered_at": now_iso(), "version": version, "format": "carousel", "slides": items, "files": files,
            "preview": "carousel/preview.jpg", "cover_photo": credit}
    save_json(prev, meta)
    return meta
