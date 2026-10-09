"""블로그 정적 사이트 생성·배포 — 사람이 승인한 초안의 블로그 글을 AI·검색이 읽기 좋은 사이트로 만든다.

게시 대상: approved/ 이후 상태(approved, rendered, ready_to_publish, published)의 초안 — 사람 검수를 통과한 글만.
(06_publish 의 ready_to_publish/ 제약은 영상 게시에 적용. 블로그는 텍스트라 초안 승인 = 게시 승인.)

    python -m pipeline.site build     # site/dist 생성
    python -m pipeline.site deploy    # build 후 바뀐 게 있으면 Cloudflare Workers(정적 자산)에 배포 (.env CLOUDFLARE_API_TOKEN·ACCOUNT_ID)

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

from pipeline import clinics, og, site_images, sponsors, tracker
from pipeline.common import (PRACTICE_KIND, ROOT, content_dir, draft_dirs, get_logger, is_medical_fact, load_draft, load_json,
                             load_yaml, run_cli, save_json, slugify)

log = get_logger("site")

PUBLIC_STATES = ("approved", "rendered", "ready_to_publish", "published")
# 중립 글의 "병원 찾기" 안내 — 개별 병원 대신 정부 공식 명단. 클릭 수 = 병원을 찾으러 나간 독자 수 (영업 근거)
REGISTRY_URL = "https://www.medicalkorea.or.kr/en/registeredhospitals"
REGISTRY_AXES = {"procedure", "price_guide", "how_to", "procedure_travel", "travel_guide", "review_curation", "trend"}
REGISTRY_KEY = "_registry"
CLINIC_LIST_AXES = {"procedure_travel", "travel_guide"}  # 코스 주변 피부과 목록을 붙이는 글 (중립 여행 글만)
CLINIC_KEY = "_clinic"  # 주변 병원 목록의 병원별 사이트 링크 (tracked_links.json 키: _clinic:<draft_id>:<병원 id>)
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
    """[F#] → 출처 번호. 같은 URL 은 한 번호로 묶는다. 의료 정보는 번호가 출처 페이지로 바로 연결된다 (출처 도메인 표시)."""
    by_id = {f["id"]: f for f in facts}
    sources: list[dict] = []
    number_of_url: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        fact = by_id.get(m.group(1))
        if not fact:
            return ""
        if fact.get("kind") == PRACTICE_KIND:  # 여러 병원 공통 정보 — 특정 병원을 링크·출처 목록에 올리지 않는다
            return ('<sup class="ref" title="Based on what several Seoul clinic websites describe in common">'
                    '[clinic websites]</sup>')
        if fact["url"] not in number_of_url:
            sources.append({"url": fact["url"], "title": fact.get("source_title") or hostname(fact["url"])})
            number_of_url[fact["url"]] = len(sources)
        n = number_of_url[fact["url"]]
        if not is_medical_fact(fact):  # 여행 정보는 글 아래 출처 목록으로
            return f'<sup class="ref"><a href="#src-{n}">[{n}]</a></sup>'
        url = html.escape(fact["url"], quote=True)
        title = html.escape(f"Source: {fact.get('source_title') or hostname(fact['url'])}", quote=True)
        # rel·target 은 _rewrite_links 가 붙인다 (스폰서 병원 사이트면 rel=sponsored)
        return f'<sup class="ref"><a href="{url}" title="{title}">[{n} · {html.escape(hostname(fact["url"]))}]</a></sup>'

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

# 디자인 (2026-10-10 개편): 크림 배경·짙은 녹색, 제목 세리프·본문 산세리프, 넓은 여백, 글마다 큰 사진.
# 글꼴은 기기 기본 글꼴만 (외부 글꼴 서버 = 방문자 정보 전송 → 개인정보 페이지의 "추적 없음"과 맞지 않아 쓰지 않는다)
CSS = """
:root{--bg:#f7f3ec;--surface:#fff;--fg:#1c2622;--muted:#66706b;--line:#e6dfd3;--accent:#0b6e4f;--accent-soft:#e3efe8;
--ad:#8a5a00;--adbg:#fff6e0;--serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--radius:14px}
@media (prefers-color-scheme:dark){:root{--bg:#121614;--surface:#1a201d;--fg:#ecefea;--muted:#a3ada7;--line:#2b332f;
--accent:#5cc99f;--accent-soft:#1f3229;--ad:#f3c969;--adbg:#2b2413}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.7 var(--sans)}
a{color:var(--accent)}img{max-width:100%;display:block}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px}
.site-head{border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--bg) 88%,transparent);position:sticky;top:0;z-index:5;
backdrop-filter:blur(8px)}
.site-head .wrap{display:flex;align-items:center;justify-content:space-between;height:64px}
.logo{font-family:var(--serif);font-size:1.45em;font-weight:700;color:var(--fg);text-decoration:none;letter-spacing:.01em}
.logo span{color:var(--accent)}
.nav a{color:var(--fg);text-decoration:none;font-size:.92em;margin-left:22px;opacity:.85}.nav a:hover{opacity:1;color:var(--accent)}
h1,h2,h3{font-family:var(--serif);line-height:1.22;font-weight:700}
.hero{position:relative;margin:28px auto 0;border-radius:var(--radius);overflow:hidden;min-height:380px;display:flex;align-items:flex-end;
background:linear-gradient(135deg,#0b6e4f,#1c3a30)}
.hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(to top,rgba(10,20,16,.86),rgba(10,20,16,.45) 60%,rgba(10,20,16,.25))}
.hero-text{text-shadow:0 1px 12px rgba(0,0,0,.35)}
.hero-text{position:relative;z-index:1;padding:40px;color:#fff;max-width:760px}
.hero-text h1{font-size:2.6em;margin:.1em 0 .3em}.hero-text p{font-size:1.08em;opacity:.92;margin:0}
.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.72em;font-weight:700;color:var(--accent)}
.hero .eyebrow{color:#d8f6e8}
.trust{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:22px 0 8px}
.trust div{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;font-size:.9em;color:var(--muted)}
.trust strong{display:block;color:var(--fg);font-size:1.02em;margin-bottom:2px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:26px 0 6px}
.chips a{border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:6px 14px;font-size:.88em;text-decoration:none;color:var(--fg)}
.chips a:hover,.chips a.on{border-color:var(--accent);color:var(--accent)}
.section-title{display:flex;align-items:baseline;justify-content:space-between;margin:34px 0 14px}
.section-title h2{margin:0;font-size:1.6em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:22px;padding:0;margin:0;list-style:none}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;display:flex;flex-direction:column;
transition:transform .15s,box-shadow .15s}.card:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(0,0,0,.08)}
.card a{text-decoration:none;color:inherit;display:flex;flex-direction:column;height:100%}
.thumb{aspect-ratio:16/10;background:linear-gradient(135deg,var(--accent-soft),var(--line));overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover}
.card-body{padding:16px 18px 18px;display:flex;flex-direction:column;gap:6px;flex:1}
.card h3{margin:0;font-size:1.22em}.card p{margin:0;color:var(--muted);font-size:.92em;line-height:1.55}
.card .meta{margin-top:auto;padding-top:8px}
.meta{color:var(--muted);font-size:.86em}
.badge{display:inline-block;background:var(--adbg);color:var(--ad);border:1px solid var(--ad);border-radius:5px;padding:0 7px;font-size:.78em;font-weight:700}
.pill{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:2px 10px;font-size:.8em;font-weight:600}
.post-head{max-width:760px;margin:40px auto 0;padding:0 20px}
.post-head h1{font-size:2.5em;margin:.25em 0 .35em}.post-head .lede{font-size:1.15em;color:var(--muted);margin:0 0 16px}
.post-meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:.86em;color:var(--muted)}
.post-photo{max-width:1120px;margin:26px auto 0;padding:0 20px}
.post-photo img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:var(--radius)}
.post-photo figcaption{font-size:.75em;color:var(--muted);margin-top:6px;text-align:right}
.post-photo figcaption a{color:var(--muted)}
article.post{max-width:720px;margin:0 auto;padding:10px 20px 0}
article.post h2{font-size:1.6em;margin:1.8em 0 .5em;scroll-margin-top:80px}article.post h3{font-size:1.2em;margin:1.4em 0 .4em}
article.post p,article.post li{font-size:1.04em}
.toc{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 20px;margin:26px 0 8px}
.toc strong{font-size:.78em;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.toc ol{margin:.4em 0 0;padding-left:1.2em}.toc li{font-size:.95em;margin:.2em 0}.toc a{text-decoration:none}
blockquote{margin:1.2em 0;padding:.8em 1.1em;border-left:4px solid var(--accent);background:var(--accent-soft);border-radius:0 10px 10px 0}
sup.ref a{text-decoration:none;font-size:.78em;background:var(--accent-soft);border-radius:6px;padding:0 5px;margin-left:2px}
.note{margin:2em 0;padding:16px 18px;border-radius:12px;background:var(--surface);border:1px solid var(--line);font-size:.92em;color:var(--muted)}
.note strong{color:var(--fg)}
.box{margin:1.6em 0;padding:1em 1.2em;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
.box h2{margin:.2em 0 .5em;font-size:1.3em}
.clinics ol{font-size:.94em;padding-left:1.4em}
.adcard{margin:1.2em 0;padding:.7em 1em;border:1px dashed var(--line);border-radius:10px;font-size:.9em;background:var(--surface)}
.sources{font-size:.9em;border-top:1px solid var(--line);margin-top:2.4em;padding-top:.4em}.sources li{word-break:break-word;margin:.25em 0}
.sources h2{font-size:1.3em}
.page{max-width:720px;margin:40px auto 0;padding:0 20px}
.site-foot{border-top:1px solid var(--line);margin-top:70px;padding:34px 0 48px;color:var(--muted);font-size:.86em}
.site-foot .wrap{display:grid;grid-template-columns:2fr 1fr;gap:24px}.site-foot .logo{font-size:1.2em}
.site-foot a{color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:.95em}td,th{border:1px solid var(--line);padding:6px 10px;text-align:left}
@media (max-width:720px){body{font-size:16px}.hero{min-height:300px;margin-top:16px}.hero-text{padding:24px}.hero-text h1{font-size:1.9em}
.trust{grid-template-columns:1fr}.post-head h1{font-size:1.9em}.nav a{margin-left:14px}.site-foot .wrap{grid-template-columns:1fr}}
"""


def page(cfg: dict, title: str, body: str, *, path: str, description: str = "", jsonld: list[dict] | None = None,
         noindex: bool = False, image: str | None = None) -> str:
    base = base_url(cfg)
    esc = html.escape
    image = image or ("/og/default.png" if cfg.get("_og_images") else None)
    head = [
        '<meta charset="utf-8">', '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{esc(title)}</title>", f'<meta name="description" content="{esc(description or cfg["description"])}">',
        f'<meta property="og:title" content="{esc(title)}">', f'<meta property="og:site_name" content="{esc(cfg["name"])}">',
        f'<meta property="og:description" content="{esc(description or cfg["description"])}">',
        f'<meta property="og:type" content="{"article" if path.count("/") == 2 and path not in ("/about/", "/privacy/") else "website"}">',
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/apple-touch-icon.png">',
        f'<link rel="alternate" type="application/rss+xml" title="{esc(cfg["name"])}" href="/rss.xml">',
        f"<style>{CSS}</style>",
    ]
    if image:  # 링크 미리보기 카드 (SNS·메신저·검색)
        src = f"{base}{image}" if base else image
        head += [f'<meta property="og:image" content="{esc(src)}">', '<meta property="og:image:width" content="1200">',
                 '<meta property="og:image:height" content="630">', '<meta name="twitter:card" content="summary_large_image">']
    if base:
        head.append(f'<meta property="og:url" content="{base}{path}">')
    if base:
        head.append(f'<link rel="canonical" href="{base}{path}">')
    if cfg.get("analytics_token"):  # Cloudflare Web Analytics — 쿠키 없음, 방문·유입 경로(AI 검색 포함) 집계
        beacon = html.escape(json.dumps({"token": cfg["analytics_token"]}))
        head.append(f'<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon="{beacon}"></script>')
    if noindex:
        head.append('<meta name="robots" content="noindex">')
    for data in jsonld or []:
        head.append('<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    contact = f' · <a href="mailto:{esc(cfg["contact_email"])}">{esc(cfg["contact_email"])}</a>' if cfg.get("contact_email") else ""
    name = esc(cfg["name"])
    return f"""<!doctype html>
<html lang="{cfg.get('language', 'en')}"><head>{''.join(head)}</head>
<body><header class="site-head"><div class="wrap"><a class="logo" href="/">{name}<span>.</span></a>
<nav class="nav"><a href="/#guides">Guides</a><a href="/about/">How we work</a></nav></div></header>
<main>{body}</main>
<footer class="site-foot"><div class="wrap"><div><a class="logo" href="/">{name}<span>.</span></a>
<p>{esc(cfg['tagline'])}. AI-assisted, source-backed information — not medical advice. Always consult a licensed doctor.
Sponsored posts are clearly labeled advertisements.</p></div>
<div><p><a href="/about/">About &amp; editorial policy</a><br><a href="/privacy/">Privacy</a><br><a href="/rss.xml">RSS</a>{contact.replace(' · ', '<br>')}</p></div></div></footer>
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


def _post_text(draft: dict) -> str:
    return " ".join([draft["blog"].get("title", ""), " ".join((draft.get("topic") or {}).get("keywords", [])),
                     draft["blog"].get("markdown", "")])


def clinic_link_key(draft_id: str, cid: str) -> str:
    return f"{CLINIC_KEY}:{draft_id}:{cid}"


def clinic_directory_html(post: dict, clinic_urls: dict[str, str] | None = None) -> str:
    """여행 글 코스 주변 피부과 전부 (심평원 공공데이터, 거리순, 추천·순위 없음, 광고주 표시). pipeline/clinics.py
    clinic_urls: 병원 id → 추적 링크. 없으면 병원 사이트로 바로 — 어느 쪽이든 모든 병원에 같은 'website' 링크."""
    stops = clinics.directory(_post_text(post["draft"]))
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
            site = (clinic_urls or {}).get(clinics.clinic_id(c)) or clinics.website(c)
            site_link = f' · <a href="{esc(site)}" rel="nofollow noopener" target="_blank">website</a>' if site else ""
            rows.append(f'<li>{esc(c["name"])}{tag} <span class="meta">{esc(c["type"])} · {esc(c["district"])} · '
                        f'{c["distance_m"]} m · <a href="{esc(maps)}" rel="nofollow noopener" target="_blank">map</a>'
                        f'{site_link}</span></li>')
        shown = len(stop["clinics"])
        count = (f"all {stop['total']}" if shown == stop["total"] else f"the {shown} closest of {stop['total']}")
        parts.append(f'<details><summary>Near {esc(stop["name"])}: {count} clinics offering dermatology within '
                     f'{stop["radius_m"]} m</summary><ol>{"".join(rows)}</ol></details>')
    fetched = max(s["fetched_at"] for s in stops)
    return (f'<section class="box clinics"><h2>Dermatology clinics near this route</h2>'
            f'<p class="meta">Every clinic listed in the Korean government\'s public health-insurance facility data (HIRA) '
            f'with a dermatology department within the radius, sorted by distance only (as of {esc(fetched)}). '
            f'We do not recommend, rank or review clinics; names are shown in Korean as registered. '
            f'Website links are the addresses listed in the same public data, shown for every clinic that has one. '
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
                ad_url: str | None = None, clinic_urls: dict[str, str] | None = None) -> str:
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
    image = post.get("image")
    photo = ""
    if image:
        credit = esc(image.get("credit", ""))
        if credit and image.get("credit_url"):
            credit = f'<a href="{esc(image["credit_url"])}" rel="noopener" target="_blank">{credit}</a>'
        photo = (f'<figure class="post-photo"><img src="{esc(image["hero"])}" alt="" width="1600" height="900" '
                 f'fetchpriority="high">{f"<figcaption>{credit}</figcaption>" if credit else ""}</figure>')
    n_src = len(sources) + (1 if "[clinic websites]" in body_html else 0)
    body = f"""<header class="post-head"><span class="eyebrow">{esc(category_label(draft))}</span>
<h1>{esc(blog.get('title', ''))}</h1>
{f'<p class="lede">{esc(blog["meta_description"])}</p>' if blog.get("meta_description") else ""}
<div class="post-meta">{badge}<span class="pill">{n_src} source{"s" if n_src != 1 else ""} cited</span>
<span class="pill">Human-approved</span><span>Updated {esc(post['date'][:10])} · {esc(cfg['byline'])}</span></div></header>
{photo}
<article class="post">{toc_html(body_html)}
{body_html}
<aside class="note"><strong>Not medical advice.</strong> This guide summarizes public sources; every medical statement links to
its source. Results and side effects vary — please consult a licensed doctor before any treatment.</aside>
{registry_box(registry_url) if not sponsor and draft.get("content_type") in REGISTRY_AXES else ""}
{clinic_directory_html(post, clinic_urls) if not sponsor and draft.get("content_type") in CLINIC_LIST_AXES else ""}
{route_ad_html(cfg, ad_sponsor, ad_url)}
<section class="sources"><h2>Sources</h2><ol>{src_items}</ol></section></article>{geo_ad_script(cfg) if ad_sponsor else ""}"""
    faq = extract_faq(strip_leading_h1(blog["markdown"]))
    return page(cfg, f"{blog.get('title', '')} | {cfg['name']}", body, path=f"/{post['slug']}/",
                description=blog.get("meta_description", ""), jsonld=post_jsonld(cfg, post, sources, faq),
                image=f"/og/{post['slug']}.png" if cfg.get("_og_images") else None)


CATEGORY_LABELS = {
    "procedure": "Procedures", "price_guide": "Prices", "how_to": "How-to", "trend": "Trends",
    "review_curation": "What visitors ask", "procedure_travel": "Treatment × travel", "travel_guide": "Travel",
    "skincare": "Skincare",
}


def category_key(draft: dict) -> str:
    if draft.get("sponsor"):
        return "partner"
    key = draft.get("content_type") or (draft.get("topic") or {}).get("axis", "")
    return key if key in CATEGORY_LABELS else "procedure"


def category_label(draft: dict) -> str:
    return "Partner content" if draft.get("sponsor") else CATEGORY_LABELS[category_key(draft)]


H2_WITH_ID = re.compile(r'<h2 id="([^"]+)">(.*?)</h2>', re.S)


def toc_html(body_html: str) -> str:
    """본문 소제목(h2) 목차 — 3개 이상일 때만."""
    heads = H2_WITH_ID.findall(body_html)
    if len(heads) < 3:
        return ""
    items = "".join(f'<li><a href="#{i}">{re.sub(r"<[^>]+>", "", t)}</a></li>' for i, t in heads)
    return f'<nav class="toc" aria-label="Contents"><strong>In this guide</strong><ol>{items}</ol></nav>'


def card_html(p: dict) -> str:
    esc = html.escape
    draft = p["draft"]
    img = (p.get("image") or {}).get("card")
    thumb = f'<img src="{esc(img)}" alt="" width="720" height="450" loading="lazy">' if img else ""
    ad = f' <span class="badge">{sponsors.label(draft["sponsor"])} · Ad</span>' if draft.get("sponsor") else ""
    return (f'<li class="card"><a href="/{p["slug"]}/"><div class="thumb">{thumb}</div><div class="card-body">'
            f'<span class="eyebrow">{esc(category_label(draft))}</span>{ad}'
            f'<h3>{esc(draft["blog"].get("title", ""))}</h3><p>{esc(draft["blog"].get("meta_description", ""))}</p>'
            f'<span class="meta">{esc(p["date"][:10])}</span></div></a></li>')


def render_index(cfg: dict, posts: list[dict]) -> str:
    esc = html.escape
    shown = posts[: cfg.get("posts_on_home", 30)]
    items = "".join(card_html(p) for p in shown) or '<li class="card"><div class="card-body"><p>First guides are coming soon.</p></div></li>'
    lead_img = next(((p.get("image") or {}).get("hero") for p in shown if p.get("image")), None)
    hero_img = f'<img src="{esc(lead_img)}" alt="" width="1600" height="900" fetchpriority="high">' if lead_img else ""
    cats = []
    for p in posts:
        k = category_key(p["draft"])
        if k != "partner" and k not in cats:
            cats.append(k)
    chips = "".join(f'<a href="#guides">{esc(CATEGORY_LABELS[k])}</a>' for k in cats)
    base = base_url(cfg)
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": cfg["name"], "description": cfg["description"],
               **({"url": base + "/"} if base else {})}
    body = f"""<div class="wrap"><section class="hero">{hero_img}<div class="hero-text"><span class="eyebrow">Korea skin treatments, explained</span>
<h1>{esc(cfg['tagline'])}</h1><p>{esc(cfg['description'])}</p></div></section>
<div class="trust"><div><strong>Every fact cited</strong>Medical statements link straight to research, government or society pages.</div>
<div><strong>Checked twice</strong>Each source page is re-read and a second AI from another company reviews the draft.</div>
<div><strong>Human-approved</strong>A person reads every guide before it goes live. No clinic rankings or testimonials.</div></div>
{f'<nav class="chips" aria-label="Topics">{chips}</nav>' if len(cats) > 1 else ""}
<div class="section-title" id="guides"><h2>Latest guides</h2><span class="meta">{len(posts)} guide{"s" if len(posts) != 1 else ""}</span></div>
<ul class="grid">{items}</ul></div>"""
    return page(cfg, f"{cfg['name']} — {cfg['tagline']}", body, path="/", jsonld=[site_ld],
                image=None)


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
    return page(cfg, f"About — {cfg['name']}", f'<div class="page">{body}</div>', path="/about/")


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
    return page(cfg, f"Privacy — {cfg['name']}", f'<div class="page">{body}</div>', path="/privacy/")


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


def _clinic_links(post: dict, mapping: dict[str, dict], title: str) -> bool:
    """주변 병원 목록의 병원 사이트마다 추적 링크 (https 사이트만 — 추적기가 https 만 받는다, 나머지는 바로 연결).
    병원별로 기록하지만 리포트·영업 자료에는 지역·시술 단위 합산으로만 쓴다 (pipeline.analytics)."""
    changed = False
    draft = post["draft"]
    for stop in clinics.directory(_post_text(draft)):
        for c in stop["clinics"]:
            site = clinics.website(c)
            key = clinic_link_key(draft["id"], clinics.clinic_id(c))
            if not site.startswith("https://") or key in mapping:
                continue
            try:
                link = tracker.add_link(None, site, f"clinic list: {title}"[:120])
            except tracker.TrackerError as e:
                log.warning("병원 목록 링크 추적 생성 실패 (바로 연결): %s — %s", c["name"], e)
                continue
            mapping[key] = {"url": f"{link['url']}?s=blog", "code": link["code"], **link_meta(post, "clinic"),
                            "placement": "clinic_dir", "attraction": stop["attraction"], "clinic": c["name"]}
            changed = True
    return changed


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
        if not draft.get("sponsor") and draft.get("content_type") in CLINIC_LIST_AXES:
            changed |= _clinic_links(p, mapping, title)
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

    og.icons(out)
    for slug, image in site_images.build_images(posts, out).items():  # 글마다 대표 사진 (없으면 브랜드 색)
        next(p for p in posts if p["slug"] == slug)["image"] = image
    if og.available():  # 링크 미리보기 카드 이미지 (글마다 + 기본)
        cfg = {**cfg, "_og_images": True}
        og.card(cfg["tagline"], cfg["name"], cfg.get("domain") or "", out / "og" / "default.png")
        for p in posts:
            s = p["draft"].get("sponsor")
            og.card(p["draft"]["blog"].get("title", ""), cfg["name"], cfg.get("domain") or cfg["tagline"],
                    out / "og" / f"{p['slug']}.png", ad_label=f"{sponsors.label(s)} · Ad" if s else "")
    else:
        log.warning("Pillow 가 없어 공유 이미지(og:image)를 만들지 않았습니다 (pip install -r requirements.txt)")

    for p in posts:
        own = links.get(p["draft"]["id"], {}).get("url")
        registry = (links.get(registry_key(p["draft"]["id"])) or links.get(REGISTRY_KEY) or {}).get("url")
        ad = route_ad_sponsor(p)
        ad_url = (links.get(route_ad_key(p["draft"]["id"], ad["id"])) or {}).get("url") if ad else None
        prefix = clinic_link_key(p["draft"]["id"], "")
        clinic_urls = {k[len(prefix):]: v["url"] for k, v in links.items() if k.startswith(prefix)}
        write(f"{p['slug']}/index.html", render_post(cfg, p, own, registry, ad_url, clinic_urls))
    write("index.html", render_index(cfg, posts))
    write("about/index.html", render_about(cfg))
    write("privacy/index.html", render_privacy(cfg))
    write("404.html", page(cfg, f"Not found — {cfg['name']}", '<div class="page"><h1>Page not found</h1><p><a href="/">Back to all guides</a></p></div>',
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


WORKER_COMPAT_DATE = "2026-10-01"


def deploy() -> str:
    """바뀐 게 있을 때만 배포하고 텔레그램용 요약을 돌려준다 (없으면 빈 문자열)."""
    result = build()
    state = load_json(state_file()) if state_file().exists() else {}
    if state.get("hash") == result["hash"]:
        return ""
    cfg = config()
    # Workers 정적 자산으로 배포 (2026-10: wrangler 4.x 는 Pages 배포를 Workers 로 넘기고 Pages 프로젝트 생성을 막는다).
    # 인증은 .env 의 CLOUDFLARE_API_TOKEN(권한: Workers Scripts Edit)·CLOUDFLARE_ACCOUNT_ID. 도메인은 대시보드에서 연결.
    # Windows 의 npx 는 npx.cmd 라 전체 경로로 넘겨야 실행된다
    cmd = [shutil.which("npx") or "npx", "--yes", "wrangler", "deploy", "--name", cfg["cloudflare_project"],
           "--assets", str(dist_dir()), "--compatibility-date", WORKER_COMPAT_DATE]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"wrangler deploy 실패: {(proc.stderr or proc.stdout)[-500:]}")
    new = [s for s in result["slugs"] if s not in state.get("slugs", [])]
    save_json(state_file(), {"hash": result["hash"], "slugs": result["slugs"], "deployed_at": datetime.now(timezone.utc).isoformat()})
    base = base_url(cfg) or f"https://{cfg['cloudflare_project']}.workers.dev"
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
