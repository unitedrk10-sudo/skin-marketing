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
BG = (11, 110, 79)        # --accent #0b6e4f
FG = (255, 255, 255)
MUTED = (200, 230, 218)
AD_BG = (255, 246, 224)
AD_FG = (138, 90, 0)
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0b6e4f"/>
<path d="M44 20c-3-4-8-6-13-6-7 0-12 4-12 10 0 13 26 7 26 19 0 6-6 9-13 9-6 0-11-2-14-7" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round"/></svg>
"""


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
    from PIL import Image, ImageDraw
    img = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(img)
    x, y = 80, 70
    draw.text((x, y), site_name, font=_font(40, bold=True), fill=FG)
    if ad_label:
        font = _font(30, bold=True)
        w = draw.textlength(ad_label, font=font)
        draw.rounded_rectangle((SIZE[0] - 80 - w - 32, y - 4, SIZE[0] - 80, y + 44), radius=8, fill=AD_BG)
        draw.text((SIZE[0] - 80 - w - 16, y + 2), ad_label, font=font, fill=AD_FG)
    font = _font(62, bold=True)
    lines = _wrap(draw, title, font, SIZE[0] - 2 * x, 4)
    y = 190 if len(lines) <= 3 else 160
    for line in lines:
        draw.text((x, y), line, font=font, fill=FG)
        y += 78
    draw.text((x, SIZE[1] - 90), tagline, font=_font(28), fill=MUTED)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def icons(out_dir: Path) -> None:
    """favicon.svg (최신 브라우저) + apple-touch-icon.png (홈 화면 추가)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "favicon.svg").write_text(ICON_SVG, encoding="utf-8")
    if not available():
        return
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (180, 180), BG)
    draw = ImageDraw.Draw(img)
    font = _font(120, bold=True)
    w = draw.textlength("S", font=font)
    draw.text(((180 - w) / 2, 18), "S", font=font, fill=FG)
    img.save(out_dir / "apple-touch-icon.png", "PNG")
