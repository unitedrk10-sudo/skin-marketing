"""블로그 글 대표 사진 — 글 위 큰 사진과 첫 화면 카드에 쓴다 (pipeline/site.py 빌드 때).

순서: ① 직접 넣은 사진 content/site/images/<draft_id>.jpg (사람이 고른 사진으로 바꾸고 싶을 때)
     ② 직접 찍은 사진 content/library/photos/<장소>/ (carousel.library_photo — 폴더 이름 단어가 글에 있으면)
     ③ Pixabay 사진 (PIXABAY_API_KEY, 사람·병원·주사 태그 제외 — carousel.pixabay_photo)
     ④ 없으면 사진 없이 (브랜드 색 배경)
한 번 받은 사진은 content/site/images/ 에 남겨 다시 받지 않는다 (빌드마다 같은 사진 → 배포 해시가 바뀌지 않음).
얼굴·환자·병원 내부·시술 장면은 쓰지 않는다 (체험담·전후 비교로 보일 수 있어서).
"""

from __future__ import annotations

from pathlib import Path

from pipeline import carousel
from pipeline.common import content_dir, get_logger, load_json, save_json

log = get_logger("site_images")

HERO = (1600, 900)
CARD = (720, 450)
# 장소 키워드가 없는 글(시술 정보 등)의 사진 검색어 — 사람 없는 정물·풍경만
CATEGORY_QUERY = {
    "procedure": "skincare cream texture",
    "price_guide": "seoul city street night",
    "how_to": "seoul street",
    "trend": "korean skincare products",
    "review_curation": "seoul cafe interior",
    "skincare": "skincare bottles",
    "procedure_travel": "seoul autumn palace",
}


def cache_dir() -> Path:
    return content_dir() / "site" / "images"


def query_for(draft: dict) -> str:
    sf = draft.get("shortform") or {}
    return (sf.get("cover_photo_query") or carousel._place_keyword(draft)
            or CATEGORY_QUERY.get(draft.get("content_type") or (draft.get("topic") or {}).get("axis", ""), "seoul skincare"))


def source_photo(draft: dict) -> tuple[Path | None, dict]:
    """원본 사진과 출처. 없으면 (None, {})."""
    cache = cache_dir()
    manual = cache / f"{draft['id']}.jpg"
    meta_file = cache / f"{draft['id']}.json"
    if manual.exists():
        return manual, load_json(meta_file) if meta_file.exists() else {"source": "manual"}
    own = carousel.library_photo(draft)
    if own:
        return own, {"source": "library"}
    cache.mkdir(parents=True, exist_ok=True)
    credit = carousel.pixabay_photo(query_for(draft), manual)
    if credit:
        save_json(meta_file, credit)
        return manual, credit
    return None, {}


def build_images(posts: list[dict], out: Path) -> dict[str, dict]:
    """글마다 큰 사진·카드 사진을 out/img/ 에 만든다 → {slug: {"hero", "card", "credit"}}. Pillow 가 없으면 빈 dict."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return {}
    result = {}
    for p in posts:
        try:
            src, credit = source_photo(p["draft"])
        except Exception as e:  # noqa: BLE001 — 사진 하나 실패가 빌드를 막지 않게
            log.warning("대표 사진 실패 %s: %s", p["draft"]["id"], type(e).__name__)
            continue
        if not src:
            continue
        img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        (out / "img").mkdir(parents=True, exist_ok=True)
        for name, size in (("hero", HERO), ("card", CARD)):
            ImageOps.fit(img, size, Image.LANCZOS).save(out / "img" / f"{p['slug']}-{name}.jpg", "JPEG", quality=82,
                                                        optimize=True, progressive=True)
        label = {"pixabay": "Photo: Pixabay", "library": "Photo: Skinbound"}.get(credit.get("source", ""), "")
        result[p["slug"]] = {"hero": f"/img/{p['slug']}-hero.jpg", "card": f"/img/{p['slug']}-card.jpg",
                             "credit": label, "credit_url": credit.get("page", "")}
    return result
