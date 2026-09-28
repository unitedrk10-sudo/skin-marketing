"""관광지 트렌드 — 지금 외국인 사이에서 뜨는 곳·행사(주 1회 Gemini 검색 스캔) + 계절 달력.

관광지 수요 점수(pipeline.demand)에 더해지는 두 가지:
  1. 트렌드 스캔 (content/trends/<주차>.json): 알려진 관광지별 trend 0~100 + 목록에 없는 신규 장소·행사(emerging).
     출처 URL 없는 항목은 버린다. 스캔이 21일보다 오래되면 반영하지 않는다 (지난 유행으로 추천하지 않게).
  2. 계절 (config/attractions.yaml season: 월 목록): 이번 달·다음 달이 시즌이면 가산점 (여행자는 1~2달 전에 검색·계획).
신규 장소는 주제 후보(travel_guide)로 바로 올라간다 (01_topics). 반복해서 뜨면 attractions.yaml 에 정식 추가한다.

    python -m pipeline.trends scan [--week 2026-W40]    # 스캔 후 요약 출력
    python -m pipeline.trends show                      # 최근 스캔 + 이번 시즌 관광지
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

from pipeline import llm
from pipeline.common import content_dir, get_logger, iso_week, load_json, load_yaml, now_iso, prompt, run_cli, save_json, slugify

log = get_logger("trends")

MAX_AGE_DAYS = 21       # 이보다 오래된 스캔은 무시
SCAN_EVERY_DAYS = 6     # 01_topics 가 이 간격으로만 새로 스캔 (주 1회)
MAX_EMERGING = 5


def trends_dir():
    return content_dir() / "trends"


def _sources(item: dict) -> list[str]:
    return [u for u in (item.get("sources") or []) if isinstance(u, str) and u.startswith("http")]


def _trend(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def validate(data: dict, known: set[str], today: date) -> dict:
    """출처 없는 항목·알 수 없는 id·끝난 행사를 버린다."""
    attractions, emerging = {}, []
    for a in data.get("attractions") or []:
        if a.get("id") in known and _sources(a) and _trend(a.get("trend")):
            prev = attractions.get(a["id"])
            if not prev or _trend(a["trend"]) > prev["trend"]:
                attractions[a["id"]] = {"trend": _trend(a["trend"]), "why": str(a.get("why", ""))[:300], "sources": _sources(a)}
    for e in data.get("emerging") or []:
        topic = e.get("topic") or {}
        until = str(e.get("until") or "")
        if until:
            try:
                if date.fromisoformat(until) < today:
                    continue
            except ValueError:
                until = ""
        if not (e.get("name") and _sources(e) and topic.get("title") and topic.get("angle")):
            continue
        emerging.append({"id": f"trend-{slugify(e['name'], 40)}", "name": e["name"], "area": e.get("area", ""),
                         "trend": _trend(e.get("trend")), "why": str(e.get("why", ""))[:300], "sources": _sources(e),
                         "until": until, "topic": {"title": str(topic["title"])[:80], "angle": topic["angle"],
                                                   "keywords": list(topic.get("keywords") or [])[:6],
                                                   "hook": topic.get("hook", "")}})
    emerging.sort(key=lambda e: -e["trend"])
    return {"attractions": attractions, "emerging": emerging[:MAX_EMERGING]}


def scan(week: str | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    week = week or iso_week()
    catalog = load_yaml("attractions.yaml").get("attractions") or {}
    text = prompt("trend_scan", today=today.isoformat(), week=week,
                  catalog="\n".join(f"- {aid}: {a['name']} ({a.get('area', '')})" for aid, a in catalog.items()))
    data, result = llm.generate_json("trends", text)
    out = {"week": week, "scanned_at": now_iso(), "model": result.model, "grounding_urls": result.grounding_urls,
           **validate(data if isinstance(data, dict) else {}, set(catalog), today)}
    save_json(trends_dir() / f"{week}.json", out)
    log.info("트렌드 스캔 %s: 관광지 %d, 신규 %d", week, len(out["attractions"]), len(out["emerging"]))
    return out


def latest(today: date | None = None, max_age: int = MAX_AGE_DAYS) -> dict | None:
    """가장 최근 스캔 (max_age 일 이내만)."""
    today = today or date.today()
    files = sorted(trends_dir().glob("*.json"), reverse=True) if trends_dir().exists() else []
    for f in files:
        data = load_json(f)
        scanned = datetime.fromisoformat(data["scanned_at"]).date()
        if (today - scanned).days <= max_age:
            return data
    return None


def ensure_fresh(today: date | None = None) -> dict | None:
    """최근 스캔이 없으면 새로 스캔. 실패해도 주제 선정은 계속한다 (트렌드 없이 사전값·계절만)."""
    fresh = latest(today, SCAN_EVERY_DAYS)
    if fresh:
        return fresh
    try:
        return scan(today=today)
    except (llm.LLMError, KeyError, ValueError) as e:
        log.warning("트렌드 스캔 실패 — 트렌드 없이 진행: %s", e)
        return latest(today)


def in_season(attraction: dict, today: date | None = None) -> bool:
    """이번 달 또는 다음 달이 시즌 (여행 1~2달 전 검색·계획)."""
    today = today or date.today()
    months = set(attraction.get("season") or [])
    return bool(months & {today.month, today.month % 12 + 1})


def emerging_topics(data: dict | None) -> list[dict]:
    """신규 트렌드 장소 → 01_topics 주제 후보 (travel_guide)."""
    out = []
    for e in (data or {}).get("emerging", []):
        t = e["topic"]
        out.append({"title": t["title"], "axis": "travel_guide", "angle": t["angle"], "keywords": t["keywords"],
                    "hook": t["hook"], "has_price": False, "why_now": f"Trending: {e['why']}",
                    "sources": [{"url": u, "title": ""} for u in e["sources"]], "seed_id": e["id"],
                    "trend": {"name": e["name"], "score": e["trend"] / 100}})
    return out


def summary(data: dict | None, today: date | None = None) -> str:
    catalog = load_yaml("attractions.yaml").get("attractions") or {}
    lines = []
    if data:
        lines.append(f"[관광지 트렌드 {data['week']}]")
        for aid, t in sorted(data["attractions"].items(), key=lambda x: -x[1]["trend"]):
            lines.append(f"- {catalog.get(aid, {}).get('name', aid)} {t['trend']}: {t['why']}")
        for e in data["emerging"]:
            lines.append(f"- 🆕 {e['name']} ({e['area']}) {e['trend']}: {e['why']}")
    else:
        lines.append("[관광지 트렌드] 최근 스캔 없음")
    season = [a["name"] for a in catalog.values() if in_season(a, today)]
    lines.append("🍂 이번·다음 달 시즌: " + (", ".join(season) or "-"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="관광지 트렌드")
    parser.add_argument("cmd", choices=["scan", "show"])
    parser.add_argument("--week", default=None)
    args = parser.parse_args(argv)
    data = scan(args.week) if args.cmd == "scan" else latest()
    print(summary(data))
    return 0


if __name__ == "__main__":
    run_cli("trends", main)
