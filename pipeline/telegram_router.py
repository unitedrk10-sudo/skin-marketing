"""텔레그램 답장 처리 (2026-11-01) — Gemini 대화 에이전트를 없애고 코드 + Claude 로.

Hermes 플러그인(hermes/plugins/skin-router)이 사람의 메시지를 받아 여기로 넘긴다.
정해진 형식은 AI 없이 정해진 명령으로만 실행하고, 그 외에는 도구 없는 Claude(stage telegram_chat)가 답만 한다.
Claude 경로는 게시·승인·파일 수정을 할 수 없다 — 글을 지어내 게시한 사고(2026-10-29·30)를 구조적으로 막는다.

    python -m pipeline.telegram_router handle < 메시지.txt   # 플러그인이 호출
    python -m pipeline.telegram_router parse "1,3 승인"      # 어떤 명령으로 바뀌는지 확인
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys

from pipeline import llm
from pipeline.common import ROOT, content_dir, get_logger, load_env_file, load_json, prompt, run_cli

log = get_logger("telegram")

RUN_TIMEOUT = 600
HISTORY_KEEP = 6
X_URL = re.compile(r"https://(?:www\.|mobile\.)?(?:x|twitter)\.com/\S+/status/\d+\S*", re.I)
THREADS_URL = re.compile(r"https://(?:www\.)?threads\.(?:com|net)/\S+", re.I)
TARGET = r"(?:전체|[Ww\d][Ww\d,~\s\-번]*?)"
SECRET = re.compile(r"(access_token=|key=|token=|Bearer\s+)\S+|\b[A-Za-z0-9_\-]{32,}\b")

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
그 외 말은 Claude 가 답만 합니다 (실행은 하지 않습니다)."""


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
    return SECRET.sub("***", text)


def run(args: list[str]) -> str:
    proc = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=RUN_TIMEOUT, check=False)
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


def chat(text: str) -> str:
    history = "\n".join(f"사람: {h['user']}\n봇: {h['bot']}" for h in load_history()) or "(없음)"
    try:
        result = llm.generate("telegram_chat", prompt("telegram_chat", help=HELP, status=snapshot(), history=history, message=text))
    except llm.LLMError as e:
        log.warning("telegram_chat 실패: %s", e)
        return "⚠️ 지금은 답변을 만들 수 없습니다 (Claude 연결 확인 필요). 정해진 명령은 그대로 됩니다 — '도움말'"
    return result.text.strip() + "\n\n(실행한 작업 없음)"


def handle(text: str) -> str:
    cmd = parse(text)
    if cmd is None:
        return chat(text)
    if "reply" in cmd:
        return cmd["reply"]
    if "show" in cmd:
        return show(cmd["show"], cmd["part"])
    log.info("명령: %s", cmd["args"][:2])
    return run(cmd["args"])


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
    try:
        reply = handle(text)
    except subprocess.TimeoutExpired:
        reply = "⚠️ 시간이 너무 오래 걸려 중단했습니다"
    remember(text, reply)
    if not importlib.import_module("pipeline.worker").hermes_send(reply):
        log.error("답장 전송 실패")
        return 1
    return 0


if __name__ == "__main__":
    run_cli("telegram", main)
