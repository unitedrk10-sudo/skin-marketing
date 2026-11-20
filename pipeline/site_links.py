"""블로그 사이트 안 연결 (2026-11-10) — 글 → 관련 여행·시술 글, 시술별 목록 페이지(/treatments/).

시술 글을 다 읽은 독자가 그 시술을 여행 일정에 맞추는 글(주변 병원 목록이 있는 곳)로, 여행 글 독자가 그 글의 시술 설명으로
넘어갈 길을 만든다. 고르는 기준은 코드 규칙만 쓴다 (LLM 없음):
  - 시술: config/procedures.yaml 키워드 (pipeline.analytics.tag — 제목·slug·주제 키워드) + group(같은 계열)
  - 관광지: config/attractions.yaml 키워드 (같은 방식)
  - 점수가 같으면 최신 글 먼저.
광고와 무관 (기획서 12-3): 코스 광고 카드·스폰서 계약 유무는 고르는 기준에 들어가지 않고, 관련 글에 스폰서 글은 넣지 않는다.
시술별 목록은 게시일 순서만 (스폰서 글은 첫 화면처럼 광고 배지를 달고 같은 순서에).
"""

from __future__ import annotations

import html

from pipeline import analytics
from pipeline.common import load_yaml

TRAVEL_AXES = {"procedure_travel", "travel_guide"}
GENERIC = {"travel", "skincare", "other"}  # 시술이 아닌 분류 — 시술 목록 페이지를 만들지 않는다
MIN_TREATMENT_POSTS = 2  # 글 1편짜리 목록 페이지는 독자·검색 모두에 얇다
TRIP_LINKS, MORE_LINKS = 2, 3


def procedures() -> dict[str, dict]:
    return load_yaml("procedures.yaml").get("procedures") or {}


def family(key: str, procs: dict[str, dict]) -> str:
    return (procs.get(key) or {}).get("group") or key


def tags(post: dict) -> dict:
    """글의 시술·계열·관광지 (한 번 계산해 post["_links"] 에 둔다)."""
    if "_links" not in post:
        draft = post["draft"]
        meta = {"title": draft["blog"].get("title", ""), "slug": post.get("slug", ""),
                "keywords": (draft.get("topic") or {}).get("keywords") or []}
        procs = procedures()
        found = set(analytics.tag(meta, analytics.load_catalog("procedures"))) - {"other"}
        treat = found - GENERIC
        post["_links"] = {"procedures": treat, "generic": found & GENERIC,
                          "families": {family(k, procs) for k in treat},
                          "attractions": set(analytics.tag(meta, analytics.load_catalog("attractions"))) - {"other"}}
    return post["_links"]


def is_travel(post: dict) -> bool:
    return post["draft"].get("content_type") in TRAVEL_AXES


def score(a: dict, b: dict) -> int:
    """같은 시술 3 · 같은 계열 1 · 같은 관광지 2 · 같은 일반 분류(여행·화장품) 1."""
    ta, tb = tags(a), tags(b)
    return (3 * len(ta["procedures"] & tb["procedures"]) + len(ta["families"] & tb["families"])
            + 2 * len(ta["attractions"] & tb["attractions"]) + len(ta["generic"] & tb["generic"]))


def _by_score_then_date(post: dict, pool: list[dict]) -> list[dict]:
    """점수 높은 순, 같으면 최신 순 (점수 0 은 뺀다)."""
    scored = [(score(post, q), q["date"], q) for q in pool]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored]


def related(post: dict, posts: list[dict]) -> dict[str, list[dict]]:
    """{"trip": 여행 일정 글, "treatments": 이 글의 시술 설명 글, "guides": 시술 설명 글(일반), "more": 그 밖의 관련 글}.
    시술 글: trip = 같은 시술·계열의 여행 글 (없으면 시술과 무관하게 쓰는 일정·비행 글), more = 관련 시술 글 (모자라면 최신 글).
    여행 글: treatments = 이 글에 나오는 시술의 설명 글 (없으면 guides = 최신 시술 설명 글), more = 같은 시술·관광지의 다른 여행 글."""
    pool = [q for q in posts if q is not post and q["slug"] != post["slug"] and not q["draft"].get("sponsor")]
    travel = [q for q in pool if is_travel(q)]
    guides = [q for q in pool if not is_travel(q)]
    newest = lambda qs: sorted(qs, key=lambda q: q["date"], reverse=True)
    if is_travel(post):
        mine = tags(post)["procedures"] | tags(post)["families"]
        treatments = [q for q in _by_score_then_date(post, guides)
                      if mine & (tags(q)["procedures"] | tags(q)["families"])][:MORE_LINKS]
        more = _by_score_then_date(post, travel)[:TRIP_LINKS]
        explainers = [q for q in newest(guides) if tags(q)["procedures"]]  # 시술을 다루는 글 (일정·화장품 일반 글 제외)
        return {"trip": [], "treatments": treatments, "guides": [] if treatments else explainers[:2], "more": more}
    trip = _by_score_then_date(post, travel)[:TRIP_LINKS]
    if not trip:  # 이 시술의 여행 글이 아직 없으면 — 어느 시술에나 쓰는 일정 글 (분류 '여행')
        trip = [q for q in newest(pool) if "travel" in tags(q)["generic"]][:TRIP_LINKS]
    rest = [q for q in guides if q not in trip]
    more = _by_score_then_date(post, rest)[:MORE_LINKS]
    if len(more) < 2:  # 관련 글이 모자라면 최신 글로 채운다 (제목은 "More guides" 라 관련성을 주장하지 않는다)
        more += [q for q in newest(rest) if q not in more][: 2 - len(more)]
    return {"trip": trip, "treatments": [], "guides": [], "more": more}


# ---------------- 시술별 목록 ----------------

def treatment_slug(key: str) -> str:
    return key.replace("_", "-")


def treatment_name(key: str, procs: dict[str, dict] | None = None) -> str:
    procs = procs if procs is not None else procedures()
    return (procs.get(key) or {}).get("name", key).replace(" (general)", "")  # "(general)" 은 분류용 이름


def short_name(key: str, procs: dict[str, dict] | None = None) -> str:
    """칩·링크용 짧은 이름 ("Rejuran (PN skin booster)" → "Rejuran")."""
    name = treatment_name(key, procs)
    return name.split(" (")[0].split(" / ")[0].strip()


def treatment_pages(posts: list[dict]) -> dict[str, list[dict]]:
    """시술 키 → 그 시술(또는 그 시술 계열)을 다루는 글, 게시일 순. 글이 MIN_TREATMENT_POSTS 편 이상인 시술만.
    순서는 글 수 많은 순 → procedures.yaml 관심도 순."""
    procs = procedures()
    pages: dict[str, list[dict]] = {}
    for key in procs:
        if key in GENERIC:
            continue
        hits = [p for p in posts if key in tags(p)["procedures"] or key in tags(p)["families"]]
        if len(hits) >= MIN_TREATMENT_POSTS:
            pages[key] = sorted(hits, key=lambda p: p["date"], reverse=True)
    order = sorted(pages, key=lambda k: (-len(pages[k]), -procs[k].get("demand", 0), k))
    return {k: pages[k] for k in order}


def post_treatments(post: dict, pages: dict[str, list[dict]]) -> list[str]:
    """이 글이 들어간 시술 목록 페이지 (시술 자신 먼저, 계열 다음)."""
    t = tags(post)
    own = [k for k in pages if k in t["procedures"]]
    return own + [k for k in pages if k in t["families"] and k not in own]


def related_html(post: dict, posts: list[dict], pages: dict[str, list[dict]] | None = None) -> str:
    """글 끝(병원 목록·광고 카드 뒤, 출처 앞)의 '이어서 읽기' — 링크는 모두 사이트 안 글."""
    from pipeline.site import category_label  # 순환 import 피함
    esc = html.escape
    rel = related(post, posts)
    procs = procedures()

    def items(qs: list[dict]) -> str:
        return "".join(f'<li><a href="/{q["slug"]}/"><span class="cat">{esc(category_label(q["draft"]))}</span>'
                       f'{esc(q["draft"]["blog"].get("title", ""))}</a></li>' for q in qs)

    groups = [("Plan it around your trip", rel["trip"]), ("The treatments in this guide", rel["treatments"]),
              ("Treatment guides", rel["guides"]),
              ("More Seoul &amp; recovery guides" if is_travel(post) else "More guides", rel["more"])]
    blocks = "".join(f'<h3>{title}</h3><ul>{items(qs)}</ul>' for title, qs in groups if qs)
    chips = "".join(f'<a class="chip" href="/treatments/{treatment_slug(k)}/">{esc(short_name(k, procs))}</a>'
                    for k in post_treatments(post, pages or {}))
    if chips:
        blocks += f'<h3>Browse by treatment</h3><div class="chips">{chips}</div>'
    return f'<section class="related"><h2>Keep reading</h2>{blocks}</section>' if blocks else ""


def render_treatment(cfg: dict, key: str, posts: list[dict]) -> str:
    from pipeline.site import card_html, page
    esc = html.escape
    name = treatment_name(key)
    guides = [p for p in posts if not is_travel(p)]
    trips = [p for p in posts if is_travel(p)]
    sec = lambda title, qs: (f'<section class="sec"><div class="sec-hd"><h2>{title}</h2></div>'
                             f'<ul class="grid">{"".join(card_html(p) for p in qs)}</ul></section>') if qs else ""
    body = (f'<div class="wrap crumbs"><a href="/">Home</a> &nbsp;/&nbsp; <a href="/treatments/">Treatments</a></div>'
            f'<div class="wrap"><header class="art-hd"><p class="kicker">Treatment guides</p><h1>{esc(name)}</h1>'
            f'<p class="sub">Every {esc(cfg["name"])} guide that covers {esc(short_name(key))}, newest first — what it is, then how to fit it '
            f'around a trip to Korea. General information, not medical advice.</p></header>'
            f'{sec("Treatment guides", guides)}{sec("Treatment × travel", trips)}</div>')
    return page(cfg, f"{name} in Korea — guides | {cfg['name']}", body, path=f"/treatments/{treatment_slug(key)}/",
                description=f"Sourced guides on {short_name(key)} in Korea for international visitors: how it works, "
                            f"recovery, and planning it around a Seoul trip.")


def render_treatments_index(cfg: dict, pages: dict[str, list[dict]]) -> str:
    from pipeline.site import page
    esc = html.escape
    procs = procedures()
    items = "".join(f'<li><a href="/treatments/{treatment_slug(k)}/">{esc(treatment_name(k, procs))}<span>{len(v)} guides →</span></a></li>'
                    for k, v in pages.items())
    body = (f'<div class="wrap crumbs"><a href="/">Home</a></div><div class="wrap"><header class="art-hd">'
            f'<p class="kicker">Browse</p><h1>Guides by treatment</h1><p class="sub">Pick a treatment to see every guide that covers it.</p>'
            f'</header><ol class="tlist">{items}</ol></div>')
    return page(cfg, f"Guides by treatment | {cfg['name']}", body, path="/treatments/",
                description="Korean skin treatment guides grouped by treatment, for international visitors.")
