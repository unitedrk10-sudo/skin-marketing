"""숏폼 대본 — 블로그 글을 "한 영상 = 한 질문" 대본으로 쪼갠다 (2026-10-22).

주마다(블로그 게시 주 기준) 7편 (2026-10-22 변경): 관광 글 하나당 관광 영상 1편(관광 내용만), 나머지는 시술 질문 —
시술 글은 작동 원리·연구가 말하는 결과를 반드시 포함, 관광 글의 시술 쪽 질문(예: 가을 햇빛에 나가도 되나)도 시술 질문으로.
글이 1편뿐인 주는 다음 주와 합친다. 재료 = 사실 목록 + 검수·승인된 블로그 본문(blog.md). 첫 주부터 순서대로 모두 만들어 두고 사람이 검수한다 (게시는 아직 안 함 —
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
DAYS = ("월", "화", "수", "목", "금", "토", "일")
PER_WEEK = 7
GUIDE_CHARS = 9000  # 블로그 본문을 대본 재료로 넣는 길이
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


def guide_text(drafts: list[dict]) -> str:
    """검수·사람 승인을 거친 블로그 본문 — 대본은 이 문장을 최대한 그대로 쓴다 (사실 목록만으로는 관광 내용이 거의 없다)."""
    out = []
    for i, d in enumerate(drafts):
        path = post_path(d.get("id", ""))
        body = (path / "blog.md").read_text(encoding="utf-8") if path and (path / "blog.md").exists() else ""
        tag = f"[P{i + 1}] " if len(drafts) > 1 else ""
        out.append(f"{tag}{d['blog'].get('title', '')}\n{body[:GUIDE_CHARS]}")
    return "\n\n---\n\n".join(out)


KIND_RULES = {
    "procedure": ("This is a TREATMENT script. It must carry one line with the main risk or limit (side effects, who should not, "
                  "results vary) and end by pointing to a licensed doctor or the full guide."),
    "travel": ("This is a TRAVEL script: only travel content — the place, what to see or eat, best time to go, opening hours, access, "
               "walking, crowds, weather. Do NOT mention any skin treatment, recovery or medical advice at all. End with the full "
               "guide line. No risk line is needed."),
}


def write(task: str, drafts: list[dict], note: str = "", kind_: str = "procedure") -> list[dict]:
    facts, posts = [], []
    for i, d in enumerate(drafts):
        prefix = f"P{i + 1}-" if len(drafts) > 1 else ""
        posts.append(f"- {d['blog'].get('title', '')}")
        facts += [{**f, "id": prefix + f["id"]} for f in _facts(d)]
    text = prompt("shorts_write", task=task + (f"\nEditor's note for this rewrite: {note}" if note else ""),
                  posts="\n".join(posts), facts=facts_text(facts), guide=guide_text(drafts), kind_rules=KIND_RULES[kind_],
                  min_words=str(MIN_WORDS), max_words=str(MAX_WORDS))
    data, _ = llm.generate_json("shorts_write", text)
    return [s for s in (data or {}).get("scripts") or [] if isinstance(s, dict) and s.get("lines")]


def all_facts(drafts: list[dict]) -> list[dict]:
    out = []
    for i, d in enumerate(drafts):
        prefix = f"P{i + 1}-" if len(drafts) > 1 else ""
        out += [{**f, "id": prefix + f["id"]} for f in _facts(d)]
    return out


def check(script: dict, facts: list[dict], guide: str = "") -> list[str]:
    social = importlib.import_module("pipeline.social")
    known = set(social.NUMBER.findall(" ".join(f.get("text", "") for f in facts) + " " + guide))
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
    if not script.get("fact_ids") and script.get("type") != "travel":
        problems.append("근거 사실(fact_ids) 없음")
    return problems


def review(script: dict, facts: list[dict], titles: str = "", guide: str = "") -> dict:
    used = [f for f in facts if f["id"] in set(script.get("fact_ids") or [])] or facts
    balance = ("It is a TRAVEL script: it must contain no treatment or medical content; no risk line is needed."
               if script.get("type") == "travel" else
               "It is a TREATMENT script: it must contain at least one risk / side effect / limit line and end by pointing to a "
               "licensed doctor or the full guide.")
    text = prompt("shorts_review", facts=facts_text(used), question=script.get("question", ""), title=titles,
                  guide=guide, balance=balance,
                  script="\n".join(f"{n}. voice: {ln.get('voice', '')} | caption: {ln.get('caption', '')}"
                                   for n, ln in enumerate(script["lines"], 1)))
    try:
        data, res = llm.generate_json("shorts_review", text)
    except llm.LLMError as e:
        return {"ok": False, "issues": [{"line": 0, "problem": f"검수 실패: {e}", "suggest": ""}], "ko": []}
    data = data if isinstance(data, dict) else {}
    return {"ok": bool(data.get("ok")), "issues": data.get("issues") or [], "ko": data.get("ko") or [],
            "model": getattr(res, "model", "")}


def _finish(item: dict, facts: list[dict], drafts: list[dict] | None = None, autofix: bool = True) -> dict:
    """검사 + 검수. 걸리면 사람에게 보내기 전에 지적 내용으로 한 번 고쳐 다시 검사·검수 (2026-10-22, 02b 자동 수정과 같은 방식)."""
    guide = guide_text(drafts) if drafts else ""
    item["problems"] = check(item, facts, guide)
    item["review"] = review(item, facts, " / ".join(d["blog"].get("title", "") for d in drafts or []), guide)
    item["status"] = "review" if not item["problems"] and item["review"]["ok"] else "flagged"
    item["checked_at"] = now_iso()
    if item["status"] == "flagged" and autofix and drafts and not item.get("autofixed"):
        notes = item["problems"] + [f"line {i.get('line')}: {i.get('problem')} -> {i.get('suggest', '')}"
                                    for i in item["review"].get("issues") or []]
        task = (f"Fix this one script (same question: {item.get('question', '')}). Change only what the notes point at; "
                f"keep everything else. Current script: " + json.dumps(item["lines"], ensure_ascii=False))
        new = (write(task, drafts, note="; ".join(notes), kind_=_kind(item)) or [None])[0]
        if new:
            item.setdefault("history", []).append({"lines": item["lines"], "note": "자동 수정: " + "; ".join(notes)[:500],
                                                   "at": now_iso()})
            item.update(**{k: new.get(k) for k in ("question", "lines", "fact_ids")}, autofixed=True)
            return _finish(item, facts, drafts, autofix=False)
    return item


def refix() -> list[str]:
    """이미 만든 대본 중 걸린 것(아직 자동 수정 안 한 것)을 자동 수정·재검수."""
    plan = load_plan()
    out = []
    for s in plan["scripts"]:
        if s.get("status") == "flagged" and not s.get("autofixed"):
            drafts = [load_draft(post_path(i)) for i in s["posts"]]
            _finish(s, all_facts(drafts), drafts)
            save_plan(plan)
            out.append(f"{s['id']}: {'✅ 통과' if s['status'] == 'review' else '⚠️ 여전히 확인 필요'}")
    return out


def destinations(draft: dict) -> list[str]:
    """관광 글이 다루는 여행지 (config/attractions.yaml 키워드가 제목에 나오는 순서) — 여러 곳이면 영상도 곳마다 1편
    (2026-10-22: '제주 1편, 부산 1편'). 못 찾으면 글 하나 = 영상 하나."""
    title = (draft.get("topic") or {}).get("title") or draft["blog"].get("title", "")
    low = title.lower()
    found = []
    for a in importlib.import_module("pipeline.attractions").load().values():
        if a.get("heat"):  # 사우나·찜질방은 장소가 아니라 활동 — 여행지로 세지 않는다
            continue
        hits = [low.find(k.lower()) for k in a.get("keywords") or [] if k.lower() in low]
        if hits:
            found.append((min(hits), a["name"]))
    names = [n for _, n in sorted(found)]
    return names if len(names) > 1 else []


def _kind(item: dict) -> str:
    return "travel" if item.get("type") == "travel" else "procedure"


def _scripts(task: str, draft_ids: list[str], drafts: dict, want: int, typ: str) -> list[dict]:
    ds = [drafts[i] for i in draft_ids]
    got = write(task, ds, kind_="travel" if typ == "travel" else "procedure")[:want]
    return [{"type": typ, "posts": draft_ids, **{k: s.get(k) for k in ("question", "lines", "fact_ids")}} for s in got]


def build_week(w: int, ids: list[str]) -> list[dict]:
    """한 주 7편: 관광 글마다 관광 영상 1편, 나머지는 시술 질문 (시술 글 우선, 모자라면 관광 글의 시술 쪽 질문)."""
    drafts = {i: load_draft(post_path(i)) for i in ids}
    proc = [i for i in ids if kind(drafts[i]) == "procedure"]
    travel = [i for i in ids if kind(drafts[i]) == "travel"]
    places = {i: destinations(drafts[i]) or [""] for i in travel}  # 관광 영상 = 글마다, 여러 여행지면 곳마다
    n_proc = max(0, PER_WEEK - sum(len(v) for v in places.values()))
    cross = min(len(travel), max(0, n_proc - 3 * len(proc))) if proc else min(len(travel), n_proc)  # 시술 글은 최소 3편
    per_cross = math.ceil((n_proc if not proc else cross) / cross) if cross else 0
    per_proc = math.ceil((n_proc - cross * (per_cross if not proc else 1)) / len(proc)) if proc else 0
    items: list[dict] = []
    left = n_proc
    for i in proc:
        want = min(per_proc, left)
        if want <= 0:
            break
        task = (f"Write {want} separate TREATMENT scripts from this guide, each answering a different question a foreign "
                f"visitor asks. Required among them: one on HOW IT WORKS (the mechanism, in plain words) and one on WHAT RESULTS "
                f"the studies report (hedged exactly as the sources: 'one study found…', 'may'). The rest from: how long it lasts, "
                f"how many sessions and how far apart, recovery and side effects, how to time it on a trip.")
        got = _scripts(task, [i], drafts, want, "procedure")
        items += got
        left -= len(got)
    for i in travel[:cross]:
        want = min(per_cross if not proc else 1, left)
        if want <= 0:
            break
        task = (f"Write {want} TREATMENT script(s) about the treatment side of this travel guide — the question a visitor has "
                f"about combining the treatment with the trip (e.g. is autumn sun or heat OK after a laser, how much walking "
                f"after injections). Use only the treatment information in the guide.")
        got = _scripts(task, [i], drafts, want, "procedure")
        items += got
        left -= len(got)
    for i in travel:
        for place in places[i]:
            where = f"about {place} only (this guide covers several places; this video is just {place})" if place else \
                "about the place itself"
            task = (f"Write 1 TRAVEL script from this guide {where}: what it is, what to see or eat, best time and season to "
                    f"go, opening hours or access, how to plan the visit. Travel content only.")
            items += _scripts(task, [i], drafts, 1, "travel")
    out = []
    for day, it in enumerate(items[:PER_WEEK]):
        it.update(id=f"S{w}-{DAYS[day]}", week=w, day=DAYS[day])
        ds = [drafts[i] for i in it["posts"]]
        out.append(_finish(it, all_facts(ds), ds))
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


def week_ready(ids: list[str], today: date | None = None) -> bool:
    """그 주 블로그 글이 다 정해졌나 — 3편(월·수·금)이 찼거나, 첫 게시일이 3일 안이면."""
    today = today or date.today()
    sched = load_json(content_dir() / "site" / "schedule.json")
    first = min(date.fromisoformat(sched[i]["date"]) for i in ids if i in sched)
    return len(ids) >= 3 or (first - today).days <= 3


def make_ready() -> list[str]:
    """워커용 — 다 찬 주 하나만 만들고 검수 안내 (2026-10-22: 블로그 그림·숏폼을 게시 전에 먼저)."""
    plan = load_plan()
    done = {tuple(w["posts"]) for w in plan["weeks"]}
    for ids in weeks():
        if tuple(ids) not in done and week_ready(ids):
            made = make(1)
            return [f"🎬 숏폼 대본 {', '.join(made)} — '숏폼 검수 목록' 이라고 답하면 보내 드립니다"] if made else []
    return []


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


def chunks(text: str, limit: int = 3000) -> list[str]:
    """텔레그램 한 메시지가 너무 길면 전송이 실패한다 (8천 자에서 실패, 2026-10-22) — 대본 단위로 나눈다."""
    out, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    return out + ([cur] if cur else [])


def send() -> str:
    worker = importlib.import_module("pipeline.worker")
    parts = chunks(list_message())
    sent = sum(1 for c in parts if worker.hermes_send(c) or worker.hermes_send(c))
    return f"검수 메시지 {sent}/{len(parts)}개 보냄"


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
            new = (write(task, drafts, note=m.group(3).strip(), kind_=_kind(s)) or [None])[0]
            if not new:
                raise ShortsError(f"{s['id']} 다시 쓰기 실패")
            s.setdefault("history", []).append({"lines": s["lines"], "note": m.group(3).strip(), "at": now_iso()})
            s.update(**{k: new.get(k) for k in ("question", "lines", "fact_ids")})
            _finish(s, all_facts(drafts), drafts)
            out.append(f"✏️ {s['id']} 다시 씀 → {'검수 통과' if s['status'] == 'review' else '⚠️ 다시 확인 필요'}")
    save_plan(plan)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="숏폼 대본 (한 영상 = 한 질문)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--weeks", type=int, default=None, help="이번에 만들 주 수 (기본: 남은 주 전부)")
    sub.add_parser("list")
    sub.add_parser("refix", help="걸린 대본 자동 수정·재검수")
    sub.add_parser("send", help="검수 메시지를 텔레그램으로 (길면 나눠서)")
    a = sub.add_parser("apply")
    a.add_argument("text")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        print("\n".join(make(args.weeks)) or "새로 만들 주 없음")
    elif args.cmd == "list":
        print(list_message())
    elif args.cmd == "send":
        print(send())
    elif args.cmd == "refix":
        print("\n".join(refix()) or "고칠 대본 없음")
    else:
        print(apply(args.text))
    return 0


if __name__ == "__main__":
    run_cli("shorts", main)
