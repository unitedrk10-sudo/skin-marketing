"""수요 점수 — 시술(config/procedures.yaml)·관광지(config/attractions.yaml)별. 주간 주제 추천 순서의 기준 (01_topics).

점수(0~1) = 사전값(카탈로그의 demand, 영어권 관심도 조사) 과 우리 트래픽의 가중 평균. 시술·관광지 모두 같은 방식.
  - 트래픽: 최근 28일 블로그 글 조회수(Cloudflare Web Analytics) + 링크 클릭(추적기: 등록기관 의도·병원 도착)을 시술별로
    글 1개당 평균으로 환산 (글을 많이 쓴 시술이 유리해지지 않게).
  - 데이터 비중은 표본이 쌓일수록 커지고 최대 70% (사전값 30% 는 항상 유지 — 아직 안 쓴 시술도 추천되도록).
  - 최근 8주에 이미 많이 쓴 시술·관광지는 조금 낮춘다 (주제 다양성).
주제 점수 = 주제에 걸리는 시술·관광지 중 가장 높은 점수. 관광지 소개·코스 글은 AI 검색 답변에 많이 인용되는 유입 통로라
관광지 점수가 시술 점수와 같은 기준으로 경쟁한다.
설정이 없거나 API 가 실패하면 사전값만 쓴다 (주제 선정이 멈추지 않게).

    python -m pipeline.demand            # 시술·관광지 점수표 출력

환경변수(선택): CF_API_TOKEN (Account Analytics:Read), CF_ACCOUNT_ID, config/site.yaml analytics_site_tag
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from pipeline.common import get_logger, load_yaml, run_cli

log = get_logger("demand")

WINDOW_DAYS = 28
RECENT_WEEKS = 8
MAX_DATA_WEIGHT = 0.7
VIEWS_FOR_FULL_WEIGHT = 2000   # 조회수가 이만큼 쌓이면 데이터 비중 최대
CLICKS_FOR_FULL_WEIGHT = 100
CLICK_VALUE = 20               # 클릭 1 = 조회 20 (병원 찾기·병원 도착은 조회보다 강한 신호)
COVERAGE_PENALTY = 0.15        # 최근 8주 글 1개당 점수 감소율
OTHER_PRIOR = 2
TREND_BOOST = 0.4              # 관광지: 트렌드 스캔 100점 = +0.4 (pipeline.trends)
SEASON_BOOST = 0.15            # 관광지: 이번·다음 달이 시즌이면 +0.15
EMERGING_PRIOR = 0.6           # 목록에 없는 신규 트렌드 장소의 기본값 (관심도 3/5 상당)
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"

PAGEVIEWS_QUERY = """query ($account: String!, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer { accounts(filter: {accountTag: $account}) {
    rumPageloadEventsAdaptiveGroups(limit: 5000, filter: $filter) { count dimensions { requestPath } }
  } }
}"""


def page_views(start: date, end: date) -> dict[str, int]:
    """경로별 조회수 (Cloudflare Web Analytics GraphQL). 미설정·실패 시 빈 dict."""
    token, account = os.environ.get("CF_API_TOKEN"), os.environ.get("CF_ACCOUNT_ID")
    site_tag = load_yaml("site.yaml").get("analytics_site_tag") or ""
    if not (token and account and site_tag):
        return {}
    variables = {"account": account, "filter": {"AND": [
        {"datetime_geq": f"{start.isoformat()}T00:00:00Z", "datetime_leq": f"{end.isoformat()}T23:59:59Z"},
        {"siteTag": site_tag}, {"bot": 0}]}}
    req = urllib.request.Request(CF_GRAPHQL, data=json.dumps({"query": PAGEVIEWS_QUERY, "variables": variables}).encode(),
                                 method="POST", headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        if body.get("errors"):
            raise ValueError(str(body["errors"])[:300])
        groups = body["data"]["viewer"]["accounts"][0]["rumPageloadEventsAdaptiveGroups"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("Cloudflare 조회수 가져오기 실패 — 사전값·클릭만 사용: %s", e)
        return {}
    views: Counter = Counter()
    for g in groups:
        views[g["dimensions"]["requestPath"]] += int(g["count"])
    return dict(views)


def link_clicks(start: date, end: date, catalog: str = "procedures") -> Counter:
    """시술(또는 관광지)별 클릭 (추적기 export, 사람만). 미설정·실패 시 빈 Counter."""
    from pipeline import analytics, tracker
    from pipeline.site import load_tracked_links
    if not tracker.configured():
        return Counter()
    try:
        rows = analytics.enrich(analytics.fetch(start, end), load_tracked_links(), analytics.load_procedures(),
                                analytics.load_catalog("attractions"))
    except (tracker.TrackerError, KeyError, TypeError) as e:
        log.warning("추적기 클릭 가져오기 실패 — 사전값·조회수만 사용: %s", e)
        return Counter()
    clicks: Counter = Counter()
    for r in rows:
        for pid in (r.get(catalog) or "other").split("|"):
            clicks[pid] += r["clicks"]
    return clicks


def posts_by_catalog(catalog: str = "procedures") -> tuple[dict[str, list[str]], Counter]:
    """게시된 글 → 시술(또는 관광지)별 경로 목록, 최근 8주 글 수."""
    from pipeline import analytics
    from pipeline.site import collect_posts
    procedures = analytics.load_catalog(catalog)
    paths: dict[str, list[str]] = {}
    recent: Counter = Counter()
    since = (date.today() - timedelta(weeks=RECENT_WEEKS)).isoformat()
    for p in collect_posts():
        meta = {"title": p["draft"]["blog"].get("title", ""), "slug": p["slug"],
                "keywords": (p["draft"].get("topic") or {}).get("keywords", [])}
        for pid in analytics.tag(meta, procedures):
            paths.setdefault(pid, []).append(f"/{p['slug']}/")
            if p["date"][:10] >= since:
                recent[pid] += 1
    return paths, recent


def scores(today: date | None = None, catalog_name: str = "procedures", views: dict[str, int] | None = None) -> dict[str, dict]:
    end = (today or date.today()) - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    catalog = load_yaml(f"{catalog_name}.yaml").get(catalog_name) or {}
    priors = {pid: float(p.get("demand", 3)) / 5 for pid, p in catalog.items()}
    priors["other"] = OTHER_PRIOR / 5
    paths, recent = posts_by_catalog(catalog_name)
    views = page_views(start, end) if views is None else views
    clicks = link_clicks(start, end, catalog_name)

    signal: dict[str, float] = {}
    raw: dict[str, dict] = {}
    for pid in priors:
        n_posts = len(paths.get(pid, []))
        v = sum(views.get(path, 0) + views.get(path.rstrip("/"), 0) for path in paths.get(pid, []))
        c = clicks.get(pid, 0)
        raw[pid] = {"posts": n_posts, "views": v, "clicks": c}
        if n_posts:
            signal[pid] = (v + CLICK_VALUE * c) / n_posts
    top = max(signal.values(), default=0)
    total_views, total_clicks = sum(views.values()), sum(clicks.values())
    weight = min(MAX_DATA_WEIGHT, MAX_DATA_WEIGHT * max(total_views / VIEWS_FOR_FULL_WEIGHT, total_clicks / CLICKS_FOR_FULL_WEIGHT))

    # 관광지는 유행·계절을 탄다: 최근 트렌드 스캔(21일 이내)과 계절 가산점
    from pipeline import trends
    scan = trends.place_trends(trends.latest(today)) if catalog_name == "attractions" else {}  # 관광지 + 근처 맛집·카페 트렌드

    out = {}
    for pid, prior in priors.items():
        # 글이 없는 시술은 데이터가 없으므로 사전값 그대로 (아직 안 써본 시술을 불리하게 만들지 않음)
        data = signal[pid] / top if pid in signal and top else prior
        w = weight if pid in signal and top else 0.0
        trend = scan.get(pid, {})
        season = catalog_name == "attractions" and trends.in_season(catalog.get(pid) or {}, today)
        base = (1 - w) * prior + w * data + TREND_BOOST * trend.get("trend", 0) / 100 + (SEASON_BOOST if season else 0)
        score = base / (1 + COVERAGE_PENALTY * recent.get(pid, 0))
        out[pid] = {**raw[pid], "prior": prior, "recent_posts": recent.get(pid, 0), "data_weight": round(w, 2),
                    "trend": trend.get("trend", 0), "trend_why": trend.get("why", ""), "season": season,
                    "score": round(score, 3), "name": (catalog.get(pid) or {}).get("name", "Other")}
    return out


def reason(s: dict) -> str:
    parts = [f"관심도 {round(s['prior'] * 5)}/5"]
    if s["data_weight"]:
        parts.append(f"조회 {s['views']}·클릭 {s['clicks']} (글 {s['posts']}개, 데이터 {round(s['data_weight'] * 100)}%)")
    if s.get("trend"):
        parts.append(f"🔥 트렌드 {s['trend']}")
    if s.get("season"):
        parts.append("🍂 시즌")
    if s["recent_posts"]:
        parts.append(f"최근 8주 {s['recent_posts']}편")
    return " · ".join(parts)


def all_scores(today: date | None = None) -> dict[str, dict[str, dict]]:
    """{"procedures": {...}, "attractions": {...}} — 조회수는 한 번만 가져온다."""
    end = (today or date.today()) - timedelta(days=1)
    views = page_views(end - timedelta(days=WINDOW_DAYS - 1), end)
    return {name: scores(today, name, views) for name in ("procedures", "attractions")}


def topic_score(topic: dict, tables: dict[str, dict[str, dict]]) -> tuple[float, str, str]:
    """주제 → (점수, 대표 항목 "catalog:id", 근거). 시술·관광지 중 걸리는 것 가운데 가장 높은 점수.
    어느 것에도 안 걸리면 시술 "other" 점수."""
    from pipeline import analytics
    meta = {"title": topic.get("title", ""), "keywords": topic.get("keywords", [])}
    hits = [(name, pid) for name, table in tables.items()
            for pid in analytics.tag(meta, analytics.load_catalog(name)) if pid != "other" and pid in table]
    if not hits:
        s = tables["procedures"]["other"]
        return s["score"], "procedures:other", f"{s['name']}: {reason(s)}"
    name, pid = max(hits, key=lambda h: tables[h[0]][h[1]]["score"])
    s = tables[name][pid]
    return s["score"], f"{name}:{pid}", f"{s['name']}: {reason(s)}"


def prompt_block(tables: dict[str, dict[str, dict]], limit: int = 8) -> str:
    """주제 조사 프롬프트용 — 수요 높은 순 시술·관광지 목록."""
    out = []
    for name, label in (("procedures", "Procedures"), ("attractions", "Places & areas")):
        rows = sorted((s for pid, s in tables.get(name, {}).items() if pid != "other"), key=lambda s: -s["score"])[:limit]
        out.append(f"{label}:")
        out += [f"- {s['name']}: demand score {s['score']:.2f}"
                + (f" (our data: {s['views']} views, {s['clicks']} clinic-search clicks on {s['posts']} posts)"
                   if s["data_weight"] else "")
                + (f" [trending: {s['trend_why']}]" if s.get("trend") else "")
                + (" [in season now or next month]" if s.get("season") else "") for s in rows]
    from pipeline import trends
    data = trends.latest()
    if data and data.get("emerging"):
        out.append("Trending right now (new places/events, from this week's scan):")
        out += [f"- {e['name']} ({e['area']}): trend {e['trend']} — {e['why']}" for e in data["emerging"]]
    if data and data.get("food"):
        out.append("Trending food & cafes (from this week's scan):")
        out += [f"- {f['name']} ({f['kind']}, {f['area']}): trend {f['trend']} — {f['why']}" for f in data["food"]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="시술·관광지 수요 점수").parse_args(argv)
    for name, table in all_scores().items():
        label = "시술" if name == "procedures" else "관광지·지역"
        print(f"[{label} 수요 점수 {datetime.now(timezone.utc).date()}] 높은 순")
        for pid, s in sorted(table.items(), key=lambda x: -x[1]["score"]):
            print(f"{s['score']:.2f}\t{s['name']}\t{reason(s)}")
    return 0


if __name__ == "__main__":
    run_cli("demand", main)
