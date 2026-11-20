"""연구 하나에 기댄 내용의 후속 글 (2026-10-22 사람 결정).

연구가 하나뿐이어도 그걸 근거로("One study…") 먼저 올리고, 나중에 같은 질문에 대한 다른 연구 —
뒷받침하는 쪽과 한계를 보이는 쪽 — 를 따로 정리해 올린다.

승인된 글의 사실 중 단일 연구(“a study”, “one trial”, “In a Korean randomized controlled trial” …, 리뷰·메타분석 제외)를
content/followups.json 에 모으고, 목요일 주제 조사(01_topics) 프롬프트에 후속 후보로 넣는다.
그 주제로 쓴 글(topic.followup_of)이 승인되면 닫는다.

    python -m pipeline.followups scan | list
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from pipeline.common import content_dir, draft_dirs, get_logger, load_draft, load_json, now_iso, run_cli, save_json

log = get_logger("followups")

STATES = ("approved", "rendered", "ready_to_publish", "published")
SINGLE = re.compile(r"\b(a|an|one|that|this|the same)\b[^.]{0,60}?\b(study|trial|case series|case report|pilot)\b", re.I)
POOLED = re.compile(r"\b(review|meta-analys|systematic|guideline|consensus)", re.I)
TRAVEL_KINDS = {"travel", "practice", "regulation"}


def file() -> Path:
    return content_dir() / "followups.json"


def load() -> dict:
    return load_json(file()) if file().exists() else {"items": {}}


def single_study(fact: dict) -> bool:
    text = fact.get("text", "")
    return fact.get("kind") not in TRAVEL_KINDS and bool(SINGLE.search(text)) and not POOLED.search(text)


def scan() -> list[str]:
    data = load()
    items = data["items"]
    added, closed = [], []
    done_refs = set()
    for state in STATES:
        for path in draft_dirs(state):
            if not (path / "draft.json").exists():
                continue
            d = load_draft(path)
            if ref := (d.get("topic") or {}).get("followup_of"):
                done_refs.add(ref)
            for f in d.get("facts", []):
                if not single_study(f):
                    continue
                key = hashlib.sha1(f"{f.get('url', '')}|{f['text']}".encode()).hexdigest()[:10]
                if key not in items:
                    items[key] = {"draft_id": d["id"], "title": d["blog"].get("title", ""), "fact_id": f["id"],
                                  "claim": f["text"], "url": f.get("url", ""), "found_at": now_iso(), "status": "open"}
                    added.append(key)
    for key in done_refs & set(items):
        if items[key]["status"] != "done":
            items[key].update(status="done", done_at=now_iso())
            closed.append(key)
    save_json(file(), data)
    return [f"단일 연구 근거 {len(added)}건 추가, {len(closed)}건 후속 글로 닫음 (열린 것 {sum(1 for i in items.values() if i['status'] == 'open')}건)"]


def prompt_block(limit: int = 5) -> str:
    items = sorted(((k, v) for k, v in load()["items"].items() if v["status"] == "open"), key=lambda x: x[1]["found_at"])
    if not items:
        return "(none)"
    return "\n".join(f"- [{k}] (from \"{v['title']}\") {v['claim']}" for k, v in items[:limit])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="단일 연구 후속 글 후보")
    ap.add_argument("cmd", choices=("scan", "list"))
    args = ap.parse_args(argv)
    print("\n".join(scan()) if args.cmd == "scan" else prompt_block(50))
    return 0


if __name__ == "__main__":
    run_cli("followups", main)
