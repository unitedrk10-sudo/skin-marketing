"""한국 시술 검색 관심도 (2026-11-17) — 월간 순위 페이지 /trends/ + 글 안의 "지금 한국에서 이만큼 찾는다" 문장.

네이버 데이터랩 검색어 트렌드(NAVER API HUB)에서 지난달까지의 월별 상대값을 받아 코드가 순위를 낸다. LLM 은 쓰지 않는다 —
숫자·문장 모두 코드가 만든다 (그래서 검수 단계가 없고, 대신 문장 틀이 고정돼 있다).
  - 값은 실제 검색 횟수가 아니라 상대값이다. 한 요청에 주제어 5개까지만 비교되므로 기준 주제어(anchor)로 눈금을 맞춘다.
  - 공개하는 것: 순위·지수(1위 = 100)·전달 대비·12개월 추세·쓴 검색어. 원자료 내려받기(CSV)는 두지 않는다.
  - 지키는 선: 검색 관심은 시술 건수·효과·안전성이 아니라고 페이지와 글 안 문장에 밝힌다. 병원 이름·추천·"가장 좋은" 은 없다.
    스폰서 글에는 문장을 넣지 않고, 페이지가 잇는 글에도 스폰서 글은 없다 (광고와 편집 분리).
공개는 config/site.yaml trends_page: true 일 때만 (네이버 API 이용 조건 확인 + 첫 결과를 사람이 본 뒤).
환경변수: NAVER_CLIENT_ID · NAVER_CLIENT_SECRET (NAVER API HUB 애플리케이션 인증 정보, 검색어 트렌드 API 선택)

    python -m pipeline.naver_trends refresh   # 지난달 기준으로 받아 저장 → 텔레그램용 요약 (키 없으면 조용)
    python -m pipeline.naver_trends show      # 저장된 최신 달 요약
    python -m pipeline.naver_trends check     # 항목별 검색어·값 (뜻이 겹치는 검색어·0 인 항목 찾기)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta

from pipeline.common import content_dir, get_logger, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("naver_trends")

API = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
SOURCE_URL = "https://datalab.naver.com/keyword/trendSearch.naver"
GROUPS_PER_CALL = 5      # API 한도: 주제어 5개
MAX_TERMS = 20           # API 한도: 주제어마다 검색어 20개
TIMEOUT = 30
STALE_MONTHS = 2         # 이보다 오래된 달은 글 안 "지금" 문장에 쓰지 않는다 (페이지는 날짜가 붙어 있어 그대로 둔다)
MOVER_MIN_INDEX = 5      # 지수가 이보다 작은 항목의 증감률은 "많이 오른·내린 것" 에서 뺀다 (작은 수의 흔들림)
POST_LINES = 3           # 글 하나에 넣는 문장 수
GUIDE_LINKS = 2          # 순위 표에서 항목마다 잇는 우리 글 수
PATH = "trends"
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")


class TrendError(RuntimeError):
    pass


def settings() -> dict:
    cfg = load_yaml("naver_trends.yaml")
    items = cfg.get("items") or {}
    anchor = cfg.get("anchor")
    if anchor not in items:
        raise TrendError(f"naver_trends.yaml anchor '{anchor}' 가 items 에 없습니다")
    for key, item in items.items():
        terms = item.get("search") or []
        if not terms or len(terms) > MAX_TERMS:
            raise TrendError(f"naver_trends.yaml {key}: search 는 1~{MAX_TERMS}개")
    return {"anchor": anchor, "months": int(cfg.get("months") or 13), "items": items}


def configured() -> bool:
    return bool(os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET"))


def store_dir():
    return content_dir() / "site" / "naver_trends"


# ---------------- 받기 ----------------

def call(body: dict) -> dict:
    req = urllib.request.Request(API, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={
        "X-NCP-APIGW-API-KEY-ID": os.environ["NAVER_CLIENT_ID"], "X-NCP-APIGW-API-KEY": os.environ["NAVER_CLIENT_SECRET"],
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read(2_000_000))
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", errors="replace")
        hint = {401: "인증 정보 또는 애플리케이션의 검색어 트렌드 API 권한 확인", 429: "호출 한도 초과"}.get(e.code, "")
        raise TrendError(f"네이버 검색어 트렌드 API {e.code} {hint}: {detail[:200]}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TrendError(f"네이버 검색어 트렌드 API 연결 실패: {e}") from None


_caller = call


def set_caller(fn=None) -> None:
    """테스트용: API 호출 함수를 바꾼다 (None 이면 기본)."""
    global _caller
    _caller = fn or call


def month_list(today: date, count: int) -> list[str]:
    """지난달까지 count 개월 ("YYYY-MM", 오래된 달부터). 이번 달은 아직 안 끝나서 뺀다."""
    y, m = today.year, today.month
    out = []
    for _ in range(count):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y}-{m:02d}")
    return out[::-1]


def fetch(today: date | None = None) -> dict:
    """{"months": [...], "series": 항목 키 → 월별 값 (기준 주제어 눈금으로 맞춘 상대값, 값이 없는 달은 0)}."""
    cfg = settings()
    today = today or date.today()
    months = month_list(today, cfg["months"])
    end = today.replace(day=1) - timedelta(days=1)
    anchor, items = cfg["anchor"], cfg["items"]
    others = [k for k in items if k != anchor]
    step = GROUPS_PER_CALL - 1
    series: dict[str, list[float]] = {}
    base = 0.0
    for i in range(0, len(others), step):
        keys = [anchor, *others[i:i + step]]
        body = {"startDate": f"{months[0]}-01", "endDate": end.isoformat(), "timeUnit": "month",
                "keywordGroups": [{"groupName": k, "keywords": [str(t) for t in items[k]["search"]]} for k in keys]}
        got = {r.get("title"): {str(d.get("period", ""))[:7]: float(d.get("ratio") or 0) for d in r.get("data") or []}
               for r in (_caller(body).get("results") or [])}
        rows = {k: [got.get(k, {}).get(m, 0.0) for m in months] for k in keys}
        total = sum(rows[anchor])
        if total <= 0:
            raise TrendError(f"기준 주제어 '{anchor}' 값이 없습니다 — 검색어를 확인하세요")
        if not base:
            base = total
            series[anchor] = rows[anchor]
        scale = base / total  # 요청마다 100 의 기준이 달라 기준 주제어 합으로 눈금을 맞춘다
        for k in keys[1:]:
            series[k] = [v * scale for v in rows[k]]
    return {"months": months, "series": series}


# ---------------- 순위 ----------------

def _order(values: dict[str, float]) -> dict[str, int]:
    return {k: n for n, k in enumerate(sorted(values, key=lambda k: (-values[k], k)), 1)}


def _pct(now: float, before: float) -> float | None:
    return round((now - before) / before * 100, 1) if before > 0 else None


def rank(months: list[str], series: dict[str, list[float]]) -> dict:
    """지난달 기준 순위. index = 그 달 1위를 100 으로 본 값 (values 도 같은 눈금)."""
    cfg = settings()
    top = max(v[-1] for v in series.values())
    if top <= 0:
        raise TrendError("지난달 값이 모두 0 입니다")
    now = {k: v[-1] for k, v in series.items()}
    ranks = _order(now)
    prev = _order({k: v[-2] for k, v in series.items()}) if len(months) > 1 else {}
    items = []
    for key in sorted(series, key=ranks.get):
        meta, values = cfg["items"][key], series[key]
        items.append({"key": key, "name": meta["name"], "search": [str(t) for t in meta["search"]],
                      "match": [str(w).lower() for w in meta.get("match") or []], "procedure": meta.get("procedure") or "",
                      "rank": ranks[key], "prev_rank": prev.get(key), "index": round(values[-1] / top * 100, 1),
                      "change": _pct(values[-1], values[-2]) if len(values) > 1 else None,
                      "yoy": _pct(values[-1], values[-13]) if len(values) >= 13 else None,
                      "values": [round(v / top * 100, 2) for v in values]})
    return {"month": months[-1], "months": months, "anchor": cfg["anchor"], "fetched_at": now_iso(), "items": items}


def refresh(today: date | None = None) -> dict:
    got = fetch(today)
    data = rank(got["months"], got["series"])
    save_json(store_dir() / f"{data['month']}.json", data)
    return data


def editions() -> list[dict]:
    """저장된 달 전부, 최신 달 먼저."""
    d = store_dir()
    files = sorted(d.glob("????-??.json"), reverse=True) if d.is_dir() else []
    return [load_json(f) for f in files]


def latest() -> dict | None:
    return next(iter(editions()), None)


def site_editions(cfg: dict) -> list[dict]:
    """사이트에 낼 달 전부 (최신 먼저). trends_page 를 켜기 전에는 내지 않는다."""
    if not cfg.get("trends_page"):
        return []
    found = editions()
    if not found:
        log.warning("trends_page 가 켜져 있지만 데이터가 없습니다 — python -m pipeline.naver_trends refresh")
    return found


def current(found: list[dict], today: date | None = None) -> dict | None:
    """글 안 "지금" 문장에 쓸 수 있는 최신 달 (너무 오래됐으면 None)."""
    data = found[0] if found else None
    return data if data and data["month"] in month_list(today or date.today(), STALE_MONTHS) else None


# ---------------- 문장 ----------------

def month_label(month: str, year: bool = True) -> str:
    name = MONTH_NAMES[int(month[5:7]) - 1]
    return f"{name} {month[:4]}" if year else name


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def index_text(value: float) -> str:
    return "<1" if value < 1 else str(round(value))


def previous_month(data: dict) -> str:
    return data["months"][-2] if len(data["months"]) > 1 else ""


def moved(item: dict, data: dict) -> str:
    """"up from 3rd in September" / "unchanged from September" / ""."""
    prev = item.get("prev_rank")
    if not prev or not previous_month(data):
        return ""
    when = month_label(previous_month(data), year=False)
    if prev == item["rank"]:
        return f"unchanged from {when}"
    return f"{'up' if item['rank'] < prev else 'down'} from {ordinal(prev)} in {when}"


def sentence(item: dict, data: dict) -> str:
    """글 안 한 줄 (코드가 만드는 고정 틀 — 추천·효과 표현 없음)."""
    text = (f"{item['name']} ranks {ordinal(item['rank'])} of the {len(data['items'])} treatments we track "
            f"(index {index_text(item['index'])}, where the most-searched is 100)")
    return f"{text}, {moved(item, data)}." if moved(item, data) else f"{text}."


def _pattern(item: dict) -> re.Pattern | None:
    words = [w for w in item.get("match") or [] if w]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?:e?s)?(?![a-z0-9])") if words else None


def _post_text(post: dict) -> str:
    draft = post["draft"]
    text = " ".join([draft["blog"].get("title", ""), post.get("slug", ""), " ".join((draft.get("topic") or {}).get("keywords") or [])])
    return re.sub(r"[-_/]", " ", text.lower())


def post_items(post: dict, data: dict) -> list[dict]:
    """이 글이 다루는 항목 (순위 높은 순). 스폰서 글은 없다 — 광고에 인기 문장을 붙이지 않는다."""
    if post["draft"].get("sponsor"):
        return []
    text = _post_text(post)
    return [it for it in data["items"] if (p := _pattern(it)) and p.search(text)]


def post_html(post: dict, data: dict | None) -> str:
    items = post_items(post, data)[:POST_LINES] if data else []
    if not items:
        return ""
    esc = html.escape
    lines = "".join(f"<li>{esc(sentence(it, data))}</li>" for it in items)
    return (f'<aside class="trend"><p class="label">Search interest in Korea · {esc(month_label(data["month"]))}</p><ul>{lines}</ul>'
            f'<p class="note">Based on NAVER search data in Korea. Search interest is not a measure of results, safety or how many '
            f'people had the treatment. <a href="/{PATH}/">See the monthly ranking</a></p></aside>')


def post_md(post: dict, data: dict | None, base: str) -> str:
    """AI 용 글 Markdown 에 붙이는 같은 내용."""
    items = post_items(post, data)[:POST_LINES] if data else []
    if not items:
        return ""
    lines = "\n".join(f"- {sentence(it, data)}" for it in items)
    return (f"## Search interest in Korea ({month_label(data['month'])})\n\n{lines}\n\n"
            f"Based on NAVER search data in Korea; not a measure of results, safety or procedure counts. "
            f"Monthly ranking: {base}/{PATH}/\n")


def item_posts(item: dict, posts: list[dict]) -> list[dict]:
    """항목을 다루는 우리 글 — 시술 설명 글 먼저, 그다음 여행 글, 각각 최신순. 스폰서 글 제외."""
    from pipeline import site_links
    pattern = _pattern(item)
    hits = [p for p in posts if pattern and not p["draft"].get("sponsor") and pattern.search(_post_text(p))]
    hits.sort(key=lambda p: p["date"], reverse=True)
    return sorted(hits, key=site_links.is_travel)  # 안정 정렬: 설명 글 → 여행 글, 그 안에서는 최신순 유지


def movers(data: dict) -> tuple[dict | None, dict | None]:
    """(전달보다 가장 많이 오른 항목, 가장 많이 내린 항목) — 지수가 너무 작은 항목은 뺀다."""
    pool = [it for it in data["items"] if it.get("change") is not None
            and max(it["index"], it["values"][-2] if len(it["values"]) > 1 else 0) >= MOVER_MIN_INDEX]
    up = max(pool, key=lambda it: it["change"], default=None)
    down = min(pool, key=lambda it: it["change"], default=None)
    return (up if up and up["change"] > 0 else None, down if down and down["change"] < 0 else None)


def pct_text(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{'+' if value > 0 else '−' if value < 0 else '±'}{abs(round(value))}%"


def rank_move(item: dict) -> str:
    prev = item.get("prev_rank")
    if not prev:
        return "new"
    step = prev - item["rank"]
    return f"▲{step}" if step > 0 else f"▼{-step}" if step < 0 else "–"


# ---------------- 페이지 ----------------

def sparkline(values: list[float]) -> str:
    """최근 12개월 추세 (항목마다 자기 최댓값 기준 — 모양만 본다. 크기 비교는 지수 칸)."""
    pts = values[-12:]
    if len(pts) < 2:
        return ""
    w, h, pad = 120, 28, 4
    top = max(pts) or 1
    xy = [(pad + i * (w - 2 * pad) / (len(pts) - 1), h - pad - v / top * (h - 2 * pad)) for i, v in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="12-month trend">'
            f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="3" fill="currentColor"/></svg>')


def edition_path(data: dict, newest: dict) -> str:
    return f"/{PATH}/" if data["month"] == newest["month"] else f"/{PATH}/{data['month']}/"


def render(cfg: dict, data: dict, posts: list[dict], pages: dict[str, list[dict]] | None = None,
           all_editions: list[dict] | None = None) -> str:
    """월간 순위 페이지. data 가 최신 달이면 /trends/, 지난 달이면 /trends/<달>/."""
    from pipeline import site_links
    from pipeline.site import base_url, page
    esc = html.escape
    all_editions = all_editions or [data]
    newest = all_editions[0]
    path = edition_path(data, newest)
    label, n = month_label(data["month"]), len(data["items"])
    prev = month_label(previous_month(data), year=False) if previous_month(data) else "previous month"
    pages = pages or {}

    def guides(item: dict) -> str:
        links = [f'<a href="/{p["slug"]}/">{esc(p["draft"]["blog"].get("title", ""))}</a>' for p in item_posts(item, posts)[:GUIDE_LINKS]]
        if item["procedure"] in pages:
            links.append(f'<a href="/treatments/{site_links.treatment_slug(item["procedure"])}/">All {esc(site_links.short_name(item["procedure"]))} guides</a>')
        return "<br>".join(links) or '<span class="meta">No guide yet</span>'

    rows = "".join(
        f'<tr><td class="num">{it["rank"]}</td><td><strong>{esc(it["name"])}</strong><br><span class="meta" lang="ko">{esc(", ".join(it["search"]))}</span></td>'
        f'<td class="num"><span class="ml">Index </span>{index_text(it["index"])}</td><td class="num"><span class="ml">vs {esc(prev)}: </span>{rank_move(it)} <span class="meta">{pct_text(it.get("change"))}</span></td>'
        f'<td>{sparkline(it["values"])}</td><td class="tg"><span class="ml">Related: </span>{guides(it)}</td></tr>' for it in data["items"])
    top3 = "; ".join(f"{it['name']} at {index_text(it['index'])}" for it in data["items"][:3])
    up, down = movers(data)
    moves = [f"<li>Most searched in {esc(label)}: {esc(top3)}.</li>"]
    if up:
        moves.append(f"<li>Biggest rise from {esc(prev)}: {esc(up['name'])}, {pct_text(up['change'])} in search interest.</li>")
    if down:
        moves.append(f"<li>Biggest drop from {esc(prev)}: {esc(down['name'])}, {pct_text(down['change'])}.</li>")
    older = [e for e in all_editions if e["month"] != data["month"]]
    archive = ("<h2>Other months</h2><ul>" + "".join(
        f'<li><a href="{edition_path(e, newest)}">{esc(month_label(e["month"]))}</a></li>' for e in older) + "</ul>") if older else ""
    title = f"Korea Skin Treatment Search Trends — {label}"
    summary = (f"Which skin treatments people in Korea searched for most in {label}: a monthly ranking of {n} treatments by "
               f"search interest on NAVER, with links to related guides.")
    body = f"""<div class="page trends"><p class="kicker">Monthly data · {esc(label)}</p><h1>What Korea is searching for: skin treatments</h1>
<p>{esc(summary)} The leading treatment is set to 100 and the rest are shown relative to it.</p>
<ul>{"".join(moves)}</ul>
<div class="tscroll"><table><thead><tr><th>#</th><th>Treatment <span class="meta">(Korean search terms)</span></th><th>Index</th>
<th>vs {esc(prev)}</th><th>12 months</th><th>Related guides</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="meta">Index: search interest in {esc(label)}, most-searched treatment = 100. "vs {esc(prev)}": change in rank, then change in
that treatment's own search interest. The 12-month line shows each treatment's own shape and is not comparable between rows.</p>
<h2>How to read this</h2>
<ul><li><strong>Search interest is not treatment volume.</strong> It counts searches, not procedures performed, and includes people who were only curious.</li>
<li><strong>It says nothing about results or safety.</strong> A treatment being searched more does not make it more effective, safer, or right for you. That is a conversation with a licensed doctor.</li>
<li>News coverage, advertising and new product launches can move searches sharply for a month.</li>
<li>These are searches made in Korea, mostly in Korean. They show what is being talked about locally, not what international visitors book.</li>
<li>We do not rank, recommend or name clinics, here or anywhere else on {esc(cfg["name"])}.</li></ul>
<h2>Method</h2>
<p>For each treatment we send the Korean search terms shown in the table to the NAVER DataLab search trend API, monthly, all devices, ages and
genders. The API returns relative values, not search counts, and compares five topics at a time, so one topic is included in every request and used
to put all {n} on a single scale. Rankings cover only the {n} treatments on this list; it is not a ranking of every treatment in Korea.
Data for {esc(label)} retrieved {esc(data["fetched_at"][:10])}.</p>
<p class="meta">Source: <a href="{SOURCE_URL}" rel="noopener">NAVER DataLab</a> search trend data, processed by {esc(cfg["name"])}. NAVER is a
trademark of NAVER Corp. {esc(cfg["name"])} is not affiliated with NAVER.</p>
{archive}</div>"""
    base = base_url(cfg)
    jsonld = [{"@context": "https://schema.org", "@type": "Dataset", "name": title, "description": summary, "url": f"{base}{path}",
               "temporalCoverage": data["month"], "dateModified": data["fetched_at"][:10], "spatialCoverage": "South Korea",
               "isBasedOn": SOURCE_URL, "measurementTechnique": "Relative search interest on NAVER (most-searched treatment = 100)",
               "creator": {"@type": "Organization", "name": cfg["name"], "url": f"{base}/"}}]
    return page(cfg, f"{title} | {cfg['name']}", body, path=path, description=summary, jsonld=jsonld)


# ---------------- 텔레그램 요약 ----------------

def summary(data: dict, published: bool | None = None) -> str:
    lines = [f"[한국 시술 검색 관심도 {data['month']} · 네이버 · {len(data['items'])}개 항목]"]
    for it in data["items"]:
        lines.append(f"{it['rank']}. {it['search'][0]} {index_text(it['index'])} ({rank_move(it)}, {pct_text(it.get('change'))})")
    up, down = movers(data)
    if up or down:
        lines.append(" · ".join(x for x in [f"가장 오른 것: {up['search'][0]} {pct_text(up['change'])}" if up else "",
                                             f"가장 내린 것: {down['search'][0]} {pct_text(down['change'])}" if down else ""] if x))
    zero = [it["search"][0] for it in data["items"] if it["index"] < 1]
    if zero:
        lines.append(f"⚠️ 값이 거의 없는 항목: {', '.join(zero)} — naver_trends.yaml 검색어 확인")
    if published is not None:
        lines.append("다음 사이트 배포 때 /trends/ 와 글 안 문장에 반영됩니다." if published
                     else "site.yaml trends_page 가 꺼져 있어 사이트에는 나가지 않습니다.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="한국 시술 검색 관심도 (네이버 데이터랩)")
    parser.add_argument("cmd", choices=["refresh", "show", "check"])
    args = parser.parse_args(argv)
    published = bool(load_yaml("site.yaml").get("trends_page"))
    if args.cmd == "show":
        data = latest()
        print(summary(data, published) if data else "저장된 검색 관심도가 없습니다 — python -m pipeline.naver_trends refresh")
        return 0
    if not configured():
        if args.cmd == "refresh":  # 크론: 키를 넣기 전에는 조용히 건너뛴다
            log.info("NAVER_CLIENT_ID·NAVER_CLIENT_SECRET 미설정 — 검색 관심도 갱신 건너뜀")
            return 0
        print("NAVER_CLIENT_ID·NAVER_CLIENT_SECRET 이 .env 에 없습니다 (NAVER API HUB 애플리케이션 인증 정보)")
        return 1
    if args.cmd == "check":
        got = fetch()
        top = max(v[-1] for v in got["series"].values()) or 1
        print(f"기준 달 {got['months'][-1]} (1위 = 100)")
        for key, values in sorted(got["series"].items(), key=lambda kv: -kv[1][-1]):
            terms = ", ".join(str(t) for t in settings()["items"][key]["search"])
            print(f"{values[-1] / top * 100:7.2f}  {key}: {terms}")
        return 0
    print(summary(refresh(), published))
    return 0


if __name__ == "__main__":
    run_cli("naver_trends", main)
