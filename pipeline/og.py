"""공유 이미지(Open Graph 1200x630)·아이콘 — 블로그 빌드 때 글마다 자동 생성 (pipeline/site.py).

SNS·메신저·검색 결과에 링크가 카드로 보일 때 쓰인다. 사진 대신 브랜드 색 배경 + 글 제목 (저작권·초상권 걱정 없음).
스폰서 글은 카드에도 "Sponsored · Ad" 를 넣는다 (광고 표시가 링크 미리보기에서도 보이게).
Pillow 가 없으면 이미지 없이 빌드한다 (og:image 태그만 빠짐).
"""

from __future__ import annotations

from pathlib import Path

from pipeline.common import get_logger

log = get_logger("og")

SIZE = (1200, 630)
# 브랜드 (2026-10-10 디자인 A): 짙은 녹색 바탕·크림 글씨·세이지 보조색, 심볼 = 둥근 사각형 안 Instrument Serif 이탤릭 "S"
BG = (30, 42, 37)          # --deep #1E2A25
FG = (251, 249, 246)       # --bg #FBF9F6
MUTED = (169, 189, 178)    # 세이지 밝은 톤
AD_BG = (255, 246, 224)
AD_FG = (138, 90, 0)
BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"   # Instrument Serif TTF (OFL)
ICON_SIZES = {"icon-32.png": 32, "apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}
MANIFEST = """{"name": "Skinbound", "short_name": "Skinbound", "icons": [
 {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
 "theme_color": "#FBF9F6", "background_color": "#FBF9F6", "display": "browser"}
"""


def _serif(size: int, italic: bool = False):
    """브랜드 세리프 (없으면 기본 글꼴)."""
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(BRAND_DIR / f"InstrumentSerif-{'Italic' if italic else 'Regular'}.ttf"), size)
    except OSError:
        return _font(size, bold=True)


def symbol(n: int):
    """브랜드 심볼 n×n (투명 배경). 작은 크기는 4배로 그려 줄여서 획이 뭉개지지 않게."""
    from PIL import Image, ImageDraw
    big = n * 4 if n < 128 else n
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * .22), fill=BG)
    font = _serif(int(big * .82), italic=True)
    box = draw.textbbox((0, 0), "S", font=font)
    draw.text(((big - (box[2] - box[0])) / 2 - box[0], (big - (box[3] - box[1])) / 2 - box[1]), "S", font=font, fill=FG)
    return img if big == n else img.resize((n, n), Image.LANCZOS)


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "Arial Bold.ttf" if bold else "Arial.ttf",
             "arialbd.ttf" if bold else "arial.ttf", "Helvetica.ttc"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)  # Pillow 10.1+ 내장 확장 글꼴


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return lines


def card(title: str, site_name: str, tagline: str, out: Path, ad_label: str = "") -> Path:
    """링크 미리보기 카드 1200×630 — 심볼·워드마크, 제목은 브랜드 세리프, 스폰서 글은 광고 표시."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(img)
    x, y = 80, 64
    mark = symbol(64)
    ring = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((0, 0, 63, 63), radius=14, outline=MUTED, width=2)
    img.paste(mark, (x, y), mark)
    img.paste(ring, (x, y), ring)
    draw.text((x + 84, y + 2), site_name, font=_serif(50), fill=FG)
    if ad_label:
        font = _font(30, bold=True)
        w = draw.textlength(ad_label, font=font)
        draw.rounded_rectangle((SIZE[0] - 80 - w - 32, y + 8, SIZE[0] - 80, y + 56), radius=8, fill=AD_BG)
        draw.text((SIZE[0] - 80 - w - 16, y + 14), ad_label, font=font, fill=AD_FG)
    font = _serif(78)
    lines = _wrap(draw, title, font, SIZE[0] - 2 * x, 3)
    y = 210 if len(lines) <= 2 else 180
    for line in lines:
        draw.text((x, y), line, font=font, fill=FG)
        y += 92
    draw.text((x, SIZE[1] - 92), tagline, font=_font(28), fill=MUTED)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def icons(out_dir: Path) -> None:
    """파비콘(.ico 16·32·48)·PNG 아이콘·홈 화면 아이콘·웹 앱 매니페스트."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "site.webmanifest").write_text(MANIFEST, encoding="utf-8")
    if not available():
        return
    from PIL import Image
    symbol(256).save(out_dir / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    for name, n in ICON_SIZES.items():
        img = symbol(n)
        if name == "apple-touch-icon.png":  # iOS 는 투명 배경을 검게 채우고 모서리를 직접 둥글린다 → 꽉 찬 사각형
            full = Image.new("RGB", (n, n), BG)
            full.paste(img, (0, 0), img)
            img = full
        img.save(out_dir / name, "PNG", optimize=True)
