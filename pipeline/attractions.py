"""관광지 카탈로그(config/attractions.yaml) — 관광지 소개·코스 글의 코스 짜기 데이터.

여행 축(procedure_travel, travel_guide) 글을 만들 때 02_draft 가 프롬프트에 넣는다:
  - 주제에 걸린 관광지 + 수요 점수 높은 관광지 목록과 속성(실내·햇빛·활동량·사우나·권역)
  - 코스 규칙: 시술 당일은 가까운 실내·저활동, 다음 날(관찰일)은 "관찰일 가능" 장소만, 햇빛·사우나·등산·당일치기·타 도시는 그 뒤
속성은 코스 판단용 내부 데이터일 뿐이고, 글에 쓰는 사실은 공식 출처(VisitKorea, Visit Seoul, 시설 공식 사이트)에서 URL 과 함께 수집한다.

    python -m pipeline.attractions            # 관찰일 가능/불가 목록 출력
"""

from __future__ import annotations

import argparse

from pipeline.common import load_yaml, run_cli

TRAVEL_AXES = {"procedure_travel", "travel_guide"}
SEOUL_ZONES = {"gangnam", "central", "east", "west", "north"}
CONTEXT_LIMIT = 10


def load() -> dict[str, dict]:
    items = load_yaml("attractions.yaml").get("attractions") or {}
    return {aid: {**a, "id": aid, "observation_ok": observation_ok(a)} for aid, a in items.items()}


def observation_ok(a: dict) -> bool:
    """시술 다음 날(관찰일)에 넣어도 되는 곳: 실내이거나 햇빛 적음 + 활동 적음 + 사우나 아님 + 서울 시내."""
    return ((a.get("setting") == "indoor" or a.get("sun") == "low") and a.get("activity") == "low"
            and not a.get("heat") and a.get("zone") in SEOUL_ZONES)


def describe(a: dict) -> str:
    notes = [a.get("setting", "?"), f"sun {a.get('sun', '?')}", f"activity {a.get('activity', '?')}"]
    if a.get("heat"):
        notes.append("sauna/heat")
    zone = a.get("zone", "")
    notes.append({"day_trip": "day trip from Seoul", "other_city": "another city"}.get(zone, f"Seoul {zone}"))
    fit = "OK for the observation day" if a["observation_ok"] else "NOT for the procedure day or the observation day"
    return f"- {a['name']} ({a.get('area', '')}; {', '.join(notes)}) — {fit}"


def context(topic: dict, tables: dict | None = None) -> str:
    """여행 축 주제용 프롬프트 블록. 다른 축은 빈 문자열."""
    if topic.get("axis") not in TRAVEL_AXES:
        return ""
    from pipeline import analytics, demand
    catalog = load()
    tagged = [a for a in analytics.tag({"title": topic.get("title", ""), "keywords": topic.get("keywords", [])},
                                      analytics.load_catalog("attractions")) if a in catalog]
    table = (tables or {}).get("attractions") or demand.scores(catalog_name="attractions")
    ranked = sorted(catalog, key=lambda a: -table.get(a, {}).get("score", 0))
    chosen = list(dict.fromkeys(tagged + ranked))[:max(CONTEXT_LIMIT, len(tagged))]
    lines = [describe(catalog[a]) for a in chosen]
    guide = ("For a place guide: what the place is, how to get there by subway, what to see and do, how much time to allow, "
             "tips for visitors who recently had a skin treatment (sun, heat, crowds), and a half-day course with nearby places. "
             if topic["axis"] == "travel_guide" else "")
    return f"""
TRAVEL PLANNING DATA (internal attributes for building routes — NOT facts to publish; every fact about a place such as
opening hours, fees, access or what is there must come from a sourced fact, preferably VisitKorea (english.visitkorea.or.kr),
Visit Seoul (english.visitseoul.net) or the venue's official website, stated "as of <month year>"):
{chr(10).join(lines)}

Route rules: on a procedure day, choose only indoor, low-activity places near where the reader is staying. On the day right after
a procedure (the observation day), use ONLY places marked "OK for the observation day". Places with strong sun, sauna/heat, hikes,
day trips or another city go on later days, and say to follow the treating clinic's aftercare advice. Group places by area to
limit travel time. {guide}Present places neutrally — no rankings or "best", no paid placements, no restaurant or shop promotions.
"""


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="관광지 카탈로그").parse_args(argv)
    catalog = load()
    for ok in (True, False):
        print("[관찰일 가능]" if ok else "[관찰일 불가 — 이후 일정]")
        for a in catalog.values():
            if a["observation_ok"] == ok:
                print(f"  {a['name']} ({a['zone']})")
    return 0


if __name__ == "__main__":
    run_cli("attractions", main)
