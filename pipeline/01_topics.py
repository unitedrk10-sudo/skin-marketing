"""1. 주간 주제 선정 — Hermes 크론 (월 09:00, 에이전트 모드).

추천 순서는 수요 점수(pipeline.demand: 시술·관광지별 영어권 관심도 사전값 + 우리 블로그 조회수·링크 클릭) 기준.
Gemini 검색 연동으로 주제 후보를 조사해 content/topics/<주차>.json 에 저장하고,
텔레그램으로 보낼 메시지를 stdout 에 출력한다. 사람이 번호를 고르면 02_draft 를 실행한다.

    python -m pipeline.01_topics [--week 2026-W40] [--count 6]
    python -m pipeline.01_topics --from-seed 6     # config/seed_topics.yaml + 주제 은행에서 안 쓴 주제를 수요 점수 순으로
    python -m pipeline.01_topics --refill          # 주제 은행 채우기 (월 1회 크론): 유형마다 안 쓴 주제가 BANK_TARGET 개가 되게 Gemini 조사
"""

from __future__ import annotations

import argparse
import importlib
import re
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
PIN_BONUS = 10                  # seed 의 week: <주차> = 운영자가 그 주 후보로 지정 (2026-11-21) — 유형 쿼터 안에서 맨 앞
MAX_PER_FOCUS = 2               # 한 주에 같은 시술·관광지 주제는 최대 2개 (다양성)
# 한 원출처가 질문 전체에 이미 답하는 주제는 뒤로 (2026-11-20 — AI 답변은 그런 질문에 원출처를 직접 인용하고 우리 글을 건너뛴다)
ANSWERED_PENALTY = 0.3
EXTRA_CANDIDATES = 4            # Gemini 에게 더 받아 두고 쿼터(config/channels.yaml topic_quota)대로 고른다
# 주제 은행 (2026-11-27): seed 가 바닥난 유형은 Gemini 조사로 채워 content/topics/bank/ 에 쌓는다 (운영 데이터 — config 는 git 이라 안 건드림)
BANK_TARGET = 10                # 월 1회: 유형마다 안 쓴 주제를 이만큼 (주 2개 후보 × 약 5주)
WEEK_MIN = 2                    # 주간 선정 직전: 유형마다 후보 2개가 안 되면 그 유형만 바로 조사


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
        t["answered_elsewhere"] = t.get("answered_elsewhere") is True
        out.append(t)
    return out


def bank_path():
    return content_dir("topics") / "bank" / "topic_bank.json"  # 하위 폴더 — 주차 파일(*.json) 목록에 섞이지 않게


def seed_topics() -> list[dict]:
    """config/seed_topics.yaml (사람이 조사) + 주제 은행 (Gemini 가 월 1회 채움)."""
    bank = load_json(bank_path()).get("topics", []) if bank_path().exists() else []
    return (load_yaml("seed_topics.yaml").get("topics") or []) + bank


def unused_by_group() -> Counter:
    axes = load_yaml("channels.yaml")["content_axes"]
    used = used_seed_ids()
    return Counter(group_of(s) for s in seed_topics() if s["id"] not in used and s.get("axis") in axes)


def refill(target: int) -> list[dict]:
    """유형마다 안 쓴 주제가 target 개 미만이면 모자란 만큼 Gemini 로 조사해 주제 은행에 더한다. 더한 주제 목록."""
    have = unused_by_group()
    need = {name: (axes, target - have[name]) for name, axes, _ in quota(8) if have[name] < target}
    if not need:
        return []
    channels = load_yaml("channels.yaml")
    known = recent_titles() + [s["title"] for s in seed_topics()]
    text = prompt(
        "topic_research",
        today=date.today().isoformat(),
        week=f"{date.today():%Y-%m} topic bank (topics for the coming weeks)",
        count=str(sum(n for _, n in need.values())),
        quota="\n".join(f"- {', '.join(a)}: {n}" for a, n in need.values()),
        channels=str({k: channels[k] for k in ("audience", "shortform", "blog")}),
        recent_titles="\n".join(f"- {t}" for t in known) or "(none yet)",
        demand=demand.prompt_block(demand.all_scores()),
        followups="(none)",
    )
    data, _ = llm.generate_json("topics", text)
    topics = validate(data.get("topics", []) if isinstance(data, dict) else data,
                      [a for axes, _ in need.values() for a in axes])
    seen = {t.strip().lower() for t in known}
    left = {name: n for name, (_, n) in need.items()}
    bank = load_json(bank_path()) if bank_path().exists() else {"topics": []}
    ids = {s["id"] for s in seed_topics()}
    added = []
    for t in topics:
        g, key = group_of(t), t["title"].strip().lower()
        if left.get(g, 0) <= 0 or key in seen:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")[:50]
        tid = next(f"bank-{slug}" + (f"-{i}" if i else "") for i in range(100) if f"bank-{slug}" + (f"-{i}" if i else "") not in ids)
        entry = {"id": tid, "wave": 2, "added": date.today().isoformat(),
                 **{k: t[k] for k in ("axis", "title", "angle", "why_now", "keywords", "hook", "has_price",
                                      "answered_elsewhere", "sources") if k in t}}
        bank["topics"].append(entry)
        added.append(entry)
        ids.add(tid)
        seen.add(key)
        left[g] -= 1
    save_json(bank_path(), bank)
    log.info("주제 은행 %d건 추가 (모자란 유형: %s)", len(added), ", ".join(need))
    return added


def bank_message(added: list[dict]) -> str:
    have = unused_by_group()
    counts = " · ".join(f"{GROUP_KO.get(name, name)} {have[name]}" for name, _, _ in quota(8))
    lines = [f"🗂 주제 은행: {len(added)}건 추가 — 유형별 남은 주제 {counts}"]
    lines += [f"- [{GROUP_KO.get(group_of(t), t['axis'])}] {t['title']}" for t in added]
    return "\n".join(lines)


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
        if t.get("answered_elsewhere"):
            score -= ANSWERED_PENALTY
            why = f"{why} · 원출처가 이미 답함 -{ANSWERED_PENALTY}"
        t["demand"] = {"score": round(score + (bonus or {}).get(i, 0), 3), "focus": focus, "reason": why}
    return sorted(topics, key=lambda t: -t["demand"]["score"])  # 동점은 원래 순서


def quota(count: int) -> list[tuple[str, list[str], int]]:
    """config/channels.yaml topic_quota → [(이름, 축 목록, 개수)]. 합이 count 와 다르면 비율대로 맞춘다."""
    groups = [(name, g["axes"], int(g["count"])) for name, g in (load_yaml("channels.yaml").get("topic_quota") or {}).items()]
    total = sum(n for _, _, n in groups)
    if not groups or total == count:
        return groups
    # 비율대로 나누고 남는 자리는 나머지가 큰 그룹부터, 같으면 앞 그룹부터 (2026-11-26 — 그룹이 4개가 되자 반올림 차이를
    # 첫 그룹에서 빼던 방식은 3개를 고를 때 시술 정보를 0개로 만들었다)
    exact = [n * count / total for _, _, n in groups]
    shares = [int(x) for x in exact]
    for i in sorted(range(len(groups)), key=lambda i: (shares[i] - exact[i], i))[:count - sum(shares)]:
        shares[i] += 1
    return [(name, axes, k) for (name, axes, _), k in zip(groups, shares)]


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
    seeds = seed_topics()
    used = used_seed_ids()
    fresh = [s for s in seeds if s["id"] not in used]
    if not fresh:
        return None
    candidates = []
    for s in fresh:
        topic = {k: v for k, v in s.items() if k not in ("id", "wave", "week", "added")}
        topic["sources"] = [x if isinstance(x, dict) else {"url": x, "title": ""} for x in s.get("sources", [])]
        topic["seed_id"] = s["id"]
        candidates.append(topic)
    candidates = validate(candidates, axes)  # 다루지 않는 축은 여기서 빠진다
    tables = tables or demand.all_scores()
    wave = {s["id"]: s.get("wave", 9) for s in fresh}  # 필터 뒤에도 주제에 맞게 붙도록 id 로 찾는다
    pinned = {s["id"] for s in fresh if str(s.get("week", "")) == week}
    bonus = {i: WAVE_BONUS.get(wave[t["seed_id"]], 0) + (PIN_BONUS if t["seed_id"] in pinned else 0)
             for i, t in enumerate(candidates)}
    ranked = annotate(candidates, tables, bonus)
    for t in ranked:
        if t["seed_id"] in pinned:
            t["demand"]["reason"] += " · 📌 운영자 지정"
            t["demand"]["pinned"] = True
    return {"week": week, "created_at": now_iso(), "model": "seed", "grounding_urls": [],
            "topics": pick(ranked, count)}


def day_slots() -> list[tuple[str, str]]:
    """[(요일 한글, topic_quota 이름)] — site.yaml publish_schedule 의 요일 순서."""
    rule = load_yaml("site.yaml").get("publish_schedule") or {}
    slots = {d.lower()[:3]: str(g) for d, g in (rule.get("slots") or {}).items()}
    return [(DAY_KO.get(d.lower()[:3], d), slots[d.lower()[:3]]) for d in rule.get("days") or [] if d.lower()[:3] in slots]


def group_of(topic: dict) -> str | None:
    return next((name for name, axes, _ in quota(8) if topic.get("axis") in axes), None)


def by_day(topics: list[dict]) -> list[dict]:
    """게시 요일 순으로 묶는다 (유형 안에서는 수요 점수 순) — 번호가 저장 순서라 저장 전에 정렬한다 (2026-11-26).
    요일 칸이 없는 유형은 맨 뒤."""
    order = {g: i for i, (_, g) in enumerate(day_slots())}
    return sorted(topics, key=lambda t: (order.get(group_of(t), len(order)), -t.get("demand", {}).get("score", 0)))


def telegram_message(week: str, topics: list[dict]) -> str:
    """요일마다 후보 2개 중 하나를 고르는 형식 (2026-11-26). 번호는 저장된 목록 순서 그대로."""
    days = {g: d for d, g in day_slots()}
    firsts = list(dict.fromkeys(group_of(t) for t in topics))
    example = ",".join(str(next(n for n, x in enumerate(topics, 1) if group_of(x) == g)) for g in firsts)
    lines = [f"[{week} 주제 후보 {len(topics)}건] 요일마다 하나씩 번호로 골라주세요 (예: {example})"]
    head = None
    for i, t in enumerate(topics, 1):
        g = group_of(t)
        if (g, days.get(g)) != head:
            head = (g, days.get(g))
            nums = [n for n, x in enumerate(topics, 1) if group_of(x) == g]
            label = f"{days[g]}요일 · {GROUP_KO.get(g, g)}" if g in days else "요일 칸 없음"
            pick = f"{' / '.join(map(str, nums))} 중 하나" if len(nums) > 1 else f"{nums[0]}번 (후보 1개)"
            lines.append(f"\n📅 {label} — {pick}")
        price = (" 💲가격" if t["has_price"] else "") + (" 📚원출처 있음" if t.get("answered_elsewhere") else "")
        d = t.get("demand")
        shown = d["score"] - PIN_BONUS if d and d.get("pinned") else (d or {}).get("score", 0)  # 지정 가산점은 순서용 — 수요 표시에선 뺀다
        score = f"\n   └ 📈 수요 {shown:.2f} ({d['reason']})" if d else ""
        lines.append(f"{i}. {t['title']} ({t['axis']}){price}\n   └ {t['angle']}\n   └ 근거: {t.get('why_now', '')}{score}")
    return "\n".join(lines)


DAY_KO = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}
GROUP_KO = {"info": "시술 정보", "before_you_book": "병원 이용법", "procedure_travel": "시술 × 여행", "skincare": "화장품"}


def schedule_hint() -> str:
    """주제 후보 끝 안내 — 게시 요일·편수는 site.yaml publish_schedule 에서 (2026-11-26 화요일 칸 추가로 주 4편)."""
    days = [d.lower()[:3] for d in ((load_yaml("site.yaml").get("publish_schedule") or {}).get("days") or ["mon", "wed", "fri"])]
    return (f"📅 다음 주 블로그 {'·'.join(DAY_KO.get(d, d) for d in days)} {len(days)}편 — 유형마다 1개씩 {len(days)}개 골라 "
            "주시면 금요일까지 초안이 오고, 주말에 검수하면 요일 칸에 맞춰 올라갑니다.")


def followups_block() -> str:
    """단일 연구 근거 후속 후보 (pipeline/followups.py) — 실패해도 주제 조사는 계속."""
    try:
        fu = importlib.import_module("pipeline.followups")
        fu.scan()
        return fu.prompt_block()
    except Exception as e:  # noqa: BLE001
        log.warning("후속 후보 실패: %s", e)
        return "(none)"


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
        followups=followups_block(),
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
                        help="다음 주 게시용 (목요일 실행 → 주말 검수 → site.yaml 게시 요일, 2026-10-10)")
    parser.add_argument("--count", type=int, default=load_yaml("channels.yaml").get("topics_per_week", 6))
    parser.add_argument("--from-seed", type=int, metavar="N", help="조사해 둔 주제(seed + 주제 은행) N개 사용")
    parser.add_argument("--refill", action="store_true", help=f"주제 은행 채우기 — 유형마다 안 쓴 주제 {BANK_TARGET}개 (월 1회)")
    args = parser.parse_args(argv)
    if args.refill:
        print(bank_message(refill(BANK_TARGET)))
        return 0
    if args.next_week:
        today = date.today()
        args.week = iso_week(today + timedelta(days=7 - today.weekday()))  # 다음 월요일의 주

    scan = trends.ensure_fresh()  # 관광지 트렌드 주 1회 스캔 (실패해도 계속)
    # 크론은 --from-seed 6 을 넘긴다 — 칸이 늘면(2026-11-26 화요일 칸) channels.yaml topics_per_week 가 더 크므로 큰 쪽
    added = []
    if args.from_seed:
        try:  # 요일마다 후보 2개 (2026-11-27) — 모자란 유형만 바로 조사, 실패해도 있는 후보로 진행
            added = refill(WEEK_MIN)
        except llm.LLMError as e:
            log.warning("주제 은행 보충 실패 — 있는 후보로 진행: %s", e)
    data = from_seed(args.week, max(args.from_seed, args.count)) if args.from_seed else None
    if data is None:
        if args.from_seed:
            log.info("초기 주제 목록을 모두 써서 Gemini 조사로 전환")
        data = build(args.week, args.count)
    data["topics"] = by_day(data["topics"])  # 요일별 묶음 = 텔레그램 번호 순서
    path = content_dir("topics") / f"{args.week}.json"
    save_json(path, data)
    log.info("주제 %d건 저장: %s", len(data["topics"]), path)
    print(telegram_message(args.week, data["topics"]))
    if args.next_week:
        print("\n" + schedule_hint())
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
