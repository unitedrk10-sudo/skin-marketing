"""1. 주간 주제 선정 — Hermes 크론 (월 09:00, 에이전트 모드).

추천 순서는 수요 점수(pipeline.demand: 시술·관광지별 영어권 관심도 사전값 + 우리 블로그 조회수·링크 클릭) 기준.
Gemini 검색 연동으로 주제 후보를 조사해 content/topics/<주차>.json 에 저장하고,
텔레그램으로 보낼 메시지를 stdout 에 출력한다. 사람이 번호를 고르면 02_draft 를 실행한다.

    python -m pipeline.01_topics [--week 2026-W40] [--count 6]
    python -m pipeline.01_topics --from-seed 6     # config/seed_topics.yaml 에서 안 쓴 주제를 수요 점수 순으로 (Gemini 호출 없음)
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta

from pipeline import demand, llm, trends
from pipeline.common import (
    STATES,
    content_dir,
    draft_dirs,
    get_logger,
    iso_week,
    load_draft,
    load_json,
    load_yaml,
    now_iso,
    prompt,
    run_cli,
    save_json,
)

log = get_logger("01_topics")
RECENT_LIMIT = 60
WAVE_BONUS = {1: 0.3, 2: 0.1}   # seed 의 wave(시의성·기초 주제)는 가산점으로만 — 순서는 수요 점수가 정한다
MAX_PER_FOCUS = 2               # 한 주에 같은 시술·관광지 주제는 최대 2개 (다양성)
EXTRA_CANDIDATES = 4            # Gemini 에게 더 받아 두고 쿼터(config/channels.yaml topic_quota)대로 고른다


def recent_titles() -> list[str]:
    """이미 다룬 주제 (중복 방지): 과거 주제 후보 + 모든 상태의 초안."""
    titles: list[str] = []
    for f in sorted(content_dir("topics").glob("*.json"), reverse=True)[:8]:
        titles += [t["title"] for t in load_json(f).get("topics", [])]
    for state in STATES[1:]:
        titles += [load_draft(d)["topic"]["title"] for d in draft_dirs(state)]
    return list(dict.fromkeys(titles))[:RECENT_LIMIT]


def validate(topics: list[dict], axes: list[str]) -> list[dict]:
    out = []
    for t in topics:
        if not t.get("title") or not t.get("angle"):
            log.warning("제목·앵글 없는 주제 제외: %s", t)
            continue
        if not t.get("axis"):
            t["axis"] = "procedure"
        if t["axis"] not in axes:  # 다루지 않기로 한 축 (예: 관광지만 다루는 travel_guide)
            log.info("다루지 않는 축 %r 주제 제외: %s", t["axis"], t["title"])
            continue
        t.setdefault("keywords", [])
        t.setdefault("sources", [])
        t["has_price"] = bool(t.get("has_price"))
        out.append(t)
    return out


def used_seed_ids() -> set[str]:
    """초안까지 만든 주제, 또는 두 번 제안했는데 안 고른 주제는 다시 내지 않는다."""
    drafted = {load_draft(d)["topic"].get("seed_id") for state in STATES[1:] for d in draft_dirs(state)}
    offered: dict[str, int] = {}
    for f in content_dir("topics").glob("*.json"):
        for t in load_json(f).get("topics", []):
            if t.get("seed_id"):
                offered[t["seed_id"]] = offered.get(t["seed_id"], 0) + 1
    return {i for i in drafted if i} | {i for i, n in offered.items() if n >= 2}


def annotate(topics: list[dict], tables: dict[str, dict[str, dict]], bonus: dict[int, float] | None = None) -> list[dict]:
    """주제마다 수요 점수·근거를 붙이고 점수 순으로 정렬한다 (bonus: 주제 index → 가산점)."""
    for i, t in enumerate(topics):
        score, focus, why = demand.topic_score(t, tables)
        t["demand"] = {"score": round(score + (bonus or {}).get(i, 0), 3), "focus": focus, "reason": why}
    return sorted(topics, key=lambda t: -t["demand"]["score"])  # 동점은 원래 순서


def quota(count: int) -> list[tuple[str, list[str], int]]:
    """config/channels.yaml topic_quota → [(이름, 축 목록, 개수)]. 합이 count 와 다르면 비율대로 맞춘다."""
    groups = [(name, g["axes"], int(g["count"])) for name, g in (load_yaml("channels.yaml").get("topic_quota") or {}).items()]
    total = sum(n for _, _, n in groups)
    if not groups or total == count:
        return groups
    scaled = [(name, axes, round(n * count / total)) for name, axes, n in groups]
    name, axes, n = scaled[0]  # 반올림 차이는 첫 그룹(시술 정보)에서 맞춘다
    return [(name, axes, n + count - sum(x[2] for x in scaled))] + scaled[1:]


def pick(topics: list[dict], count: int) -> list[dict]:
    """유형별 쿼터만큼 점수 순으로 고르고(같은 시술·관광지는 MAX_PER_FOCUS 개까지), 모자라면 나머지에서 채운다.
    결과는 수요 점수 순."""
    picked, per = [], Counter()

    def take(pool: list[dict], n: int, limit: bool = True) -> None:
        for t in pool:
            if n <= 0:
                return
            focus = t["demand"]["focus"]
            if t in picked or (limit and per[focus] >= MAX_PER_FOCUS and not focus.endswith(":other")):
                continue
            picked.append(t)
            per[focus] += 1
            n -= 1

    for _name, axes, n in quota(count):
        take([t for t in topics if t.get("axis") in axes], n)
    take(topics, count - len(picked))                 # 한 유형 후보가 모자라면 다른 유형으로
    take(topics, count - len(picked), limit=False)    # 그래도 모자라면 같은 시술·관광지 제한 없이
    return sorted(picked, key=lambda t: -t["demand"]["score"])


def from_seed(week: str, count: int, tables: dict[str, dict[str, dict]] | None = None) -> dict | None:
    """조사해 둔 초기 주제 목록에서 아직 안 쓴 것을 수요 점수(+wave 가산점) 순으로 꺼낸다. 다 썼으면 None."""
    axes = load_yaml("channels.yaml")["content_axes"]
    seeds = load_yaml("seed_topics.yaml").get("topics", [])
    used = used_seed_ids()
    fresh = [s for s in seeds if s["id"] not in used]
    if not fresh:
        return None
    candidates = []
    for s in fresh:
        topic = {k: v for k, v in s.items() if k not in ("id", "wave")}
        topic["sources"] = [x if isinstance(x, dict) else {"url": x, "title": ""} for x in s.get("sources", [])]
        topic["seed_id"] = s["id"]
        candidates.append(topic)
    candidates = validate(candidates, axes)  # 다루지 않는 축은 여기서 빠진다
    tables = tables or demand.all_scores()
    wave = {s["id"]: s.get("wave", 9) for s in fresh}  # 필터 뒤에도 주제에 맞게 붙도록 id 로 찾는다
    bonus = {i: WAVE_BONUS.get(wave[t["seed_id"]], 0) for i, t in enumerate(candidates)}
    ranked = annotate(candidates, tables, bonus)
    return {"week": week, "created_at": now_iso(), "model": "seed", "grounding_urls": [],
            "topics": pick(ranked, count)}


def telegram_message(week: str, topics: list[dict]) -> str:
    lines = [f"[{week} 주제 후보 {len(topics)}건 — 수요 높은 순] 번호로 골라주세요 (예: 1,3,4)"]
    for i, t in enumerate(topics, 1):
        price = " 💲가격" if t["has_price"] else ""
        d = t.get("demand")
        score = f"\n   └ 📈 수요 {d['score']:.2f} ({d['reason']})" if d else ""
        lines.append(f"{i}. {t['title']} ({t['axis']}){price}\n   └ {t['angle']}\n   └ 근거: {t.get('why_now', '')}{score}")
    return "\n".join(lines)


def build(week: str, count: int) -> dict:
    channels = load_yaml("channels.yaml")
    axes = channels["content_axes"]
    recent = recent_titles()
    tables = demand.all_scores()
    asked = count + EXTRA_CANDIDATES  # 쿼터에 맞춰 고를 여유분
    text = prompt(
        "topic_research",
        today=date.today().isoformat(),
        week=week,
        count=str(asked),
        quota="\n".join(f"- {', '.join(a)}: {round(n * asked / count)}+" for _, a, n in quota(count)),
        channels=str({k: channels[k] for k in ("audience", "shortform", "blog")}),
        recent_titles="\n".join(f"- {t}" for t in recent) or "(none yet)",
        demand=demand.prompt_block(tables),
    )
    data, result = llm.generate_json("topics", text)
    topics = validate(data.get("topics", []) if isinstance(data, dict) else data, axes)
    if not topics:
        raise llm.LLMError("유효한 주제가 없습니다")
    return {
        "week": week,
        "created_at": now_iso(),
        "model": result.model,
        "grounding_urls": result.grounding_urls,
        "topics": pick(annotate(topics, tables), count),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주간 주제 후보 생성")
    parser.add_argument("--week", default=iso_week())
    parser.add_argument("--next-week", action="store_true",
                        help="다음 주 게시용 (목요일 실행 → 주말 검수 → 월·수·금 게시, 2026-10-10)")
    parser.add_argument("--count", type=int, default=load_yaml("channels.yaml").get("topics_per_week", 6))
    parser.add_argument("--from-seed", type=int, metavar="N", help="조사해 둔 초기 주제 N개 사용 (Gemini 호출 없음)")
    args = parser.parse_args(argv)
    if args.next_week:
        today = date.today()
        args.week = iso_week(today + timedelta(days=7 - today.weekday()))  # 다음 월요일의 주

    scan = trends.ensure_fresh()  # 관광지 트렌드 주 1회 스캔 (실패해도 계속)
    data = from_seed(args.week, args.from_seed) if args.from_seed else None
    if data is None:
        if args.from_seed:
            log.info("초기 주제 목록을 모두 써서 Gemini 조사로 전환")
        data = build(args.week, args.count)
    path = content_dir("topics") / f"{args.week}.json"
    save_json(path, data)
    log.info("주제 %d건 저장: %s", len(data["topics"]), path)
    print(telegram_message(args.week, data["topics"]))
    if args.next_week:
        print("\n📅 다음 주 블로그 월·수·금 3편 — 3개 골라 주시면 금요일까지 초안이 오고, 주말에 검수하면 월요일부터 순서대로 올라갑니다.")
    if scan and (scan["attractions"] or scan["emerging"] or scan.get("food")):
        hot = sorted(scan["attractions"].items(), key=lambda x: -x[1]["trend"])[:3]
        names = load_yaml("attractions.yaml").get("attractions") or {}
        line = ", ".join(f"{names.get(a, {}).get('name', a)} {t['trend']}" for a, t in hot)
        new = ", ".join(e["name"] for e in scan["emerging"][:3])
        food = ", ".join(f["name"] for f in scan.get("food", [])[:3])
        print(f"🔥 관광지 트렌드: {line or '-'}" + (f" / 신규: {new}" if new else "") + (f" / 🍜 {food}" if food else ""))
    return 0


if __name__ == "__main__":
    run_cli("01_topics", main)
