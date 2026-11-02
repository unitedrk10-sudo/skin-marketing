"""글별 도달 추세 — 지난주와 그 전 주 비교, 기존 글·새 글 구분, 새 글 첫 7일 (주간 리포트, 2026-11-02).

주 = 일~토 (일요일 저녁 리포트 때 다 찬 주). 글별 숫자는 Cloudflare 서버 요청 중 브라우저 모양인 것(봇 이름 제외):
요청이 몰린 위장 봇(DISGUISED)도 뺀다. 남은 위장 봇이 있어 사람 수의 상한으로 보지만 글마다 거의 전수로 잡혀 추세 비교가 된다. Web Analytics(JS 비콘, demand.page_views)는
사람에 가깝지만 표본이라 글이 적게 읽힐 때는 글별 숫자가 0·10 처럼 튀어서 합계만 같이 보여 준다.
서버 기록은 약 31일만 남아 다 찬 주의 숫자를 content/reports/page_requests.json 에 쌓는다.

    python -m pipeline.trend    # 숫자 출력 + reports/weekly/<주차>-trend.png
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import date, datetime, timedelta

from pipeline.common import ROOT, content_dir, get_logger, iso_week, load_json, run_cli, save_json

log = get_logger("trend")

WEEKS = 6
TOP_POSTS = 12
TITLE_CHARS = 40
NON_BROWSER = re.compile(r"bot|crawl|spider|slurp|fetch|preview|headless|python|curl|wget|node|axios|go-http|scan|monitor|facebookexternal|meta-", re.I)
# 브라우저인 척하는 자동 수집: 2019년 iOS·리눅스 데스크톱 같은 판 하나가 글 요청의 81% (2026-11-02 확인, 사람이면 이렇게 몰리지 않는다)
DISGUISED = re.compile(r"iPhone OS 13_2_3|X11; Linux x86_64\) AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/138\.|^Mozilla/5\.0$")
REQUEST_QUERY = """query ($zone: String!, $filter: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 5000, filter: $filter) { count dimensions { clientRequestPath userAgent } }
  } }
}"""
# dataviz 기본 팔레트 슬롯 1·2 (validate_palette.js 통과), 이전 기간은 같은 파랑의 옅은 단계
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
EXISTING, NEW, PREVIOUS = "#2a78d6", "#eb6834", "#a9c9ef"


def post_date(post: dict) -> date:
    return datetime.fromisoformat(str(post["date"])).date()


def _views_of(views: dict[str, int], slug: str) -> int:
    return views.get(f"/{slug}/", 0) + views.get(f"/{slug}", 0)


def page_requests(start: date, end: date) -> dict[str, int]:
    """페이지 경로별 브라우저 모양 요청 수 (봇 이름·파일 요청 제외). 보관 기간 밖이거나 실패하면 예외."""
    from pipeline import ai_traffic
    token, _ = ai_traffic._creds()
    data = ai_traffic._post(REQUEST_QUERY, {"zone": ai_traffic._zone_id(token), "filter": {
        "datetime_geq": f"{start}T00:00:00Z", "datetime_leq": f"{end}T23:59:59Z", "requestSource": "eyeball",
        "edgeResponseStatus": 200}}, token)
    out: Counter = Counter()
    for g in data["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]:
        path, agent = g["dimensions"]["clientRequestPath"] or "/", g["dimensions"]["userAgent"] or ""
        if path.endswith("/") and "Mozilla" in agent and not NON_BROWSER.search(agent) and not DISGUISED.search(agent):
            out[path] += int(g["count"])
    return dict(out)


def _cache_file():
    return content_dir() / "reports" / "page_requests.json"


def weekly(a: date, b: date, cache: dict, fetch) -> dict[str, int] | None:
    """한 주 숫자. 쌓아 둔 것 → 없으면 조회해서 저장. 보관 기간이 지나 못 읽으면 None."""
    key = f"{a}_{b}"
    if key not in cache:
        try:
            cache[key] = fetch(a, b)
        except Exception as e:  # noqa: BLE001 — 오래된 주는 원래 못 읽는다
            log.info("%s 조회 못 함: %s", key, str(e)[:120])
            return None
    return cache[key]


def collect(today: date | None = None, weeks: int = WEEKS, posts: list[dict] | None = None, fetch=None, rum=None) -> dict | None:
    """최근 다 찬 주(일~토)들. 비교할 두 주를 못 읽으면 None."""
    from pipeline import demand, site
    fetch, rum = fetch or page_requests, rum or demand.page_views
    today = today or date.today()
    posts = site.collect_posts() if posts is None else posts
    last_sat = today - timedelta(days=(today.weekday() + 2) % 7 or 7)  # 오늘이 토요일이면 그 전 토요일 (아직 안 끝남)
    cache = load_json(_cache_file()) if _cache_file().exists() else {}
    spans = [(last_sat - timedelta(days=7 * i + 6), last_sat - timedelta(days=7 * i)) for i in range(weeks - 1, -1, -1)]
    pairs = [(span, weekly(*span, cache, fetch)) for span in spans]
    pairs = [(span, v) for span, v in pairs if v is not None]
    if len(pairs) < 2 or pairs[-1][0][1] != last_sat:
        return None
    save_json(_cache_file(), cache)
    rows = [{"slug": p["slug"], "title": p["draft"].get("blog", {}).get("title") or p["slug"], "date": post_date(p),
             "views": [_views_of(v, p["slug"]) for _, v in pairs]} for p in posts]
    series = []
    for (a, b), v in pairs:
        new = sum(_views_of(v, r["slug"]) for r in rows if a <= r["date"] <= b)
        total = sum(_views_of(v, r["slug"]) for r in rows)
        series.append({"from": a, "to": b, "new": new, "existing": total - new, "home": v.get("/", 0),
                       "new_posts": sum(1 for r in rows if a <= r["date"] <= b)})
    end = today - timedelta(days=1)
    for r in rows:  # 새 글: 게시일부터 7일
        r["age"] = (today - r["date"]).days
        r["first7"] = None
        if 0 < r["age"] <= 24:
            try:
                r["first7"] = _views_of(fetch(r["date"], min(r["date"] + timedelta(days=6), end)), r["slug"])
            except Exception as e:  # noqa: BLE001
                log.info("%s 첫 7일 조회 못 함: %s", r["slug"], str(e)[:120])
    a, b = series[-1]["from"], series[-1]["to"]
    seen = rum(a, b)
    return {"windows": series, "rows": rows, "rum": sum(n for p, n in seen.items() if p != "/") if seen else None}


def report_lines(data: dict) -> list[str]:
    last, prev = data["windows"][-1], data["windows"][-2]
    total, before = last["new"] + last["existing"], prev["new"] + prev["existing"]
    rows = data["rows"]
    lines = [f"📊 글 도달 추세 ({last['from']:%m/%d}~{last['to']:%m/%d}, 그 전 주와 비교)",
             f"   글 전체 {before} → {total} · 홈 {prev['home']} → {last['home']}",
             f"   기존 글 {before} → {last['existing']} · 새 글 {last['new_posts']}편 {last['new']}"]
    moved = sorted((r for r in rows if r["views"][-1] != r["views"][-2]), key=lambda r: abs(r["views"][-1] - r["views"][-2]), reverse=True)
    for r in moved[:6]:
        lines.append(f"   {r['views'][-2]} → {r['views'][-1]}  {r['title'][:TITLE_CHARS]}")
    newest = [r for r in rows if last["from"] <= r["date"] <= last["to"]]
    older = sorted((r for r in rows if r["date"] < last["from"]), key=lambda r: r["date"], reverse=True)[:len(newest)]
    if newest and older:
        lines.append(f"   새 글 {len(newest)}편 {sum(r['views'][-1] for r in newest)} vs 바로 전 {len(older)}편 {sum(r['views'][-1] for r in older)} (같은 주)")
    fresh = sorted((r for r in rows if r["first7"] is not None), key=lambda r: r["date"], reverse=True)[:6]
    if fresh:
        lines.append("   새 글 첫 7일:")
        for r in fresh:
            partial = f", {r['age']}일째" if r["age"] < 7 else ""
            lines.append(f"   {r['first7']} ({r['date']:%m/%d} 게시{partial})  {r['title'][:TITLE_CHARS]}")
    note = "   ※ 서버 요청에서 봇 이름·위장 봇 패턴을 뺀 숫자. 남은 봇이 있어 사람 수의 상한"
    if data.get("rum") is not None:
        note += f" (같은 주 JS 집계 글 조회 {data['rum']}, 표본)"
    return lines + [note]


def chart(data: dict, out):
    """왼쪽 = 기존 글·새 글 추이, 오른쪽 = 글별 지난주와 그 전 주. matplotlib 없으면 None (리포트는 글자로 나간다)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib 없음 — 그래프 없이 리포트")
        return None
    plt.rcParams.update({"font.family": ["Malgun Gothic", "DejaVu Sans"], "axes.unicode_minus": False,
                         "text.color": INK, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    w = data["windows"]
    rows = sorted(data["rows"], key=lambda r: (-(r["views"][-1] + r["views"][-2]), r["slug"]))[:TOP_POSTS]
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.5, 6), dpi=150, width_ratios=[1, 1.35], facecolor=SURFACE)
    for ax in (left, right):
        ax.set_facecolor(SURFACE)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(length=0)

    xs = range(len(w))
    existing, new = [x["existing"] for x in w], [x["new"] for x in w]
    left.bar(xs, existing, 0.58, color=EXISTING, edgecolor=SURFACE, linewidth=2, label="기존 글")
    left.bar(xs, new, 0.58, bottom=existing, color=NEW, edgecolor=SURFACE, linewidth=2, label="그 주에 나온 새 글")
    top = max(max(a + b for a, b in zip(existing, new)), 1)
    for x, a, b in zip(xs, existing, new):
        left.text(x, a + b + top * 0.02, str(a + b), ha="center", va="bottom", fontsize=11, fontweight="bold")
    left.set_xticks(list(xs), [f"{x['to']:%m/%d}" for x in w], fontsize=10)
    left.set_ylim(0, top * 1.25)
    left.set_xlabel("주 (일~토)의 마지막 날", fontsize=10)
    left.yaxis.grid(True, color=GRID, linewidth=0.8)
    left.set_axisbelow(True)
    left.set_title("글 도달 추이 (서버 요청, 홈 제외)", loc="left", fontsize=14, fontweight="bold", pad=14)
    left.legend(frameon=False, loc="upper left", fontsize=10, ncols=2)

    ys = list(range(len(rows)))[::-1]
    h = 0.36
    right.barh([y + h / 2 for y in ys], [r["views"][-2] for r in rows], h, color=PREVIOUS, edgecolor=SURFACE, linewidth=1, label="그 전 주")
    right.barh([y - h / 2 for y in ys], [r["views"][-1] for r in rows], h, color=EXISTING, edgecolor=SURFACE, linewidth=1, label="지난주")
    peak = max((max(r["views"][-1], r["views"][-2]) for r in rows), default=0) or 1
    for y, r in zip(ys, rows):
        right.text(r["views"][-1] + peak * 0.015, y - h / 2, str(r["views"][-1]), va="center", fontsize=9)
        right.text(r["views"][-2] + peak * 0.015, y + h / 2, str(r["views"][-2]), va="center", fontsize=9, color=MUTED)
    right.set_yticks(ys, [r["title"][:TITLE_CHARS] + ("…" if len(r["title"]) > TITLE_CHARS else "") for r in rows], fontsize=9)
    right.set_xlim(0, peak * 1.12)
    right.xaxis.set_visible(False)
    right.spines["bottom"].set_visible(False)
    right.set_title("글별 변화", loc="left", fontsize=14, fontweight="bold", pad=14)
    right.legend(frameon=False, loc="lower right", fontsize=10)

    fig.tight_layout(w_pad=3)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def report(today: date | None = None) -> list[str]:
    """주간 리포트용 줄 + 그래프 첨부 줄. """
    data = collect(today)
    if not data:
        return ["📊 글 도달 추세: 숫자를 읽지 못함 (Cloudflare 연결 확인)"]
    base = ROOT / "reports" / "weekly" / f"{iso_week()}-trend"
    lines = report_lines(data)
    png = chart(data, base.with_suffix(".png"))
    return lines + ([f"MEDIA:`{png}`"] if png else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="글별 도달 추세")
    parser.parse_args(argv)
    data = collect()
    if not data:
        print("숫자를 읽지 못했습니다")
        return 1
    print("\n".join(report_lines(data)))
    print(chart(data, ROOT / "reports" / "weekly" / f"{iso_week()}-trend.png") or "그래프 없음")
    return 0


if __name__ == "__main__":
    run_cli("trend", main)
