"""Reddit 밖 외부 언급 장부 — 디렉터리 등록·기고·언론·질문답변·데이터 세트 (2026-11-02, docs/reddit-ops.md 6장).

AI 검색이 인용하는 출처를 넓히려는 일의 기록만 한다. 신청·작성·게시는 사람이 하고, 도구는 무엇을 어디에 냈고 어떻게 됐는지 남겨
주간 리포트에 보여 준다. 대가가 오가는 게재는 광고 표시·rel="sponsored" 가 필요하다고 표시하고, 이해관계자 편집을 막는 곳(위키백과 등)과
병원 순위·추천 디렉터리는 추가하지 않는다. Reddit 답변은 pipeline/reddit.py 가 따로 기록한다.

    python -m pipeline.mentions list
    python -m pipeline.mentions add <종류> "<이름>" [--url 주소] [--paid] [--note …]
    python -m pipeline.mentions set <N> applied|live|rejected|removed|dropped [--url 주소] [--note …]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from pipeline.common import content_dir, get_logger, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("mentions")

STATUS = {"idea": "계획", "applied": "신청·제안", "live": "게재", "rejected": "거절", "removed": "삭제됨", "dropped": "중단"}
STATUS_KO = {"신청": "applied", "제안": "applied", "게재": "live", "거절": "rejected", "삭제됨": "removed", "삭제": "removed", "중단": "dropped"}
KIND_LABEL = {"directory": "디렉터리", "guest_post": "기고", "press": "언론", "qa": "질문답변", "dataset": "데이터", "forum": "커뮤니티", "other": "기타"}


class MentionError(RuntimeError):
    pass


def settings() -> dict:
    return load_yaml("mentions.yaml")


def _file():
    return content_dir() / "mentions" / "state.json"


def load() -> dict:
    return load_json(_file()) if _file().exists() else {"items": []}


def save(state: dict) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    save_json(_file(), state)


def _kind(kind: str) -> str:
    kinds = settings().get("kinds") or {}
    value = kinds.get(kind, kind)
    if value not in KIND_LABEL:
        raise MentionError(f"종류는 {', '.join(kinds) or ', '.join(KIND_LABEL)} 중 하나입니다")
    return value


def _url(url: str) -> str:
    if url and not url.startswith(("https://", "http://")):
        raise MentionError("주소는 http(s):// 로 시작해야 합니다")
    return url


def add(kind: str, name: str, url: str = "", paid: bool = False, note: str = "") -> dict:
    name = name.strip()
    if not name:
        raise MentionError("이름이 없습니다")
    low = f"{name} {url}".lower()
    if hit := next((b for b in settings().get("blocked_names", []) if b.lower() in low), None):
        raise MentionError(f"추가하지 않습니다: '{hit}' — 이해관계자 편집 금지 또는 병원 순위·추천 매체 (docs/reddit-ops.md 6장)")
    state = load()
    item = {"n": max((i["n"] for i in state["items"]), default=0) + 1, "kind": _kind(kind), "name": name, "url": _url(url),
            "paid": paid, "status": "idea", "note": note, "at": now_iso(), "history": [{"status": "idea", "at": now_iso()}]}
    state["items"].append(item)
    save(state)
    return item


def set_status(n: int, status: str, url: str = "", note: str = "") -> dict:
    status = STATUS_KO.get(status, status)
    if status not in STATUS:
        raise MentionError(f"상태는 {', '.join(STATUS_KO)} 중 하나입니다")
    state = load()
    item = next((i for i in state["items"] if i["n"] == n), None)
    if not item:
        raise MentionError(f"{n}번 언급 기록이 없습니다")
    if status == "live" and not (url or item.get("live_url")):
        raise MentionError("게재는 실린 주소와 함께: `언급 N 게재 <주소>`")
    item["status"] = status
    if url:
        item["live_url" if status == "live" else "url"] = _url(url)
    if note:
        item["note"] = note
    item.setdefault("history", []).append({"status": status, "at": now_iso(), **({"note": note} if note else {})})
    save(state)
    return item


def _line(i: dict) -> str:
    paid = " · 💰 대가 있음 → 광고 표시·rel=sponsored 필요" if i.get("paid") else ""
    where = i.get("live_url") or i.get("url") or ""
    return f"{i['n']}. [{KIND_LABEL[i['kind']]}] {i['name']} — {STATUS[i['status']]}{paid}" + (f"\n   {where}" if where else "") + (f"\n   {i['note']}" if i.get("note") else "")


def _since(i: dict, status: str) -> str:
    return next((h["at"] for h in reversed(i.get("history", [])) if h["status"] == status), i.get("at", ""))


def list_message() -> str:
    items = load()["items"]
    if not items:
        return "외부 언급 기록이 없습니다. 추가: `언급 추가 <디렉터리|기고|언론|질문답변|데이터|커뮤니티|기타> <이름> [주소] [유료]`"
    order = ["live", "applied", "idea", "rejected", "removed", "dropped"]
    lines = ["[외부 언급] docs/reddit-ops.md 6장 — 그곳 규칙 먼저 읽기, 운영자 신분 공개, 병원명·스폰서 병원 없음"]
    for i in sorted(items, key=lambda i: (order.index(i["status"]), i["n"])):
        lines.append(_line(i))
    lines.append("상태 바꾸기: `언급 N 신청|게재 <주소>|거절|삭제됨|중단 [메모]`")
    return "\n".join(lines)


def report_lines(days: int = 7) -> list[str]:
    items = load()["items"]
    if not items:
        return []
    now = datetime.fromisoformat(now_iso())
    since = (now - timedelta(days=days)).isoformat()
    follow = int(settings().get("follow_up_days", 21))
    live = [i for i in items if i["status"] == "live"]
    new_live = [i for i in live if _since(i, "live") >= since]
    waiting = [i for i in items if i["status"] == "applied" and _since(i, "applied") <= (now - timedelta(days=follow)).isoformat()]
    lines = [f"📣 외부 언급: 게재 {len(live)} (이번 주 새로 {len(new_live)}), 신청 중 {sum(1 for i in items if i['status'] == 'applied')}, "
             f"계획 {sum(1 for i in items if i['status'] == 'idea')}"]
    if waiting:
        lines.append(f"   {follow}일 넘게 답 없음: " + ", ".join(f"{i['n']}. {i['name']}" for i in waiting))
    gone = [i for i in items if i["status"] == "removed" and _since(i, "removed") >= since]
    if gone:
        lines.append("   ⚠️ 삭제됨: " + ", ".join(f"{i['n']}. {i['name']}" for i in gone))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="외부 언급 장부")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("kind")
    p.add_argument("name")
    p.add_argument("--url", default="")
    p.add_argument("--paid", action="store_true")
    p.add_argument("--note", default="")
    p = sub.add_parser("set")
    p.add_argument("n", type=int)
    p.add_argument("status")
    p.add_argument("--url", default="")
    p.add_argument("--note", default="")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            print(list_message())
        elif args.cmd == "add":
            item = add(args.kind, args.name, args.url, args.paid, args.note)
            print("외부 언급 추가\n" + _line(item))
        elif args.cmd == "set":
            print(_line(set_status(args.n, args.status, args.url, args.note)))
    except MentionError as e:
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("mentions", main)
