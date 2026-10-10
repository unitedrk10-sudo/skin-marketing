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
PALETTES = {"green": (30, 42, 37), "charcoal": (31, 31, 31)}   # 심볼·카드 바탕 = 사이트 --deep (site.yaml palette)


def _palette_bg() -> tuple:
    from pipeline.common import load_yaml
    return PALETTES.get(load_yaml("site.yaml").get("palette") or "green", PALETTES["green"])


BG = _palette_bg()
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


def symbol(n: int, inverse: bool = False):
    """브랜드 심볼 n×n (투명 배경). inverse = 어두운 바탕용(크림 사각형 + 차콜 S).
    작은 크기는 4배로 그려 줄여서 획이 뭉개지지 않게."""
    from PIL import Image, ImageDraw
    big = n * 4 if n < 128 else n
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box_color, glyph_color = (FG, BG) if inverse else (BG, FG)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * .22), fill=box_color)
    font = _serif(int(big * .82), italic=True)
    box = draw.textbbox((0, 0), "S", font=font)
    draw.text(((big - (box[2] - box[0])) / 2 - box[0], (big - (box[3] - box[1])) / 2 - box[1]), "S", font=font, fill=glyph_color)
    return img if big == n else img.resize((n, n), Image.LANCZOS)


# 로고 묶음 비율 (사이트 CSS 와 같다): 워드마크 글자 크기 F 기준 심볼 1.63F · KOREA 0.32F(자간 .52em) · 간격 0.47F
LOCKUP = {"symbol": 1.63, "korea": 0.32, "korea_track": 0.52, "gap": 0.47}


def lockup(img, x: int, y: int, font_size: int, color, muted, inverse: bool = False) -> int:
    """(x, y) 를 왼쪽 위로 심볼 + Skinbound + KOREA 를 그린다. 반환: 전체 높이(px)."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    s = round(font_size * LOCKUP["symbol"])
    mark = symbol(s, inverse=inverse)
    img.paste(mark, (x, y), mark)
    word_font, korea_font = _serif(font_size), _font(round(font_size * LOCKUP["korea"]))
    tx = x + s + round(font_size * LOCKUP["gap"])
    wb = draw.textbbox((0, 0), "Skinbound", font=word_font)
    word_w, word_h = wb[2] - wb[0], wb[3] - wb[1]
    track = korea_font.size * LOCKUP["korea_track"]
    widths = [draw.textlength(ch, font=korea_font) for ch in "KOREA"]
    korea_w = sum(widths) + track * 4
    kb = draw.textbbox((0, 0), "K", font=korea_font)
    k_h = kb[3] - kb[1]
    gap = round(font_size * 0.3)  # Skinbound 바닥 ~ KOREA 위 (사이트 실측과 같은 비율)
    block = word_h + gap + k_h
    top = y + (s - block) / 2  # 글자 덩어리를 심볼 세로 가운데에
    draw.text((tx - wb[0], top - wb[1]), "Skinbound", font=word_font, fill=color)
    cx = tx + (word_w - korea_w) / 2
    for ch, w in zip("KOREA", widths):
        draw.text((cx, top + word_h + gap - kb[1]), ch, font=korea_font, fill=muted)
        cx += w + track
    return s


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
    x, y = 80, 60
    mark_h = lockup(img, x, y, 44, FG, MUTED, inverse=True)  # 사이트와 같은 비율의 로고 묶음 (어두운 바탕 → 반전 심볼)
    if ad_label:
        font = _font(30, bold=True)
        w = draw.textlength(ad_label, font=font)
        top = y + (mark_h - 48) / 2
        draw.rounded_rectangle((SIZE[0] - 80 - w - 32, top, SIZE[0] - 80, top + 48), radius=8, fill=AD_BG)
        draw.text((SIZE[0] - 80 - w - 16, top + 6), ad_label, font=font, fill=AD_FG)
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


def social_assets(out_dir: Path) -> list[Path]:
    """SNS 프로필용 이미지 (사람이 직접 올린다 — Threads API 는 프로필 수정이 없고 X 는 유료).
    - profile.png 800×800: 차콜 바탕 + 크림 이탤릭 S. 원형으로 잘려도 S 가 가운데·여유 있게 (원 안 지름 70%)
    - x-banner.png 1500×500: 크림 바탕 + 로고 묶음 + 슬로건. X 는 왼쪽 아래에 프로필 사진이 겹치고 휴대폰에서 위아래가 잘리므로
      내용은 오른쪽·가운데 띠에만"""
    from PIL import Image, ImageDraw
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    n = 800
    prof = Image.new("RGB", (n, n), BG)
    draw = ImageDraw.Draw(prof)
    font = _serif(int(n * .62), italic=True)
    box = draw.textbbox((0, 0), "S", font=font)
    draw.text(((n - (box[2] - box[0])) / 2 - box[0], (n - (box[3] - box[1])) / 2 - box[1]), "S", font=font, fill=FG)
    prof.save(out_dir / "profile.png", "PNG", optimize=True)
    made.append(out_dir / "profile.png")

    w, h = 1500, 500
    banner = Image.new("RGB", (w, h), FG)
    draw = ImageDraw.Draw(banner)
    f = 64  # 로고 묶음 글자 크기 → 심볼 1.63F
    sym = round(f * LOCKUP["symbol"])
    x0 = 560  # 왼쪽 아래 프로필 사진 자리를 피해 가운데보다 오른쪽에서 시작
    lockup(banner, x0, 120, f, BG, (125, 112, 98))
    slogan = _serif(46, italic=True)
    draw.text((x0, 120 + sym + 44), "Korean skin treatments, explained honestly.", font=slogan, fill=BG)
    draw.text((x0, 120 + sym + 112), "skinboundkorea.com", font=_font(26), fill=(125, 112, 98))
    banner.save(out_dir / "x-banner.png", "PNG", optimize=True)
    made.append(out_dir / "x-banner.png")
    return made


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
    symbol(192, inverse=True).save(out_dir / "icon-inverse-192.png", "PNG", optimize=True)  # 어두운 꼬리말용


def main(argv: list[str] | None = None) -> int:
    """python -m pipeline.og social  → assets/brand/social/ 에 SNS 프로필 사진·X 배너 (사람이 직접 올린다)."""
    import argparse
    parser = argparse.ArgumentParser(description="브랜드 이미지")
    parser.add_argument("cmd", choices=["social"])
    parser.parse_args(argv)
    for f in social_assets(BRAND_DIR / "social"):
        print(f)
    return 0


if __name__ == "__main__":
    from pipeline.common import run_cli
    run_cli("og", main)
