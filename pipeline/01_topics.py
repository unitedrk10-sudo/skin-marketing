"""1. 주간 주제 선정 — Hermes 크론 (월 09:00, 에이전트 모드).

Gemini 검색 연동으로 주제 후보를 조사해 content/topics/<주차>.json 에 저장하고,
텔레그램으로 보낼 메시지를 stdout 에 출력한다. 사람이 번호를 고르면 02_draft 를 실행한다.

    python -m pipeline.01_topics [--week 2026-W40] [--count 6]
    python -m pipeline.01_topics --from-seed 6     # config/seed_topics.yaml 에서 안 쓴 주제를 wave 순으로 (Gemini 호출 없음)
"""

from __future__ import annotations

import argparse
from datetime import date

from pipeline import llm
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
        if t.get("axis") not in axes:
            log.warning("알 수 없는 축 %r → procedure 로 처리", t.get("axis"))
            t["axis"] = "procedure"
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


def from_seed(week: str, count: int) -> dict | None:
    """조사해 둔 초기 주제 목록에서 아직 안 쓴 것을 wave·순서대로 꺼낸다. 다 썼으면 None."""
    axes = load_yaml("channels.yaml")["content_axes"]
    seeds = load_yaml("seed_topics.yaml").get("topics", [])
    used = used_seed_ids()
    fresh = sorted((s for s in seeds if s["id"] not in used), key=lambda s: s.get("wave", 9))  # 같은 wave 는 파일 순서
    if not fresh:
        return None
    picked = []
    for s in fresh[:count]:
        topic = {k: v for k, v in s.items() if k not in ("id", "wave")}
        topic["sources"] = [x if isinstance(x, dict) else {"url": x, "title": ""} for x in s.get("sources", [])]
        topic["seed_id"] = s["id"]
        picked.append(topic)
    return {"week": week, "created_at": now_iso(), "model": "seed", "grounding_urls": [], "topics": validate(picked, axes)}


def telegram_message(week: str, topics: list[dict]) -> str:
    lines = [f"[{week} 주제 후보 {len(topics)}건] 번호로 골라주세요 (예: 1,3,4)"]
    for i, t in enumerate(topics, 1):
        price = " 💲가격" if t["has_price"] else ""
        lines.append(f"{i}. {t['title']} ({t['axis']}){price}\n   └ {t['angle']}\n   └ 근거: {t.get('why_now', '')}")
    return "\n".join(lines)


def build(week: str, count: int) -> dict:
    channels = load_yaml("channels.yaml")
    axes = channels["content_axes"]
    recent = recent_titles()
    text = prompt(
        "topic_research",
        today=date.today().isoformat(),
        week=week,
        count=str(count),
        axes="\n".join(f"- {a}" for a in axes),
        channels=str({k: channels[k] for k in ("audience", "shortform", "blog")}),
        recent_titles="\n".join(f"- {t}" for t in recent) or "(none yet)",
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
        "topics": topics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주간 주제 후보 생성")
    parser.add_argument("--week", default=iso_week())
    parser.add_argument("--count", type=int, default=load_yaml("channels.yaml").get("topics_per_week", 6))
    parser.add_argument("--from-seed", type=int, metavar="N", help="조사해 둔 초기 주제 N개 사용 (Gemini 호출 없음)")
    args = parser.parse_args(argv)

    data = from_seed(args.week, args.from_seed) if args.from_seed else None
    if data is None:
        if args.from_seed:
            log.info("초기 주제 목록을 모두 써서 Gemini 조사로 전환")
        data = build(args.week, args.count)
    path = content_dir("topics") / f"{args.week}.json"
    save_json(path, data)
    log.info("주제 %d건 저장: %s", len(data["topics"]), path)
    print(telegram_message(args.week, data["topics"]))
    return 0


if __name__ == "__main__":
    run_cli("01_topics", main)
