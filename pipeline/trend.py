"""글별 도달 추세 — 지난주와 그 전 주 비교, 기존 글·새 글 구분, 새 글 첫 7일 (주간 리포트, 2026-11-02).

주 = 일~토 (일요일 저녁 리포트 때 다 찬 주). 글별 숫자는 Cloudflare 서버 요청 중 브라우저 모양인 것(봇 이름 제외):
요청이 몰린 위장 봇(DISGUISED)도 뺀다. 남은 위장 봇이 있어 사람 수의 상한으로 보지만 글마다 거의 전수로 잡혀 추세 비교가 된다. Web Analytics(JS 비콘, demand.page_views)는
사람에 가깝지만 표본이라 글이 적게 읽힐 때는 글별 숫자가 0·10 처럼 튀어서 합계만 같이 보여 준다.
서버 기록은 약 31일만 남아 다 찬 주의 숫자를 content/reports/page_requests.json 에 쌓는다.
그래프 왼쪽은 날짜별 선(2026-11-17 — 주 합계 막대로는 언제 오르내렸는지 안 보였다): 최근 4주, 날짜별 숫자는
content/reports/page_requests_daily.json 에 쌓는다 (날짜 경계는 UTC).

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
DAILY_DAYS = 28
BOT_MIN_VIEWS = 5    # 봇 의심 판정에 넣을 글 (두 주 중 많은 쪽 조회)
BOT_MIN_POSTS = 5
BOT_SHARE = 0.8      # 기존 글의 이만큼이 같은 방향으로 2배 이상 움직이면 사람이 아니라 수집 봇으로 본다
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
SURFACE, INK, MUTED, GRID, MARK = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#d3d1c9"
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


def _daily_file():
    return content_dir() / "reports" / "page_requests_daily.json"


def daily(end: date, slugs: list[str], fetch, rum, days: int = DAILY_DAYS) -> list[dict]:
    """end 까지 days 일의 날짜별 {"date", "requests"(서버 요청·글), "views"(JS 집계·글)}. 못 읽은 날은 None.
    쌓아 둔 날은 다시 조회하지 않는다. JS 집계는 빈 결과가 실패인지 0 인지 구분이 안 돼 빈 날은 쌓지 않는다."""
    cache = load_json(_daily_file()) if _daily_file().exists() else {}
    req, seen = cache.setdefault("requests", {}), cache.setdefault("rum", {})
    out = []
    for i in range(days - 1, -1, -1):
        day = end - timedelta(days=i)
        key = day.isoformat()
        if key not in req:
            try:
                req[key] = fetch(day, day)
            except Exception as e:  # noqa: BLE001 — 보관 기간이 지난 날
                log.info("%s 조회 못 함: %s", key, str(e)[:120])
        if key not in seen and (got := rum(day, day)):
            seen[key] = got
        out.append({"date": day, "requests": sum(_views_of(req[key], s) for s in slugs) if key in req else None,
                    "views": sum(n for path, n in seen[key].items() if path != "/") if key in seen else None})
    save_json(_daily_file(), cache)
    if not any(d["views"] is not None for d in out):
        return out  # JS 집계를 하나도 못 읽었으면 선을 그리지 않는다
    return [{**d, "views": d["views"] or 0} for d in out]


def bot_suspect(data: dict) -> str:
    """기존 글 대부분이 한 주 사이 같은 방향으로 2배 이상 움직였으면 경고 문구 (아니면 빈 문자열).
    사람 방문은 글마다 다르게 움직인다 — 2026-11-07 주에 글 12편이 전부 30 안팎이었다가 다음 주 전부 5 안팎이 됐다."""
    prev = data["windows"][-2]
    rows = [r for r in data["rows"] if r["date"] < prev["from"] and max(r["views"][-1], r["views"][-2]) >= BOT_MIN_VIEWS]
    up = sum(1 for r in rows if r["views"][-1] >= 2 * max(r["views"][-2], 1))
    down = sum(1 for r in rows if r["views"][-2] >= 2 * max(r["views"][-1], 1))
    if len(rows) < BOT_MIN_POSTS or max(up, down) < len(rows) * BOT_SHARE:
        return ""
    if up > down:
        return f"⚠️ 기존 글 {len(rows)}편 중 {up}편이 한꺼번에 2배 이상 늘었음 — 이번 주 서버 요청이 수집 봇으로 부풀었을 가능성"
    return f"⚠️ 기존 글 {len(rows)}편 중 {down}편이 한꺼번에 절반 이하로 줄었음 — 전주 서버 요청이 수집 봇으로 부풀었을 가능성"


def movers(data: dict, n: int = 2) -> tuple[list[dict], list[dict]]:
    """이번 주 전에 나온 글 중 가장 많이 오른 글·내린 글."""
    start = data["windows"][-1]["from"]
    rows = sorted((r for r in data["rows"] if r["date"] < start), key=lambda r: r["views"][-1] - r["views"][-2])
    return ([r for r in reversed(rows) if r["views"][-1] > r["views"][-2]][:n],
            [r for r in rows if r["views"][-1] < r["views"][-2]][:n])


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
    def js_views(window: dict) -> int | None:
        seen = rum(window["from"], window["to"])
        return sum(n for p, n in seen.items() if p != "/") if seen else None
    return {"windows": series, "rows": rows, "rum": js_views(series[-1]), "rum_prev": js_views(series[-2]),
            "daily": daily(last_sat, [r["slug"] for r in rows], fetch, rum)}


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
    """왼쪽 = 날짜별 글 조회 선, 오른쪽 = 글별 지난주와 그 전 주. matplotlib 없으면 None (리포트는 글자로 나간다)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib 없음 — 그래프 없이 리포트")
        return None
    plt.rcParams.update({"font.family": ["Malgun Gothic", "DejaVu Sans"], "axes.unicode_minus": False,
                         "text.color": INK, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    rows = sorted(data["rows"], key=lambda r: (-(r["views"][-1] + r["views"][-2]), r["slug"]))[:TOP_POSTS]
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.5, 6), dpi=150, width_ratios=[1.25, 1], facecolor=SURFACE)
    for ax in (left, right):
        ax.set_facecolor(SURFACE)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(length=0)

    days = data.get("daily") or []
    xs = range(len(days))
    series = [("requests", "서버 요청 (봇 일부 포함)", EXISTING), ("views", "JS 집계 (사람에 가까움)", NEW)]
    top = max([d[k] or 0 for d in days for k, _, _ in series] + [1])
    posted = {r["date"] for r in data["rows"]}
    for x, d in zip(xs, days):
        if d["date"] in posted:  # 글 게시일 — 옅은 세로선
            left.axvline(x, color=MARK, linewidth=1, zorder=1)
    for key, label, color in series:
        pts = [(x, d[key]) for x, d in zip(xs, days) if d[key] is not None]
        if not pts:
            continue
        left.plot(*zip(*pts), color=color, linewidth=2, label=label, zorder=3, solid_capstyle="round")
        left.scatter(*pts[-1], s=36, color=color, edgecolor=SURFACE, linewidth=2, zorder=4)
        left.text(pts[-1][0] + 0.5, pts[-1][1], str(pts[-1][1]), va="center", fontsize=10, fontweight="bold")
    left.plot([], [], color=MARK, linewidth=1, label="글 게시일")
    sundays = [x for x, d in zip(xs, days) if d["date"].weekday() == 6]
    left.set_xticks(sundays, [f"{days[x]['date']:%m/%d}" for x in sundays], fontsize=10)
    left.set_xlim(-0.5, len(days) + 1.5)
    left.set_ylim(0, top * 1.25)
    left.set_xlabel("날짜 (눈금 = 일요일, UTC)", fontsize=10)
    left.yaxis.grid(True, color=GRID, linewidth=0.8)
    left.set_axisbelow(True)
    left.set_title(f"일별 글 조회 (최근 {len(days) // 7}주, 홈 제외)", loc="left", fontsize=14, fontweight="bold", pad=34)
    left.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1), borderaxespad=0.2, fontsize=10, ncols=3, handlelength=1.4, columnspacing=1.2)

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
    right.set_title("글별 변화 (서버 요청)", loc="left", fontsize=14, fontweight="bold", pad=34)
    right.legend(frameon=False, loc="lower right", fontsize=10)

    fig.tight_layout(w_pad=3)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def report(today: date | None = None, data: dict | None = None) -> list[str]:
    """주간 리포트용 줄 + 그래프 첨부 줄. data 를 주면 다시 조회하지 않는다."""
    data = data or collect(today)
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
