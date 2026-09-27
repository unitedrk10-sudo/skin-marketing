"""1. 주간 주제 선정 — Hermes 크론 (월 09:00, 에이전트 모드).

Gemini 검색 연동으로 주제 후보를 조사해 content/topics/<주차>.json 에 저장하고,
텔레그램으로 보낼 메시지를 stdout 에 출력한다. 사람이 번호를 고르면 02_draft 를 실행한다.

    python -m pipeline.01_topics [--week 2026-W40] [--count 6]
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
    args = parser.parse_args(argv)

    data = build(args.week, args.count)
    path = content_dir("topics") / f"{args.week}.json"
    save_json(path, data)
    log.info("주제 %d건 저장: %s", len(data["topics"]), path)
    print(telegram_message(args.week, data["topics"]))
    return 0


if __name__ == "__main__":
    run_cli("01_topics", main)
