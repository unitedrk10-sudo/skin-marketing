"""텔레그램 답장 처리 (2026-11-01) — Gemini 대화 에이전트를 없애고 코드 + Claude 로.

Hermes 플러그인(hermes/plugins/skin-router)이 사람의 메시지를 받아 여기로 넘긴다.
정해진 형식은 AI 없이 정해진 명령으로만 실행한다. 그 외 말은 Claude(stage telegram_chat)가 해석한다:
코드가 민감값을 지워 넘긴 로그·검수 결과만 보고 답하며(도구 없음), 실행이 필요하면 정해진 명령을 제안만 한다. 코드가 제안을 다시 검사하고,
조회가 아닌 명령은 사람이 "응" 해야 실행한다. Claude 는 게시·승인·파일 수정을 할 수 없다 (2026-10-29·30 사고 방지).

    python -m pipeline.telegram_router handle < 메시지.txt   # 플러그인이 호출
    python -m pipeline.telegram_router parse "1,3 승인"      # 어떤 명령으로 바뀌는지 확인
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import llm
from pipeline.common import ROOT, content_dir, get_logger, load_env_file, load_json, prompt, run_cli

log = get_logger("telegram")

RUN_TIMEOUT = 600
HISTORY_KEEP = 6
X_URL = re.compile(r"https://(?:www\.|mobile\.)?(?:x|twitter)\.com/\S+/status/\d+\S*", re.I)
THREADS_URL = re.compile(r"https://(?:www\.)?threads\.(?:com|net)/\S+", re.I)
TARGET = r"(?:전체|[Ww\d][Ww\d,~\s\-번]*?)"
CHAIN = re.compile(r"(승인|폐기|병원확인|빼기|올려)(?:하고|한 다음|한 뒤|\s*그리고|\s*,)\s+")  # "1 승인하고 오늘 스레드 올려"
SECRET = re.compile(r"(access_token=|key=|token=|Bearer\s+)\S+|(?<![A-Za-z0-9_\-])(?!\d{4}-W\d\d)(?=[A-Za-z0-9_\-]*[A-Z0-9])[A-Za-z0-9_\-]{32,}")  # 글 id·주소 slug 는 살린다
YES = re.compile(r"(응|네|예|ㅇㅇ|그래|좋아|실행|진행|ok|yes)[.!\s]*", re.I)
NO = re.compile(r"(아니|아니요|아니오|취소|ㄴㄴ|no)[.!\s]*", re.I)
PENDING_MINUTES = 10
READ_ONLY = {("pipeline.figures", "list"), ("pipeline.social", "list"), ("pipeline.site", "schedule"), ("pipeline.03_review", "list"),
             ("pipeline.tracker", "report"), ("pipeline.trends", "show"), ("pipeline.sponsors", "check"), ("pipeline.experiment", "summary")}
EVIDENCE_LOGS = ("worker", "social", "site", "figures", "telegram", "02_draft", "02b_auto_review", "03_review", "01_topics")
EVIDENCE_POSTS = 12
LOG_TAIL = 80
LINE_CHARS = 300
FILE_CHARS = 2500
EVIDENCE_CHARS = 80000

HELP = """[명령어]
검수: 1,3 승인 / 전체 승인 / 2 수정: 내용 / 5 폐기 / 2 병원확인 / 재확인 1 승인 (경고 건)
주제: 1,3,4 (주제 후보 번호)
그림: 그림 1,2 승인 / 그림 2 빼기 / 그림 전체 승인 / 그림 목록
SNS: 오늘 스레드 올려 / 오늘 2번째 스레드 올려 / 3 스레드 올려 / X·Threads 주소 붙여넣기 / 3 + 주소 / SNS 목록 / 오늘 글
조회: 검수 목록 / 승인 목록 / 3번 보여줘 / 3번 블로그 보여줘 / 블로그 일정 / 유입 / 관광지 트렌드
승인 글: 승인글 2 폐기 / 2 되돌리기: 내용
스폰서: 스폰서 목록 / 스폰서 <id> | <영문 제목> | <답할 질문> (코스 글은 끝에 | 코스)
숏폼: 숏폼 1,3 승인 / 숏폼 2 반려 / 숏폼 4 수정: 내용 / 숏폼 검수 목록
실험: 실험 영상 넣었어 / 실험 점수 4 4 3 예 / 실험 정리
여러 개: 줄을 나누거나 연달아 보내면 순서대로 실행합니다 (예: 1 승인하고 오늘 스레드 올려). 앞 명령이 실패하면 멈춥니다.
그 외 말: Claude 가 기록을 읽고 답하고, 실행이 필요하면 명령을 제안합니다 ("응" 하면 실행)."""


def _review_args(text: str) -> list[str] | None:
    """초안 검수 답장 → 03_review 인자. 수정 요청은 뒤의 여러 줄까지 원문 그대로, 나머지는 한 줄에 명령 하나."""
    confirm = bool(re.match(r"\s*재확인\s", text))
    body = re.sub(r"^\s*재확인\s+", "", text).strip()
    if re.fullmatch(rf"{TARGET}\s*수정\s*:.+", body, re.S):
        return ["pipeline.03_review", "apply", body]
    lines = [re.sub(r"(\d)\s*번", r"\1", x.strip()) for x in body.splitlines() if x.strip()]
    if not lines or not all(re.fullmatch(rf"{TARGET}\s*(?:승인|폐기|병원확인)", x) for x in lines):
        return None
    if confirm and any(x.startswith("전체") for x in lines):
        return None  # 경고 건 승인은 번호를 골라서만
    return ["pipeline.03_review", "apply", "\n".join(lines), *(["--confirm"] if confirm else [])]


def _sns_ref(text: str) -> str:
    """'오늘' = 가장 최근에 보낸 글, '오늘 2번째'·'1 오늘' = 오늘 그 칸, '3' = social list 번호."""
    if m := re.search(r"오늘\s*(\d)\s*번째", text) or re.match(r"\s*(\d)\s*(?:번\s*)?오늘", text):
        return f"오늘#{m.group(1)}"
    if "오늘" in text:
        return "오늘"
    if m := re.match(r"\s*(\d+)\b", text):
        return m.group(1)
    return "오늘"


def parse(text: str) -> dict | None:
    """정해진 형식이면 {"args": [...]} 또는 {"reply": ...} / {"show": ...}. 아니면 None (Claude 가 답만)."""
    t = text.strip()
    if re.fullmatch(r"(도움말|명령어|help)", t, re.I):
        return {"reply": HELP}
    if re.fullmatch(r"그림\s*(승인\s*)?목록", t):
        return {"args": ["pipeline.figures", "list"]}
    if m := re.fullmatch(r"그림\s+(.+)", t):
        return {"args": ["pipeline.figures", "apply", m.group(1)]}
    if re.fullmatch(r"숏폼\s*(대본 보여줘|검수 목록)", t):
        return {"args": ["pipeline.shorts", "send"]}
    if m := re.fullmatch(r"숏폼\s+([\d,\s]+(?:승인|반려|수정\s*:.+))", t, re.S):
        return {"args": ["pipeline.shorts", "apply", m.group(1)]}
    if re.fullmatch(r"(실험 )?영상 (다 )?넣었어", t):
        return {"args": ["pipeline.experiment", "build"]}
    if m := re.fullmatch(r"실험 점수\s+(\d)\s+(\d)\s+(\d)\s+(예|아니오)", t):
        return {"args": ["pipeline.experiment", "score", "최근", *m.groups()]}
    if re.fullmatch(r"실험 (정리|결과)", t):
        return {"args": ["pipeline.experiment", "summary"]}
    for channel, pattern in (("x", X_URL), ("threads", THREADS_URL)):
        if m := pattern.search(t):
            return {"args": ["pipeline.social", "done", _sns_ref(t[:m.start()]), channel, m.group(0)]}
    if re.search(r"(스레드|threads)\s*(에\s*)?(올려|게시)", t, re.I) and len(t) <= 30:
        return {"args": ["pipeline.social", "post", _sns_ref(t), "threads"]}
    if re.fullmatch(r"(SNS|sns|X·Threads)\s*(글\s*)?목록", t):
        return {"args": ["pipeline.social", "list"]}
    if re.fullmatch(r"오늘(의)?\s*(SNS\s*)?글(\s*(다시\s*)?(보여줘|줘))?", t):
        return {"args": ["pipeline.social", "today"]}
    if re.fullmatch(r"(블로그 일정|언제 올라가\??)", t):
        return {"args": ["pipeline.site", "schedule"]}
    if re.fullmatch(r"검수 목록( 다시)?", t):
        return {"args": ["pipeline.03_review", "list"]}
    if re.fullmatch(r"승인 목록", t):
        return {"args": ["pipeline.03_review", "list", "--stage", "approved"]}
    if m := re.fullmatch(r"승인글\s+(.+)", t, re.S):
        return {"args": ["pipeline.03_review", "apply", m.group(1), "--stage", "approved"]}
    if re.fullmatch(r"\d+\s*되돌리기\s*(:.*)?", t, re.S):
        return {"args": ["pipeline.03_review", "apply", t, "--stage", "approved"]}
    if m := re.fullmatch(r"(\d+)\s*번?\s*(블로그|대본)?\s*(글\s*)?보여\s*줘", t):
        return {"show": int(m.group(1)), "part": m.group(2) or ""}
    if re.fullmatch(r"유입( 어때\??)?", t):
        return {"args": ["pipeline.tracker", "report", "--days", "7"]}
    if re.fullmatch(r"(관광지 트렌드|요즘 뜨는 관광지\??)", t):
        return {"args": ["pipeline.trends", "show"]}
    if re.fullmatch(r"스폰서 목록", t):
        return {"args": ["pipeline.sponsors", "check"]}
    if m := re.fullmatch(r"스폰서\s+(\S+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)(\s*\|\s*코스)?", t):
        return {"args": ["pipeline.worker", "request-sponsored", "--sponsor", m.group(1), "--title", m.group(2),
                         "--angle", m.group(3), *(["--course"] if m.group(4) else [])]}
    if args := _review_args(t):
        return {"args": args}
    if re.fullmatch(r"(주제\s*)?\d+(\s*번)?(\s*[,\s]\s*\d+(\s*번)?)*", t):
        return {"args": ["pipeline.worker", "request-drafts", "--pick", ",".join(re.findall(r"\d+", t))]}
    return None


def scrub(text: str) -> str:
    for name, value in os.environ.items():  # 형식과 상관없이 실제 키 값은 반드시 지운다
        if value and len(value) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|_OC$", name):
            text = text.replace(value, "***")
    return SECRET.sub("***", text)


def run(args: list[str]) -> str:
    proc = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=RUN_TIMEOUT, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    out = proc.stdout.strip()
    if out:
        return out
    if proc.returncode == 0:
        return "✅ 완료"
    log.warning("명령 실패 %s: %s", args[:2], scrub(proc.stderr[-500:]))
    last = (proc.stderr.strip().splitlines() or ["원인 불명"])[-1]
    return f"⚠️ 실패: {scrub(last)[:300]}"


def show(n: int, part: str) -> str:
    batch = content_dir("drafts") / "_batch.json"
    ids = load_json(batch).get("ids", []) if batch.exists() else []
    if not 1 <= n <= len(ids):
        return f"❓ 검수 목록에 {n}번이 없습니다 (현재 {len(ids)}개)"
    path = content_dir("drafts") / ids[n - 1]
    if not path.exists():
        return f"❓ {ids[n - 1]}: 이미 처리된 글입니다"
    review = importlib.import_module("pipeline.03_review")
    doc = {"블로그": path / "blog.md", "대본": path / "script.md"}.get(part) or review.review_doc(path)
    return f"{n}번 {ids[n - 1]}\nMEDIA:`{doc}`"


def _history_file():
    return content_dir() / "telegram" / "history.json"


def load_history() -> list[dict]:
    f = _history_file()
    return load_json(f) if f.exists() else []


def remember(text: str, reply: str) -> None:
    f = _history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    rows = (load_history() + [{"user": text[:500], "bot": reply[:800]}])[-HISTORY_KEEP:]
    f.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def snapshot() -> str:
    """Claude 답변용 현재 상태 (읽기 전용)."""
    parts = []
    for name, args in (("블로그 일정", ["pipeline.site", "schedule"]), ("검수 대기", ["pipeline.03_review", "list"])):
        try:
            parts.append(f"[{name}]\n{run(args)[:1200]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            parts.append(f"[{name}] 읽지 못함: {type(e).__name__}")
    daily = content_dir() / "social" / "daily.json"
    if daily.exists():
        rows = sorted(load_json(daily).items())[-3:]
        parts.append("[최근 오늘의 SNS 글]\n" + "\n".join(f"{k}: {v['key']} ({v['kind']})" for k, v in rows))
    return "\n\n".join(parts)


def _log_tail(name: str) -> str:
    f = ROOT / "logs" / f"{name}.log"
    if not f.exists():
        return ""
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
    return "\n".join(x[:LINE_CHARS] for x in lines)


def _json_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:FILE_CHARS] if path.exists() else ""


def evidence() -> str:
    """Claude 답변 근거. 저장소 경로·도구를 주지 않고 코드가 고른 기록만, 값처럼 보이는 것은 지워서 프롬프트로 넣는다."""
    parts = [(f"log {n}", _log_tail(n)) for n in EVIDENCE_LOGS]
    parts += [("schedule.json", _json_text(content_dir() / "site" / "schedule.json")),
              ("sns daily.json", _json_text(content_dir() / "social" / "daily.json"))]
    topics_dir = content_dir() / "topics"
    for t in sorted(topics_dir.glob("*.json"))[-1:] if topics_dir.exists() else []:
        parts.append((f"topics {t.name}", _json_text(t)))
    posts = [p for stage in ("drafts", "approved") if content_dir(stage).exists() for p in content_dir(stage).iterdir() if p.is_dir()]
    for post in sorted(posts, key=lambda p: p.stat().st_mtime)[-EVIDENCE_POSTS:]:
        for name in ("review", "social", "figures"):
            parts.append((f"{post.parent.name}/{post.name}/{name}.json", _json_text(post / f"{name}.json")))
    return scrub("\n\n".join(f"--- {title} ---\n{body}" for title, body in parts if body))[:EVIDENCE_CHARS]


def _pending_file() -> Path:
    return content_dir() / "telegram" / "pending.json"


def _save_pending(chunks: list[str]) -> None:
    f = _pending_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "chunks": chunks}, ensure_ascii=False), encoding="utf-8")


def _take_pending() -> list[str]:
    """제안해 둔 명령을 꺼내고 지운다. 다른 말을 하거나 시간이 지나면 없던 일로 한다."""
    f = _pending_file()
    if not f.exists():
        return []
    data = load_json(f)
    f.unlink()
    fresh = datetime.now(timezone.utc) - datetime.fromisoformat(data["at"]) <= timedelta(minutes=PENDING_MINUTES)
    return data["chunks"] if fresh else []


def _read_only(chunk: str) -> bool:
    cmd = parse(chunk) or {}
    return "reply" in cmd or "show" in cmd or tuple((cmd.get("args") or [""])[:2]) in READ_ONLY


def chat(text: str) -> str:
    history = "\n".join(f"사람: {h['user']}\n봇: {h['bot']}" for h in load_history()) or "(없음)"
    try:
        data, _ = llm.generate_json("telegram_chat", prompt("telegram_chat", help=HELP, status=snapshot(), history=history,
                                                            message=text, evidence=evidence()))
    except llm.LLMError as e:
        log.warning("telegram_chat 실패: %s", e)
        return "⚠️ 지금은 답변을 만들 수 없습니다 (Claude 연결 확인 필요). 정해진 명령은 그대로 됩니다 — '도움말'"
    data = data if isinstance(data, dict) else {}
    answer = scrub(str(data.get("reply") or "")).strip()
    if "[오늘의 SNS 글" in answer:  # SNS 글은 워커만 보낸다
        answer = "⚠️ 답변이 규칙에 맞지 않아 보내지 않았습니다."
    chunks, dropped = [], []
    for proposal in data.get("commands") or []:
        found = split_commands(str(proposal))
        if found:
            chunks += found
        else:
            dropped.append(str(proposal))
    if dropped:
        log.warning("Claude 제안 버림: %s", dropped)
    if not chunks:
        return (answer or "❓ 무슨 뜻인지 모르겠습니다. '도움말' 을 확인해 주세요") + "\n\n(실행한 작업 없음)"
    if all(_read_only(c) for c in chunks):
        return "\n\n".join(x for x in (answer, run_chunks(chunks)) if x)
    _save_pending(chunks)
    return "\n\n".join(x for x in (answer, "이렇게 실행할까요? (응 / 취소)\n" + "\n".join(f"→ {c}" for c in chunks)) if x)


def _is_memo(chunk: str) -> bool:
    return bool(re.search(r"(수정|되돌리기)\s*:", chunk))


def _is_plain_review(cmd: dict | None) -> bool:
    args = (cmd or {}).get("args") or []
    return args[:2] == ["pipeline.03_review", "apply"] and len(args) == 3 and "수정" not in args[2]


def split_commands(text: str) -> list[str] | None:
    """한 메시지(또는 몰아 온 메시지들)를 순서대로 실행할 명령으로 나눈다. 모르는 줄이 하나라도 있으면 None.
    수정 메모의 다음 줄은 명령처럼 보이지 않으면 메모로 이어 붙이고, 이어지는 승인·폐기 줄은 한 번에 검증되게 합친다."""
    lines = [x.strip() for x in CHAIN.sub(r"\1\n", text).splitlines() if x.strip()] if not _is_memo(text) else \
            [x.strip() for x in text.splitlines() if x.strip()]
    chunks: list[str] = []
    for line in lines:
        cmd = parse(line)
        if chunks and _is_memo(chunks[-1]) and (cmd is None or (cmd.get("args") or [""])[0] == "pipeline.worker"):
            chunks[-1] += "\n" + line  # 메모 안의 숫자만 있는 줄을 주제 선택으로 읽지 않는다
        elif cmd is None:
            return None
        elif chunks and _is_plain_review(cmd) and _is_plain_review(parse(chunks[-1])) and not line.startswith("재확인"):
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    return chunks


def run_one(chunk: str) -> str:
    cmd = parse(chunk)
    if "reply" in cmd:
        return cmd["reply"]
    if "show" in cmd:
        return show(cmd["show"], cmd["part"])
    log.info("명령: %s", cmd["args"][:2])
    return run(cmd["args"])


def handle(text: str) -> str:
    pending = _take_pending()
    if pending and YES.fullmatch(text.strip()):
        return run_chunks(pending)
    if pending and NO.fullmatch(text.strip()):
        return "취소했습니다."
    chunks = split_commands(text)
    return chat(text) if chunks is None else run_chunks(chunks)


def run_chunks(chunks: list[str]) -> str:
    """명령을 순서대로 실행한다. 앞 명령이 실패하면 뒤 명령은 실행하지 않는다."""
    out = []
    for n, chunk in enumerate(chunks):
        reply = run_one(chunk)
        out.append(reply)
        if reply.startswith(("⚠️", "❓")) and chunks[n + 1:]:
            out.append("⏹ 앞 명령이 실패해 멈췄습니다. 실행 안 한 명령:\n" + "\n".join(chunks[n + 1:]))
            break
    return "\n\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="텔레그램 답장 처리")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("handle")
    p = sub.add_parser("parse")
    p.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "parse":
        print(json.dumps(parse(args.text), ensure_ascii=False))
        return 0
    load_env_file()
    text = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    if not text:
        return 0
    worker = importlib.import_module("pipeline.worker")
    if split_commands(text) is None and not YES.fullmatch(text) and not NO.fullmatch(text):
        worker.hermes_send("🔎 확인 중입니다")  # 기록을 읽는 데 1분 안팎 걸린다
    try:
        reply = handle(text)
    except subprocess.TimeoutExpired:
        reply = "⚠️ 시간이 너무 오래 걸려 중단했습니다"
    remember(text, reply)
    if not worker.hermes_send(reply):
        log.error("답장 전송 실패")
        return 1
    return 0


if __name__ == "__main__":
    run_cli("telegram", main)
