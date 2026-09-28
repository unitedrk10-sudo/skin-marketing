"""블로그 정적 사이트 생성·배포 — 사람이 승인한 초안의 블로그 글을 AI·검색이 읽기 좋은 사이트로 만든다.

게시 대상: approved/ 이후 상태(approved, rendered, ready_to_publish, published)의 초안 — 사람 검수를 통과한 글만.
(06_publish 의 ready_to_publish/ 제약은 영상 게시에 적용. 블로그는 텍스트라 초안 승인 = 게시 승인.)

    python -m pipeline.site build     # site/dist 생성
    python -m pipeline.site deploy    # build 후 바뀐 게 있으면 Cloudflare Pages 에 배포 (wrangler 로그인 필요)

AI·검색용: 글마다 JSON-LD(MedicalWebPage/Article + FAQPage + citation), 출처 목록, sitemap.xml, robots.txt(AI 크롤러 허용),
llms.txt, rss.xml. 스폰서 글: 광고 배지·표시, 병원 링크 rel="sponsored" + 추적 링크 자동 치환(TRACKER 설정 시).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import urllib.parse
from urllib.parse import urlparse

import markdown

from pipeline import clinics, sponsors, tracker
from pipeline.common import ROOT, content_dir, draft_dirs, get_logger, load_draft, load_json, load_yaml, run_cli, save_json, slugify

log = get_logger("site")

PUBLIC_STATES = ("approved", "rendered", "ready_to_publish", "published")
# 중립 글의 "병원 찾기" 안내 — 개별 병원 대신 정부 공식 명단. 클릭 수 = 병원을 찾으러 나간 독자 수 (영업 근거)
REGISTRY_URL = "https://www.medicalkorea.or.kr/en/registeredhospitals"
REGISTRY_AXES = {"procedure", "price_guide", "how_to", "procedure_travel", "travel_guide", "review_curation", "trend"}
REGISTRY_KEY = "_registry"
CLINIC_LIST_AXES = {"procedure_travel", "travel_guide"}  # 코스 주변 피부과 목록을 붙이는 글 (중립 여행 글만)
FACT_REF = re.compile(r"\s?\[(F\d+)\]")
FAQ_HEADING = re.compile(r"^##\s+.*\b(FAQ|Frequently Asked Questions)\b", re.I | re.M)
SAFE_HREF = re.compile(r"^(https?://|/|#|mailto:)", re.I)


def dist_dir() -> Path:
    return Path(os.environ.get("SKIN_SITE_DIR", ROOT / "site" / "dist"))


def state_file() -> Path:
    return content_dir() / "site" / "state.json"


def config() -> dict:
    cfg = load_yaml("site.yaml")
    cfg["domain"] = (cfg.get("domain") or "").strip().removeprefix("https://").strip("/")
    return cfg


def base_url(cfg: dict) -> str:
    return f"https://{cfg['domain']}" if cfg["domain"] else ""


# ---------------- 글 모으기 ----------------

def published_at(path: Path, draft: dict) -> str:
    """승인 시각 (history.json 의 approved 이동) — 없으면 생성 시각."""
    history = path / "history.json"
    if history.exists():
        for event in load_json(history):
            if event.get("to") == "approved":
                return event["at"]
    return draft.get("created_at", "")


def collect_posts() -> list[dict]:
    posts, used = [], set()
    for state in PUBLIC_STATES:
        for path in draft_dirs(state):
            draft = load_draft(path)
            blog = draft.get("blog") or {}
            if not blog.get("markdown"):
                continue
            slug = slugify(blog.get("slug") or blog.get("title") or draft["id"], 60)
            if slug in used:
                slug = f"{slug}-{hashlib.sha1(draft['id'].encode()).hexdigest()[:4]}"
            used.add(slug)
            posts.append({"draft": draft, "path": path, "slug": slug, "date": published_at(path, draft)})
    return sorted(posts, key=lambda p: p["date"], reverse=True)


# ---------------- 본문 변환 ----------------

def _numbered_sources(md: str, facts: list[dict]) -> tuple[str, list[dict]]:
    """[F#] → 각주 번호. 같은 URL 은 한 번호로 묶는다."""
    by_id = {f["id"]: f for f in facts}
    sources: list[dict] = []
    number_of_url: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        fact = by_id.get(m.group(1))
        if not fact:
            return ""
        if fact["url"] not in number_of_url:
            sources.append({"url": fact["url"], "title": fact.get("source_title") or hostname(fact["url"])})
            number_of_url[fact["url"]] = len(sources)
        n = number_of_url[fact["url"]]
        return f'<sup class="ref"><a href="#src-{n}">[{n}]</a></sup>'

    # 원시 HTML 차단 (LLM 출력·출처 텍스트로 스크립트가 들어오지 않게) — 인용(>)은 유지
    safe = autolink(md.replace("<", "&lt;"))
    return FACT_REF.sub(repl, safe), sources


BARE_URL = re.compile(r"(?<![(\[<\"'=])\bhttps?://[^\s)\]<>\"']+")


def autolink(md: str) -> str:
    """본문에 그냥 적힌 URL 을 마크다운 링크로 (이미 [text](url) 인 것은 그대로)."""
    def repl(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:!?")
        return f"[{url}]({url})" + m.group(0)[len(url):]
    return BARE_URL.sub(repl, md)


def hostname(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _rewrite_links(body: str, sponsor: dict | None, tracked_url: str | None) -> str:
    """안전하지 않은 href 제거, 외부 링크 새 창·noopener, 스폰서 병원 링크는 rel=sponsored (+추적 링크)."""
    official = sponsors.official_host(sponsor) if sponsor else None

    def repl(m: re.Match) -> str:
        href = html.unescape(m.group(1))
        if not SAFE_HREF.match(href):
            return 'href="#"'
        if href.startswith("http"):
            host = hostname(href)
            if official and (host == official or host.endswith("." + official)):
                target = tracked_url or href
                return f'href="{html.escape(target)}" rel="sponsored noopener" target="_blank"'
            return f'href="{html.escape(href)}" rel="noopener" target="_blank"'
        return m.group(0)

    return re.sub(r'href="([^"]*)"', repl, body)


def render_markdown(md: str) -> str:
    return markdown.markdown(md, extensions=["extra", "sane_lists", "toc"], output_format="html")


def strip_leading_h1(md: str) -> str:
    """제목은 페이지 H1 으로 따로 넣으므로 본문의 첫 H1 을 뺀다 (스폰서 글은 광고 표시 뒤에 H1 이 온다)."""
    return re.sub(r"^#\s+[^\n]*\n?", "", md, count=1, flags=re.M)


def extract_faq(md: str) -> list[dict]:
    m = FAQ_HEADING.search(md)
    if not m:
        return []
    section = md[m.end():]
    nxt = re.search(r"^##\s", section, re.M)
    section = section[: nxt.start()] if nxt else section
    faq = []
    for q in re.finditer(r"^###\s+(.+?)\s*$\n(.*?)(?=^###\s|\Z)", section, re.M | re.S):
        answer = re.sub(r"\s+", " ", FACT_REF.sub("", q.group(2))).strip()
        answer = re.sub(r"[*_`>]", "", answer)
        if answer:
            faq.append({"q": q.group(1).strip().rstrip("?") + "?", "a": answer})
    return faq


# ---------------- HTML ----------------

CSS = """
:root{--fg:#1d1d1f;--muted:#5f6368;--bg:#fff;--line:#e5e5ea;--accent:#0b6e4f;--ad:#8a5a00;--adbg:#fff6e0}
@media (prefers-color-scheme:dark){:root{--fg:#ececf1;--muted:#a1a1aa;--bg:#141416;--line:#2c2c30;--accent:#4cc39a;--ad:#f3c969;--adbg:#2b2413}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.65 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main,header,footer{max-width:720px;margin:0 auto;padding:0 20px}header{padding-top:24px}header a{color:var(--fg);text-decoration:none;font-weight:700}
.tagline{color:var(--muted);font-size:.95em;margin:.2em 0 1.5em}a{color:var(--accent)}h1{line-height:1.25;font-size:1.9em}
.meta{color:var(--muted);font-size:.9em}.badge{display:inline-block;background:var(--adbg);color:var(--ad);border:1px solid var(--ad);border-radius:4px;padding:0 6px;font-size:.8em;font-weight:600}
blockquote{margin:1em 0;padding:.6em 1em;border-left:4px solid var(--ad);background:var(--adbg)}sup.ref a{text-decoration:none;font-size:.8em}
.box{margin:1.5em 0;padding:.8em 1em;border:1px solid var(--line);border-radius:6px}.box h2{margin:.3em 0 .5em;font-size:1.25em}
.clinics ol{font-size:.95em;padding-left:1.4em}
.adcard{margin:1em 0;padding:.5em .8em;border:1px dashed var(--line);border-radius:6px;font-size:.9em}
.sources{font-size:.9em;border-top:1px solid var(--line);margin-top:2em}.sources li{word-break:break-word}
.posts{list-style:none;padding:0}.posts li{padding:.8em 0;border-bottom:1px solid var(--line)}.posts a{font-weight:600;text-decoration:none}
footer{color:var(--muted);font-size:.85em;border-top:1px solid var(--line);margin-top:3em;padding-bottom:40px}table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:4px 8px}
"""


def page(cfg: dict, title: str, body: str, *, path: str, description: str = "", jsonld: list[dict] | None = None,
         noindex: bool = False) -> str:
    base = base_url(cfg)
    esc = html.escape
    head = [
        '<meta charset="utf-8">', '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{esc(title)}</title>", f'<meta name="description" content="{esc(description or cfg["description"])}">',
        f'<meta property="og:title" content="{esc(title)}">', f'<meta property="og:site_name" content="{esc(cfg["name"])}">',
        f'<link rel="alternate" type="application/rss+xml" title="{esc(cfg["name"])}" href="/rss.xml">',
        f"<style>{CSS}</style>",
    ]
    if base:
        head.append(f'<link rel="canonical" href="{base}{path}">')
    if cfg.get("analytics_token"):  # Cloudflare Web Analytics — 쿠키 없음, 방문·유입 경로(AI 검색 포함) 집계
        beacon = html.escape(json.dumps({"token": cfg["analytics_token"]}))
        head.append(f'<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon="{beacon}"></script>')
    if noindex:
        head.append('<meta name="robots" content="noindex">')
    for data in jsonld or []:
        head.append('<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    contact = f' · <a href="mailto:{esc(cfg["contact_email"])}">Contact</a>' if cfg.get("contact_email") else ""
    return f"""<!doctype html>
<html lang="{cfg.get('language', 'en')}"><head>{''.join(head)}</head>
<body><header><a href="/">{esc(cfg['name'])}</a><p class="tagline">{esc(cfg['tagline'])}</p></header>
<main>{body}</main>
<footer><p>AI-assisted, source-backed information — not medical advice. Always consult a licensed doctor.
Sponsored posts are clearly labeled advertisements.</p>
<p><a href="/about/">About &amp; editorial policy</a> · <a href="/privacy/">Privacy</a>{contact} · <a href="/rss.xml">RSS</a></p></footer>
</body></html>"""


def post_jsonld(cfg: dict, post: dict, sources: list[dict], faq: list[dict]) -> list[dict]:
    draft, blog = post["draft"], post["draft"]["blog"]
    base = base_url(cfg)
    org = {"@type": "Organization", "name": cfg["name"], **({"url": base + "/"} if base else {})}
    article = {
        "@context": "https://schema.org",
        "@type": ["Article", "MedicalWebPage"],
        "headline": blog.get("title", ""),
        "description": blog.get("meta_description", ""),
        "datePublished": post["date"],
        "dateModified": post["date"],
        "author": {**org, "name": cfg["byline"]},
        "publisher": org,
        "inLanguage": cfg.get("language", "en"),
        "citation": [s["url"] for s in sources],
        "keywords": ", ".join(draft.get("topic", {}).get("keywords", [])),
    }
    if base:
        article["url"] = f"{base}/{post['slug']}/"
    if draft.get("sponsor"):
        s = draft["sponsor"]
        article["sponsor"] = {"@type": "MedicalOrganization", "name": s["name_en"], "url": s["official_url"]}
    out = [article]
    if faq:
        out.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faq]})
    return out


def registry_box(registry_url: str | None) -> str:
    href = html.escape(registry_url or REGISTRY_URL)
    return (f'<aside class="box"><strong>Looking for a clinic?</strong> Check whether a clinic is registered for '
            f'international patients on the government-run <a href="{href}" rel="noopener" target="_blank">Medical Korea '
            f'registry</a>. We don\'t recommend or rank individual clinics.</aside>')


def clinic_directory_html(post: dict) -> str:
    """여행 글 코스 주변 피부과 전부 (심평원 공공데이터, 거리순, 추천·순위 없음, 광고주 표시). pipeline/clinics.py"""
    draft = post["draft"]
    text = " ".join([draft["blog"].get("title", ""), " ".join((draft.get("topic") or {}).get("keywords", [])),
                     draft["blog"].get("markdown", "")])
    stops = clinics.directory(text)
    if not stops:
        return ""
    esc = html.escape
    parts = []
    for stop in stops:
        rows = []
        for c in stop["clinics"]:
            ad = c["advertiser"]
            tag = f' <span class="badge">Advertiser</span>' if ad else ""
            maps = "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{c['name']} {c['addr']}")
            rows.append(f'<li>{esc(c["name"])}{tag} <span class="meta">{esc(c["type"])} · {esc(c["district"])} · '
                        f'{c["distance_m"]} m · <a href="{esc(maps)}" rel="nofollow noopener" target="_blank">map</a></span></li>')
        shown = len(stop["clinics"])
        count = (f"all {stop['total']}" if shown == stop["total"] else f"the {shown} closest of {stop['total']}")
        parts.append(f'<details><summary>Near {esc(stop["name"])}: {count} clinics offering dermatology within '
                     f'{stop["radius_m"]} m</summary><ol>{"".join(rows)}</ol></details>')
    fetched = max(s["fetched_at"] for s in stops)
    return (f'<section class="box clinics"><h2>Dermatology clinics near this route</h2>'
            f'<p class="meta">Every clinic listed in the Korean government\'s public health-insurance facility data (HIRA) '
            f'with a dermatology department within the radius, sorted by distance only (as of {esc(fetched)}). '
            f'We do not recommend, rank or review clinics; names are shown in Korean as registered. '
            f'"Advertiser" marks clinics that pay us for labeled ads — it does not change their place in the list. '
            f'Check a clinic\'s status on the official registry before booking.</p>{"".join(parts)}</section>')


def route_ads_file() -> Path:
    return content_dir() / "site" / "route_ads.json"


def route_ad_sponsor(post: dict) -> dict | None:
    """중립 여행 글의 코스 광고 카드에 들어갈 광고주 1곳 (없으면 None). 글 하나 = 광고주 하나 (독점 지면).
    조건: route_ad 계약·광고 문구 병원 확인·계약 기간·코스 정류장과 같은 권역.
    배정은 content/site/route_ads.json 에 고정 — 계약 기간 동안 그 글은 그 광고주 것 (새 광고주가 와도 안 바뀜).
    새 글은 같은 권역 광고주 중 배정된 글이 가장 적은 곳에. 권역 독점(route_ad.exclusive) 광고주는 그 권역 글 전부."""
    draft = post["draft"]
    if draft.get("sponsor") or draft.get("content_type") not in CLINIC_LIST_AXES:
        return None
    try:
        registered = sponsors.load().values()
    except sponsors.SponsorError as e:
        log.warning("스폰서 목록 오류 — 코스 광고 생략: %s", e)
        return None
    from pipeline import attractions
    catalog = attractions.load()
    text = " ".join([draft["blog"].get("title", ""), draft["blog"].get("markdown", "")])
    zones = {catalog[a]["zone"] for a in clinics.stops_for(text)}
    candidates = sorted((s for s in registered if s.get("route_ad") and s.get("zone") in zones
                         and not sponsors.problems(s)), key=lambda s: s["id"])
    exclusive = [s for s in candidates if s["route_ad"]["exclusive"]]
    candidates = exclusive or candidates
    if not candidates:
        return None
    assigned = load_json(route_ads_file()) if route_ads_file().exists() else {}
    current = assigned.get(draft["id"])
    by_id = {s["id"]: s for s in candidates}
    if current in by_id:
        return by_id[current]
    load = {s["id"]: sum(1 for v in assigned.values() if v == s["id"]) for s in candidates}
    chosen = min(candidates, key=lambda s: (load[s["id"]], s["id"]))
    assigned[draft["id"]] = chosen["id"]
    save_json(route_ads_file(), assigned)
    return chosen


def route_ad_html(cfg: dict, sponsor: dict | None, tracked_url: str | None) -> str:
    """조용한 광고 카드: 작게·본문 뒤에, 표시는 분명히 ("Ad"). 설정한 나라(기본 KR)에서는 보이지 않는다 (해외 대상 광고)."""
    if not sponsor or not (cfg.get("route_ads") or {}).get("enabled", True):
        return ""
    esc = html.escape
    href = esc(tracked_url or sponsor["official_url"])
    tagline = sponsor["route_ad"].get("tagline") or ""
    area = sponsor.get("area") or ""
    label = "Ad · Partner (unpaid pilot)" if sponsors.is_pilot(sponsor) else "Ad · Sponsored"
    return (f'<aside class="adcard" data-geo-ad hidden><span class="badge">{label}</span> '
            f'<strong>{esc(sponsor["name_en"])}</strong>{" — " + esc(tagline) if tagline else ""}'
            f'{" · " + esc(area) if area else ""} · <a href="{href}" rel="sponsored noopener" target="_blank">Official website</a>'
            f'<br><span class="meta">Advertisement. Not a recommendation; it does not affect the clinic list or this guide.</span>'
            f'</aside>')


def geo_ad_script(cfg: dict) -> str:
    """광고 카드는 기본 숨김 → 방문 국가가 제외 국가가 아니면 표시 (Cloudflare /cdn-cgi/trace). 확인 실패 시 숨김 유지."""
    hide = [c.upper() for c in (cfg.get("route_ads") or {}).get("hide_in_countries", ["KR"])]
    return ('<script>(function(){var a=document.querySelectorAll("[data-geo-ad]");if(!a.length)return;'
            'fetch("/cdn-cgi/trace").then(function(r){return r.text()}).then(function(t){var m=/loc=([A-Z]{2})/.exec(t);'
            f'if(m&&{json.dumps(hide)}.indexOf(m[1])<0)a.forEach(function(e){{e.hidden=false}})}}).catch(function(){{}})}})();</script>')


def render_post(cfg: dict, post: dict, tracked_url: str | None = None, registry_url: str | None = None,
                ad_url: str | None = None) -> str:
    draft = post["draft"]
    blog = draft["blog"]
    sponsor = draft.get("sponsor")
    md, sources = _numbered_sources(strip_leading_h1(blog["markdown"]), draft.get("facts", []))
    body_html = _rewrite_links(render_markdown(md), sponsor, tracked_url)
    esc = html.escape
    badge = f'<span class="badge">{sponsors.label(sponsor)} · Ad by {esc(sponsor["name_en"])}</span> ' if sponsor else ""
    ad_sponsor = route_ad_sponsor(post)
    src_items = "".join(
        f'<li id="src-{i}"><a href="{esc(s["url"])}" rel="noopener" target="_blank">{esc(s["title"])}</a> '
        f'<span class="meta">({esc(hostname(s["url"]))})</span></li>' for i, s in enumerate(sources, 1))
    body = f"""<article><h1>{esc(blog.get('title', ''))}</h1>
<p class="meta">{badge}{esc(post['date'][:10])} · {esc(cfg['byline'])} · Based on publicly available sources</p>
{body_html}
{registry_box(registry_url) if not sponsor and draft.get("content_type") in REGISTRY_AXES else ""}
{clinic_directory_html(post) if not sponsor and draft.get("content_type") in CLINIC_LIST_AXES else ""}
{route_ad_html(cfg, ad_sponsor, ad_url)}
<section class="sources"><h2>Sources</h2><ol>{src_items}</ol></section></article>{geo_ad_script(cfg) if ad_sponsor else ""}"""
    faq = extract_faq(strip_leading_h1(blog["markdown"]))
    return page(cfg, f"{blog.get('title', '')} | {cfg['name']}", body, path=f"/{post['slug']}/",
                description=blog.get("meta_description", ""), jsonld=post_jsonld(cfg, post, sources, faq))


def render_index(cfg: dict, posts: list[dict]) -> str:
    esc = html.escape
    items = "".join(
        f'<li><a href="/{p["slug"]}/">{esc(p["draft"]["blog"].get("title", ""))}</a>'
        + (f' <span class="badge">{sponsors.label(p["draft"]["sponsor"])} · Ad</span>' if p["draft"].get("sponsor") else "")
        + f'<br><span class="meta">{esc(p["date"][:10])} · {esc(p["draft"]["blog"].get("meta_description", ""))}</span></li>'
        for p in posts[: cfg.get("posts_on_home", 30)]) or "<li>First guides are coming soon.</li>"
    base = base_url(cfg)
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": cfg["name"], "description": cfg["description"],
               **({"url": base + "/"} if base else {})}
    return page(cfg, f"{cfg['name']} — {cfg['tagline']}", f"<p>{esc(cfg['description'])}</p><ul class=\"posts\">{items}</ul>",
                path="/", jsonld=[site_ld])


def render_about(cfg: dict) -> str:
    name = html.escape(cfg["name"])
    body = f"""<h1>About {name}</h1>
<p>{html.escape(cfg['description'])}</p>
<h2>How our guides are made</h2>
<ol><li>Topics come from what international visitors search for and ask about.</li>
<li>An AI model gathers facts, and <strong>every fact must cite a source</strong> — peer-reviewed research, government and
professional-society pages first. Clinic websites are never used as sources for medical claims.</li>
<li>The draft is checked automatically against Korean medical-advertising rules, every source page is fetched and compared
with the claim, and a second AI model from a different company reviews it.</li>
<li>A person on our team reads and approves every post before it is published.</li></ol>
<p>Our guides are general information, not medical advice, and are not written or reviewed by a doctor. Results and
side effects vary from person to person — always consult a licensed doctor.</p>
<h2>What we never do</h2>
<ul><li>No clinic rankings, "best clinic" lists, patient testimonials or before-and-after photos.</li>
<li>No clinic recommendations or booking links in our independent guides, and no payment from clinics for those guides.</li>
<li>Route guides may end with a list of every clinic with a dermatology department near the route, taken from Korean
government public data and sorted by distance only. Clinics cannot pay to be added, removed or moved in that list;
advertisers are marked.</li>
<li>Some route guides show one small card labeled <em>Ad</em> for a clinic in the same area. It is a flat-fee advertisement,
not a recommendation, and it does not change the guide's text or the clinic list.</li></ul>
<h2>Sponsored posts</h2>
<p>Some posts are advertisements paid for by a licensed clinic at a flat fee, or produced free of charge during a short
partner pilot. They are always labeled <em>Sponsored</em> or <em>Partner</em> and as an advertisement at the top, the clinic is the advertiser and approves the final text, links to the clinic are marked as
sponsored, and the same accuracy and advertising rules apply. Clinics cannot pay to appear in, or influence, the text of our independent guides.</p>"""
    return page(cfg, f"About — {cfg['name']}", body, path="/about/")


def render_privacy(cfg: dict) -> str:
    contact = html.escape(cfg.get("contact_email") or "the contact address on this site")
    body = f"""<h1>Privacy</h1>
<p>This site does not use advertising cookies or ask you to log in.</p>
<ul><li><strong>Hosting logs:</strong> our host (Cloudflare) processes standard request data such as IP address and browser
type to deliver and protect the site.</li>
<li><strong>Visit statistics:</strong> we use Cloudflare Web Analytics, which counts page views and referring sites without
cookies or tracking you across sites.</li>
<li><strong>Outbound link counts:</strong> some outbound links (clinic websites in sponsored posts and the official clinic registry)
go through our link counter. It records
the date, which post and channel the click came from, the country, and whether it looks automated. We do not store your IP
address; a one-way code that changes every day is used only to count unique visits per day. The clinic's page receives
standard campaign tags (utm_source, utm_medium, utm_campaign) so the clinic can see the visit came from us.</li>
<li><strong>Ad display:</strong> to show clinic ads only to visitors outside Korea, the page asks our host (Cloudflare)
for your country code. Nothing is stored.</li>
<li>We do not sell personal information.</li></ul>
<p>Questions: {contact}.</p>"""
    return page(cfg, f"Privacy — {cfg['name']}", body, path="/privacy/")


def render_llms_txt(cfg: dict, posts: list[dict]) -> str:
    base = base_url(cfg)
    lines = [f"# {cfg['name']}", "", f"> {cfg['description']}", "",
             "Every fact in our guides links to its source. Independent guides never name or rank clinics; "
             "sponsored posts are labeled advertisements. Information only — not medical advice.", "", "## Guides"]
    for p in posts:
        blog = p["draft"]["blog"]
        tag = " (sponsored)" if p["draft"].get("sponsor") else ""
        lines.append(f"- [{blog.get('title', '')}]({base}/{p['slug']}/){tag}: {blog.get('meta_description', '')}")
    lines += ["", "## About", f"- [Editorial policy]({base}/about/)"]
    return "\n".join(lines) + "\n"


def render_sitemap(cfg: dict, posts: list[dict]) -> str:
    base = base_url(cfg)
    urls = [("/", posts[0]["date"] if posts else ""), ("/about/", ""), ("/privacy/", "")]
    urls += [(f"/{p['slug']}/", p["date"]) for p in posts]
    items = "".join(f"<url><loc>{base}{u}</loc>" + (f"<lastmod>{d[:10]}</lastmod>" if d else "") + "</url>" for u, d in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>\n'


def render_rss(cfg: dict, posts: list[dict]) -> str:
    base = base_url(cfg)
    esc = html.escape
    items = "".join(
        f"<item><title>{esc(p['draft']['blog'].get('title', ''))}</title><link>{base}/{p['slug']}/</link>"
        f"<guid>{base}/{p['slug']}/</guid><description>{esc(p['draft']['blog'].get('meta_description', ''))}</description>"
        f"<pubDate>{esc(_rfc822(p['date']))}</pubDate></item>" for p in posts[:50])
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(cfg["name"])}</title>'
            f"<link>{base}/</link><description>{esc(cfg['description'])}</description>{items}</channel></rss>\n")


def _rfc822(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    except ValueError:
        return ""


def render_robots(cfg: dict) -> str:
    # AI 검색·학습 크롤러를 막지 않는다 — AI 답변에 인용되는 것이 이 사이트의 목적
    sitemap = f"\nSitemap: {base_url(cfg)}/sitemap.xml" if cfg["domain"] else ""
    return f"User-agent: *\nAllow: /\n{sitemap}\n"


# ---------------- 스폰서 추적 링크 ----------------

def link_meta(post: dict, kind: str) -> dict:
    """추적 링크 → 글 메타데이터 (pipeline.analytics 가 시술·축별로 묶을 때 쓴다)."""
    draft = post["draft"]
    sponsor = draft.get("sponsor") or {}
    return {"kind": kind, "draft_id": draft["id"], "slug": post["slug"], "date": post["date"][:10], "title": draft["blog"].get("title", ""),
            "axis": draft.get("content_type", ""), "keywords": (draft.get("topic") or {}).get("keywords", []),
            "sponsor_id": sponsor.get("id"), "contract": (sponsor.get("contract") or {}).get("type")}


def route_ad_key(draft_id: str, sponsor_id: str) -> str:
    return f"_routead:{draft_id}:{sponsor_id}"


def registry_key(draft_id: str) -> str:
    return f"{REGISTRY_KEY}:{draft_id}"


def load_tracked_links() -> dict[str, dict]:
    """content/site/tracked_links.json — {키: {url, code, kind, draft_id, title, axis, keywords, ...}}.
    키: 스폰서 글은 draft_id, 중립 글의 등록기관 링크는 "_registry:<draft_id>". 예전 형식(키 → URL 문자열)도 읽는다."""
    path = content_dir() / "site" / "tracked_links.json"
    raw = load_json(path) if path.exists() else {}
    return {k: (v if isinstance(v, dict) else {"url": v}) for k, v in raw.items()}


def tracked_links(posts: list[dict]) -> dict[str, dict]:
    """글별 추적 링크 (?s=blog): 스폰서 글 → 병원 공식 사이트, 중립 시술·여행 글 → 등록기관 목록 (글마다 따로 세서
    어떤 글·시술이 병원 찾기로 이어졌는지 본다). 추적기 미설정이면 기존 목록만 — 링크는 원래 주소로 나간다."""
    mapping = load_tracked_links()
    if not tracker.configured():
        return mapping
    changed = False
    for p in posts:
        draft = p["draft"]
        title = draft["blog"].get("title", "")
        if draft.get("sponsor"):
            key, kind, sponsor_id, target, label = draft["id"], "sponsor", draft["sponsor"]["id"], None, f"blog: {title}"
        elif draft.get("content_type") in REGISTRY_AXES:
            key, kind, sponsor_id, target, label = registry_key(draft["id"]), "registry", None, REGISTRY_URL, f"registry: {title}"
        else:
            continue
        ad = route_ad_sponsor(p)
        if ad and route_ad_key(draft["id"], ad["id"]) not in mapping:  # 코스 광고 카드 → 광고주 공식 사이트 (글·광고주별로 센다)
            link = tracker.add_link(ad["id"], None, f"route ad: {title}"[:120])
            mapping[route_ad_key(draft["id"], ad["id"])] = {
                "url": f"{link['url']}?s=blog", "code": link["code"], **link_meta(p, "sponsor"),
                "sponsor_id": ad["id"], "contract": ad["contract"]["type"], "placement": "route_ad"}
            changed = True
        if key in mapping:
            if "kind" not in mapping[key]:  # 예전 형식 → 메타데이터 보강 (링크는 그대로)
                mapping[key] = {**mapping[key], **link_meta(p, kind)}
                changed = True
            continue
        link = tracker.add_link(sponsor_id, target, label[:120])
        mapping[key] = {"url": f"{link['url']}?s=blog", "code": link["code"], **link_meta(p, kind)}
        changed = True
    if changed:
        save_json(content_dir() / "site" / "tracked_links.json", mapping)
    return mapping


# ---------------- 빌드·배포 ----------------

def build(out: Path | None = None) -> dict:
    cfg = config()
    out = out or dist_dir()
    posts = collect_posts()
    links = tracked_links(posts)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    def write(rel: str, text: str) -> None:
        f = out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    for p in posts:
        own = links.get(p["draft"]["id"], {}).get("url")
        registry = (links.get(registry_key(p["draft"]["id"])) or links.get(REGISTRY_KEY) or {}).get("url")
        ad = route_ad_sponsor(p)
        ad_url = (links.get(route_ad_key(p["draft"]["id"], ad["id"])) or {}).get("url") if ad else None
        write(f"{p['slug']}/index.html", render_post(cfg, p, own, registry, ad_url))
    write("index.html", render_index(cfg, posts))
    write("about/index.html", render_about(cfg))
    write("privacy/index.html", render_privacy(cfg))
    write("404.html", page(cfg, f"Not found — {cfg['name']}", '<h1>Page not found</h1><p><a href="/">Back to all guides</a></p>',
                           path="/404", noindex=True))
    write("robots.txt", render_robots(cfg))
    write("llms.txt", render_llms_txt(cfg, posts))
    write("rss.xml", render_rss(cfg, posts))
    if cfg["domain"]:
        write("sitemap.xml", render_sitemap(cfg, posts))
    else:
        log.warning("config/site.yaml domain 이 비어 있어 sitemap·canonical 을 만들지 않았습니다")
    digest = hashlib.sha256()
    for f in sorted(out.rglob("*")):
        if f.is_file():
            digest.update(str(f.relative_to(out)).encode() + f.read_bytes())
    return {"posts": len(posts), "sponsored": sum(1 for p in posts if p["draft"].get("sponsor")),
            "slugs": [p["slug"] for p in posts], "hash": digest.hexdigest()}


def deploy() -> str:
    """바뀐 게 있을 때만 배포하고 텔레그램용 요약을 돌려준다 (없으면 빈 문자열)."""
    result = build()
    state = load_json(state_file()) if state_file().exists() else {}
    if state.get("hash") == result["hash"]:
        return ""
    cfg = config()
    cmd = ["npx", "--yes", "wrangler", "pages", "deploy", str(dist_dir()), "--project-name", cfg["cloudflare_project"],
           "--branch", "main", "--commit-dirty=true"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"wrangler pages deploy 실패: {(proc.stderr or proc.stdout)[-500:]}")
    new = [s for s in result["slugs"] if s not in state.get("slugs", [])]
    save_json(state_file(), {"hash": result["hash"], "slugs": result["slugs"], "deployed_at": datetime.now(timezone.utc).isoformat()})
    base = base_url(cfg) or f"https://{cfg['cloudflare_project']}.pages.dev"
    lines = [f"🌐 블로그 업데이트: 글 {result['posts']}개 (스폰서 {result['sponsored']})"]
    lines += [f"- 새 글: {base}/{s}/" for s in new]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="블로그 정적 사이트")
    parser.add_argument("cmd", choices=["build", "deploy"])
    args = parser.parse_args(argv)
    if args.cmd == "build":
        r = build()
        print(f"{dist_dir()}: 글 {r['posts']}개 (스폰서 {r['sponsored']})")
        return 0
    message = deploy()
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    run_cli("site", main)
