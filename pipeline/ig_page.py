"""인스타그램 프로필 링크 도착 페이지 /ig/ (2026-11-10, 리믹스 번역 릴스 → 홈페이지 연결).

릴스 끝 문구 "Our take on <시술> → link in bio" 를 누르고 온 사람이 그 시술 글을 바로 찾게 한다.
- 내용은 config/reels.yaml `landing` 과 주제별 `posts` (블로그 글 주소) — 코드가 글을 고르지 않는다.
- 스폰서 글·코스 광고 카드가 붙은 글은 넣지 않는다 (릴스 유입을 광고 지면에 바로 붙이지 않는다, shortform-ops-handoff).
- 검색 제외(noindex)·sitemap 제외. 측정은 프로필 링크를 추적 링크(`tracker add --target https://<도메인>/ig/`, `?s=ig`)로 걸어
  추적기에서 센다 (`python -m pipeline.reels traffic`).
"""

from __future__ import annotations

import html

from pipeline.common import get_logger, load_yaml

log = get_logger("ig_page")

PATH = "ig"


def settings() -> dict:
    return load_yaml("reels.yaml")


def sections(posts: list[dict], cfg: dict | None = None, ad_for=None) -> list[tuple[str, list[dict]]]:
    """[(제목, 글 목록)] — 설정 순서 그대로. 없는 글·광고 글은 빼고 로그만 남긴다."""
    cfg = cfg if cfg is not None else settings()
    if ad_for is None:
        from pipeline import site
        ad_for = site.route_ad_sponsor
    by_slug = {p["slug"]: p for p in posts}
    landing = cfg.get("landing") or {}

    def pick(slugs: list[str]) -> list[dict]:
        out = []
        for slug in slugs or []:
            p = by_slug.get(slug)
            if not p:
                log.warning("/ig/: 게시된 글이 아님 — %s (config/reels.yaml)", slug)
            elif p["draft"].get("sponsor") or ad_for(p):
                log.warning("/ig/: 광고가 붙은 글이라 뺌 — %s", slug)
            else:
                out.append(p)
        return out

    rows = []
    for topic in (cfg.get("topics") or {}).values():
        if got := pick(topic.get("posts")):
            rows.append((topic.get("title") or topic.get("en") or "", got))
    if got := pick(landing.get("trip_posts")):
        rows.append((landing.get("trip_title") or "Planning the trip around it", got))
    return rows


def enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else settings()
    return bool((cfg.get("landing") or {}).get("enabled"))


def render(site_cfg: dict, posts: list[dict], cfg: dict | None = None, ad_for=None) -> str | None:
    """페이지 HTML. 넣을 글이 하나도 없으면 None (만들지 않음)."""
    from pipeline import site
    cfg = cfg if cfg is not None else settings()
    rows = sections(posts, cfg, ad_for)
    if not rows:
        return None
    esc = html.escape
    landing = cfg.get("landing") or {}
    parts = [f'<div class="page"><p class="kicker">{esc(landing.get("kicker") or "From our Reels")}</p>'
             f'<h1>{esc(landing.get("heading") or "Our guides on the treatments in our Reels")}</h1>'
             f'<p>{esc(landing.get("intro") or "")}</p></div><div class="wrap">']
    for title, items in rows:
        parts.append(f'<section class="sec"><div class="sec-hd"><h2>{esc(title)}</h2></div>'
                     f'<ul class="grid">{"".join(site.card_html(p) for p in items)}</ul></section>')
    parts.append('<p><a class="btn" href="/">All guides</a></p></div>')
    return site.page(site_cfg, f"From our Reels — {site_cfg['name']}", "".join(parts),
                     path=f"/{PATH}/", description=landing.get("intro") or site_cfg["description"], noindex=True)
