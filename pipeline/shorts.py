"""숏폼 대본 — 블로그 글을 "한 영상 = 한 질문" 대본으로 쪼갠다 (2026-10-22).

주마다(블로그 게시 주 기준) 시술 글 → 월~금 질문 5개, 관광(시술 × 여행) 글 → 토요일 1편으로 합침, 일요일 쉼.
글이 1편뿐인 주는 다음 주와 합친다. 첫 주부터 순서대로 모두 만들어 두고 사람이 검수한다 (게시는 아직 안 함 —
화면 방식은 숏폼 실험 결과를 보고 정한다).

쓰기 = Claude (stage shorts_write, 사실 목록만) → 코드 규칙 검사(금지 표현·병원명·사실에 없는 수치·길이) →
검수 = Gemini (stage shorts_review, 다른 회사 — 사실 대조·과장·위험 안내 줄·한 질문, 한국어 번역 같이) → 텔레그램 사람 검수.

    python -m pipeline.shorts plan            # 아직 없는 주의 대본을 만들고 검수
    python -m pipeline.shorts list            # 검수 메시지 (번호 · 한국어)
    python -m pipeline.shorts apply "1,3 승인"  # 승인 / "2 반려" / "4 수정: 메모" (그 대본만 다시 써서 재검수)
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
from datetime import date
from pathlib import Path

from pipeline import llm
from pipeline.common import content_dir, facts_text, get_logger, load_draft, load_json, now_iso, prompt, run_cli, save_json

log = get_logger("shorts")

STATES = ("approved", "rendered", "ready_to_publish", "published")
TRAVEL = {"procedure_travel", "travel_guide"}
DAYS = ("월", "화", "수", "목", "금", "토")
MIN_WORDS, MAX_WORDS = 45, 85


class ShortsError(RuntimeError):
    pass


def plan_file() -> Path:
    return content_dir() / "shorts" / "plan.json"


def load_plan() -> dict:
    f = plan_file()
    return load_json(f) if f.exists() else {"weeks": [], "scripts": []}


def save_plan(plan: dict) -> None:
    save_json(plan_file(), plan)


def post_path(draft_id: str) -> Path | None:
    for state in STATES:
        p = content_dir(state) / draft_id
        if (p / "draft.json").exists():
            return p
    return None


def kind(draft: dict) -> str:
    return "travel" if (draft.get("axis") in TRAVEL or draft.get("content_type") in TRAVEL) else "procedure"


def weeks() -> list[list[str]]:
    """블로그 게시 일정 순서로 주 묶음 (글 1편뿐인 주는 다음 주와 합침)."""
    sched = load_json(content_dir() / "site" / "schedule.json") if (content_dir() / "site" / "schedule.json").exists() else {}
    posts = sorted(((v["date"], k) for k, v in sched.items() if post_path(k)), key=lambda x: x[0])
    groups: dict[tuple, list[str]] = {}
    for d, k in posts:
        groups.setdefault(date.fromisoformat(d).isocalendar()[:2], []).append(k)
    out, carry = [], []
    for key in sorted(groups):
        ids = carry + groups[key]
        if len(ids) < 2:
            carry = ids
            continue
        out.append(ids)
        carry = []
    if carry:
        out.append(carry)
    return out


def _facts(draft: dict) -> list[dict]:
    return importlib.import_module("pipeline.social").social_facts(draft)


def write(task: str, drafts: list[dict], note: str = "") -> list[dict]:
    facts, posts = [], []
    for i, d in enumerate(drafts):
        prefix = f"P{i + 1}-" if len(drafts) > 1 else ""
        posts.append(f"- {d['blog'].get('title', '')}")
        facts += [{**f, "id": prefix + f["id"]} for f in _facts(d)]
    text = prompt("shorts_write", task=task + (f"\nEditor's note for this rewrite: {note}" if note else ""),
                  posts="\n".join(posts), facts=facts_text(facts), min_words=str(MIN_WORDS), max_words=str(MAX_WORDS))
    data, _ = llm.generate_json("shorts_write", text)
    return [s for s in (data or {}).get("scripts") or [] if isinstance(s, dict) and s.get("lines")]


def all_facts(drafts: list[dict]) -> list[dict]:
    out = []
    for i, d in enumerate(drafts):
        prefix = f"P{i + 1}-" if len(drafts) > 1 else ""
        out += [{**f, "id": prefix + f["id"]} for f in _facts(d)]
    return out


def check(script: dict, facts: list[dict]) -> list[str]:
    social = importlib.import_module("pipeline.social")
    known = set(social.NUMBER.findall(" ".join(f.get("text", "") for f in facts)))
    ids = {f["id"] for f in facts}
    problems = []
    words = sum(len(str(ln.get("voice", "")).split()) for ln in script["lines"])
    if not MIN_WORDS - 5 <= words <= MAX_WORDS + 10:
        problems.append(f"길이 {words}단어 (권장 {MIN_WORDS}~{MAX_WORDS})")
    for n, ln in enumerate(script["lines"], 1):
        for field in ("voice", "caption"):
            problems += [f"{n}줄 {field}: {p}" for p in social.text_problems(str(ln.get(field, "")), known)]
    if [i for i in script.get("fact_ids") or [] if i not in ids]:
        problems.append("없는 사실 번호")
    if not script.get("fact_ids"):
        problems.append("근거 사실(fact_ids) 없음")
    return problems


def review(script: dict, facts: list[dict]) -> dict:
    used = [f for f in facts if f["id"] in set(script.get("fact_ids") or [])] or facts
    text = prompt("shorts_review", facts=facts_text(used), question=script.get("question", ""),
                  script="\n".join(f"{n}. voice: {ln.get('voice', '')} | caption: {ln.get('caption', '')}"
                                   for n, ln in enumerate(script["lines"], 1)))
    try:
        data, res = llm.generate_json("shorts_review", text)
    except llm.LLMError as e:
        return {"ok": False, "issues": [{"line": 0, "problem": f"검수 실패: {e}", "suggest": ""}], "ko": []}
    data = data if isinstance(data, dict) else {}
    return {"ok": bool(data.get("ok")), "issues": data.get("issues") or [], "ko": data.get("ko") or [],
            "model": getattr(res, "model", "")}


def _finish(item: dict, facts: list[dict]) -> dict:
    item["problems"] = check(item, facts)
    item["review"] = review(item, facts)
    item["status"] = "review" if not item["problems"] and item["review"]["ok"] else "flagged"
    item["checked_at"] = now_iso()
    return item


def build_week(w: int, ids: list[str]) -> list[dict]:
    drafts = {i: load_draft(post_path(i)) for i in ids}
    proc = [i for i in ids if kind(drafts[i]) == "procedure"]
    travel = [i for i in ids if kind(drafts[i]) == "travel"]
    source = proc or travel  # 시술 글이 없는 주는 관광 글에서 질문을 뽑는다
    per = math.ceil(5 / len(source)) if source else 0
    out, day = [], 0
    for i in source:
        want = min(per, 5 - day)
        if want <= 0:
            break
        task = (f"Write {want} separate scripts from this guide, each answering a different question a foreign visitor "
                f"would ask about it (how long / how it works / how often / recovery / what to plan).")
        for s in write(task, [drafts[i]])[:want]:
            out.append(_finish({"id": f"S{w}-{DAYS[day]}", "week": w, "day": DAYS[day], "posts": [i], "type": "question",
                                **{k: s.get(k) for k in ("question", "lines", "fact_ids")}}, all_facts([drafts[i]])))
            day += 1
    if travel and proc:
        ds = [drafts[i] for i in travel]
        task = ("Write 1 script that merges these travel guides into one practical answer: how to fit a skin treatment "
                "into a Korea trip (timing, sun, walking, recovery days), with one example from each guide.")
        s = (write(task, ds) or [None])[0]
        if s:
            out.append(_finish({"id": f"S{w}-토", "week": w, "day": "토", "posts": travel, "type": "travel",
                                **{k: s.get(k) for k in ("question", "lines", "fact_ids")}}, all_facts(ds)))
    return out


def make(limit_weeks: int | None = None) -> list[str]:
    plan = load_plan()
    done = {tuple(w["posts"]) for w in plan["weeks"]}
    made = []
    for w, ids in enumerate(weeks(), 1):
        if tuple(ids) in done:
            continue
        if limit_weeks is not None and len(made) >= limit_weeks:
            break
        scripts = build_week(w, ids)
        plan["weeks"].append({"week": w, "posts": ids, "made_at": now_iso()})
        plan["scripts"] += scripts
        save_plan(plan)
        made.append(f"{w}주차: 대본 {len(scripts)}개")
        log.info("숏폼 %d주차 — %d개", w, len(scripts))
    return made


def pending(plan: dict) -> list[dict]:
    return [s for s in plan["scripts"] if s.get("status") in ("review", "flagged")]


def list_message() -> str:
    plan = load_plan()
    items = pending(plan)
    if not items:
        return "숏폼 대본 검수 대기 없음"
    lines = ["[숏폼 대본 검수] 한 영상 = 한 질문 (20~30초). 답장: '숏폼 1,3 승인' / '숏폼 2 반려' / '숏폼 4 수정: 메모'", ""]
    for n, s in enumerate(items, 1):
        flag = "⚠️" if s["status"] == "flagged" else "✅"
        words = sum(len(ln["voice"].split()) for ln in s["lines"])
        lines.append(f"{n}. {flag} {s['id']} — {s.get('question', '')} ({words}단어)")
        ko = s.get("review", {}).get("ko") or []
        for i, ln in enumerate(s["lines"]):
            lines.append(f"   {i + 1}) {ko[i] if i < len(ko) else ln['voice']}  [{ln.get('caption', '')}]")
        for p in s.get("problems") or []:
            lines.append(f"   ↳ 규칙: {p}")
        for iss in s.get("review", {}).get("issues") or []:
            lines.append(f"   ↳ 검수({iss.get('line')}줄): {iss.get('problem')}" + (f" → {iss['suggest']}" if iss.get("suggest") else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def apply(text: str) -> str:
    """'1,3 승인' / '2 반려' / '4 수정: 메모' — 번호는 list_message 순서."""
    m = re.match(r"\s*(?:숏폼\s*)?([\d,\s]+)\s*(승인|반려|수정)\s*:?\s*(.*)$", text)
    if not m:
        raise ShortsError("형식: '1,3 승인' / '2 반려' / '4 수정: 메모'")
    nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
    plan = load_plan()
    items = pending(plan)
    out = []
    for n in nums:
        if not 1 <= n <= len(items):
            raise ShortsError(f"{n}번 없음 (대기 {len(items)}개)")
        s = items[n - 1]
        if m.group(2) == "승인":
            s.update(status="approved", decided_at=now_iso())
            out.append(f"✅ {s['id']} 승인")
        elif m.group(2) == "반려":
            s.update(status="rejected", decided_at=now_iso())
            out.append(f"🗑 {s['id']} 반려")
        else:
            drafts = [load_draft(post_path(i)) for i in s["posts"]]
            task = f"Rewrite this one script (same question: {s.get('question', '')}). Current script: " + \
                   json.dumps(s["lines"], ensure_ascii=False)
            new = (write(task, drafts, note=m.group(3).strip()) or [None])[0]
            if not new:
                raise ShortsError(f"{s['id']} 다시 쓰기 실패")
            s.setdefault("history", []).append({"lines": s["lines"], "note": m.group(3).strip(), "at": now_iso()})
            s.update(**{k: new.get(k) for k in ("question", "lines", "fact_ids")})
            _finish(s, all_facts(drafts))
            out.append(f"✏️ {s['id']} 다시 씀 → {'검수 통과' if s['status'] == 'review' else '⚠️ 다시 확인 필요'}")
    save_plan(plan)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="숏폼 대본 (한 영상 = 한 질문)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--weeks", type=int, default=None, help="이번에 만들 주 수 (기본: 남은 주 전부)")
    sub.add_parser("list")
    a = sub.add_parser("apply")
    a.add_argument("text")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        print("\n".join(make(args.weeks)) or "새로 만들 주 없음")
    elif args.cmd == "list":
        print(list_message())
    else:
        print(apply(args.text))
    return 0


if __name__ == "__main__":
    run_cli("shorts", main)
