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
LONG_RUN_TIMEOUT = 1500  # Reddit 정리 → 조사 → 초안은 LLM 을 여러 번 불러 10분을 넘길 수 있다 (2026-11-05, 플러그인 TIMEOUT 1800 보다 짧게)
LONG_COMMANDS = {("pipeline.reddit", c) for c in ("want", "want-text", "research", "draft", "direct", "brief")}
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
             ("pipeline.tracker", "report"), ("pipeline.trends", "show"), ("pipeline.sponsors", "check"), ("pipeline.experiment", "summary"), ("pipeline.reddit", "list"), ("pipeline.reddit", "show"),
             ("pipeline.reddit", "communities"), ("pipeline.mentions", "list"), ("pipeline.data_notes", "list")}
EVIDENCE_LOGS = ("worker", "social", "site", "figures", "telegram", "02_draft", "02b_auto_review", "03_review", "01_topics")
REDDIT_EVIDENCE_LOGS = ("reddit", "telegram", "worker")  # Reddit 방의 Claude 답변 근거 (2026-11-03)
EVIDENCE_POSTS = 12
LOG_TAIL = 80
LINE_CHARS = 300
FILE_CHARS = 2500
EVIDENCE_CHARS = 80000

HELP = """[명령어]
검수: 1,3 승인 / W46-03 승인 (글번호, 하이픈) / 전체 승인 (✅만) / 2 수정: 내용 / 5 폐기 / 2 병원확인 / 재확인 1 승인 (스폰서 ⛔ 건)
주제: 1,3,4 (주제 후보 번호)
그림: 그림 1,2 승인 / 그림 2 빼기 / 그림 전체 승인 / 그림 목록
SNS: 오늘 스레드 올려 / 오늘 2번째 스레드 올려 / 3 스레드 올려 / X·Threads 주소 붙여넣기 / 3 + 주소 / SNS 목록 / 오늘 글
조회: 검수 목록 / 승인 목록 / 3번 보여줘 / 3번 블로그 보여줘 / 블로그 일정 / 유입 / 관광지 트렌드
승인 글: 승인글 2 폐기 / 2 되돌리기: 내용
데이터 글: 데이터글 목록 / 데이터글 승인 / 데이터글 폐기 (검수 대기 글에 적용, 지역을 고르는 명령이 아님) / 데이터글 초안 where / 데이터글 초안 changes (분기 변화)
Reddit (기준 docs/reddit-ops.md): 레딧 글 주소 + 글 내용 보내기 (주소 없이 본문만 붙여 넣어도 됨, Reddit 방) → ① 정리(질문 요약·우리 글 근거·작성 권고) → ② 조사(출처 찾기·사실 확인) → ③ 초안(영어, 붙여넣기용) — 활동 단계 커뮤니티 / 직접 쓰기: 레딧 1 검사: 한국어 본문 → 번역·검사 / 레딧 1 정리 / 레딧 1 조사 (다시 조사 + 초안) / 레딧 1 조사: 스킨부스터, 컨시어지 (그 주제를 더 찾아서 초안 다시) / 레딧 1 커뮤니티 <이름> / 레딧 기준 / 레딧 후보 찾아 / 레딧 목록 / 레딧 1 초안 / 레딧 1 방향: 내가 하고 싶은 말 (커뮤니티 말투로 영작) / 레딧 1 사실 → 레딧 1 검사: 직접 쓴 본문 (영어, 번역 허용된 곳은 한국어도 — Google 번역, 숫자는 빼고) / 레딧 1 보여줘 / 레딧 1 빼기 / 레딧 추가 <글 주소> <질문> / 레딧 글 주소만 보내기 → 글 내용 붙여넣기 → 답변
Reddit 기록: 레딧 1 완료 <댓글 주소> / 레딧 1 반응 5 답글 2 / 레딧 1 삭제됨 [메모] / 레딧 경고 <서브레딧|전체> [메모] / 레딧 재개 <서브레딧|전체> / 레딧 규칙확인 <서브레딧> [링크허용] [AI금지] [번역허용] [시술금지] / 레딧 문의 <서브레딧> / 레딧 운영진답변 <서브레딧> 허용|거절 [링크허용] [AI금지] [번역허용] [시술금지] [메모] / 레딧 규칙초기화 <서브레딧> / 레딧 규칙 조사 (주 1회 자동)
외부 언급: 언급 목록 / 언급 추가 <디렉터리|기고|언론|질문답변|데이터|커뮤니티|기타> <이름> [주소] [유료] / 언급 1 신청|게재 <주소>|거절|삭제됨|중단 [메모]
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


SUB = r"(?:r/)?([A-Za-z0-9_-]+)"
FLAGS = r"(?:\s*(?:링크\s*허용|AI\s*금지|번역\s*허용|시술\s*금지))*"


def _reddit_flags(text: str | None) -> list[str]:
    text = text or ""
    return [*(["--links"] if re.search(r"링크\s*허용", text) else []), *(["--no-ai"] if re.search(r"AI\s*금지", text, re.I) else []),
            *(["--mt"] if re.search(r"번역\s*허용", text) else []), *(["--no-treatments"] if re.search(r"시술\s*금지", text) else [])]


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
    if re.fullmatch(r"데이터\s*글\s*목록", t):
        return {"args": ["pipeline.data_notes", "list"]}
    if m := re.fullmatch(r"데이터\s*글\s*(초안|승인|폐기)\s+([a-z0-9_-]+)", t):
        return {"args": ["pipeline.data_notes", {"초안": "draft", "승인": "approve", "폐기": "reject"}[m.group(1)], m.group(2)]}
    if m := re.fullmatch(r"(?:데이터\s*글?|where)\s*(승인|폐기)", t, re.I):  # 글 id 없이: 검수 대기 글이 하나면 그 글 (2026-11-02)
        return {"args": ["pipeline.data_notes", {"승인": "approve", "폐기": "reject"}[m.group(1)]]}
    if re.fullmatch(r"(레딧|reddit)\s*후보\s*(찾아(줘)?|찾기)?", t, re.I):
        return {"args": ["pipeline.reddit", "find"]}
    if re.fullmatch(r"(레딧|reddit)\s*목록", t, re.I):
        return {"args": ["pipeline.reddit", "list"]}
    if re.fullmatch(r"(레딧|reddit)\s*(기준|커뮤니티|규칙\s*목록)", t, re.I):
        return {"args": ["pipeline.reddit", "communities"]}
    if re.fullmatch(r"(레딧|reddit)\s*규칙\s*(조사|다시\s*조사)(\s*해\s*줘)?", t, re.I):
        return {"args": ["pipeline.reddit", "rules", "--force"]}
    if m := re.fullmatch(rf"(?:레딧|reddit)\s*규칙\s*확인\s+{SUB}({FLAGS})", t, re.I):
        return {"args": ["pipeline.reddit", "allow", m.group(1), *_reddit_flags(m.group(2))]}
    if m := re.fullmatch(rf"(?:레딧|reddit)\s*운영진\s*답변\s+{SUB}\s+(허용|거절)({FLAGS})(?:\s+(.+))?", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "mods", m.group(1), "allowed" if m.group(2) == "허용" else "refused", *_reddit_flags(m.group(3)),
                         *(["--note", m.group(4).strip()] if m.group(4) else [])]}
    if m := re.fullmatch(rf"(?:레딧|reddit)\s*(?:운영진\s*)?문의\s+{SUB}", t, re.I):
        return {"args": ["pipeline.reddit", "asked", m.group(1)]}
    if m := re.fullmatch(rf"(?:레딧|reddit)\s*규칙\s*초기화\s+{SUB}", t, re.I):
        return {"args": ["pipeline.reddit", "reset", m.group(1)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*경고\s+(?:r/)?(전체|[A-Za-z0-9_-]+)(?:\s+(.+))?", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "warn", "all" if m.group(1) == "전체" else m.group(1), *(["--note", m.group(2).strip()] if m.group(2) else [])]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*재개\s+(?:r/)?(전체|[A-Za-z0-9_-]+)", t, re.I):
        return {"args": ["pipeline.reddit", "resume", "all" if m.group(1) == "전체" else m.group(1)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*(초안|보여\s*줘|빼기|사실(?:\s*정리)?|정리(?:\s*해\s*줘)?|조사(?:\s*해\s*줘)?)", t, re.I):
        word = m.group(2)
        cmd = ("facts" if word.startswith("사실") else "brief" if word.startswith("정리") else "research" if word.startswith("조사")
               else {"초안": "draft", "빼기": "skip"}.get(word, "show"))
        return {"args": ["pipeline.reddit", cmd, m.group(1)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*(?:추가\s*)?조사\s*[:：]\s*(.+)", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "research", m.group(1), "--", m.group(2).strip()]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*커뮤니티\s+(?:r/)?([A-Za-z0-9_-]+)", t, re.I):
        return {"args": ["pipeline.reddit", "set-sub", m.group(1), m.group(2)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*검사\s*[:：]\s*(.+)", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "check", m.group(1), "--", m.group(2).strip()]}  # 본문이 "-" 로 시작해도 옵션으로 읽지 않게
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*반응\s+(-?\d+)\s*점?(?:\s*,?\s*답글\s*(\d+)\s*개?)?", t, re.I):
        return {"args": ["pipeline.reddit", "react", m.group(1), m.group(2), *(["--replies", m.group(3)] if m.group(3) else [])]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*삭제\s*됨?(?:\s+(.+))?", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "removed", m.group(1), *(["--note", m.group(2).strip()] if m.group(2) else [])]}
    if re.fullmatch(r"(외부\s*)?언급\s*목록", t):
        return {"args": ["pipeline.mentions", "list"]}
    if m := re.fullmatch(r"언급\s*추가\s+(\S+)\s+(.+)", t, re.S):
        rest, url = m.group(2), re.search(r"https?://\S+", m.group(2))
        paid = bool(re.search(r"\s유료\s*$", " " + rest))
        name = re.sub(r"\s*유료\s*$", "", (rest.replace(url.group(0), " ") if url else rest)).strip()
        return {"args": ["pipeline.mentions", "add", m.group(1), name, *(["--url", url.group(0)] if url else []), *(["--paid"] if paid else [])]}
    if m := re.fullmatch(r"언급\s*(\d+)\s*번?\s*(신청|제안|게재|거절|삭제됨|삭제|중단)(?:\s+(https?://\S+))?(?:\s+(.+))?", t, re.S):
        return {"args": ["pipeline.mentions", "set", m.group(1), m.group(2), *(["--url", m.group(3)] if m.group(3) else []),
                         *(["--note", m.group(4).strip()] if m.group(4) else [])]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*방향\s*[:：]\s*(.+)", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "direct", m.group(1), m.group(2)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*(\d+)\s*번?\s*완료\s+(https://\S+)", t, re.I):
        return {"args": ["pipeline.reddit", "done", m.group(1), m.group(2)]}
    if m := re.fullmatch(r"(?:레딧|reddit)\s*추가\s+(https://\S+)\s+(.+)", t, re.I | re.S):
        return {"args": ["pipeline.reddit", "add", m.group(1), m.group(2)]}
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
    timeout = LONG_RUN_TIMEOUT if tuple(args[:2]) in LONG_COMMANDS else RUN_TIMEOUT
    proc = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False,
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


# ── 방 나누기 (2026-11-02): Reddit 전용 방 SKIN_REDDIT_DELIVER=telegram:<chat_id>[:<thread_id>] ──
# 플러그인이 메시지가 온 방을 SKIN_TG_ORIGIN 으로 넘긴다. 방을 모르면(예전 플러그인) 지금처럼 기본 방에서 모든 명령을 받는다.
REDDIT_MODULES = ("pipeline.reddit", "pipeline.mentions")
REDDIT_HELP = "[Reddit 방 명령어]\n" + "\n".join(line for line in HELP.splitlines() if line.startswith(("Reddit", "외부 언급"))) + \
              "\n다른 명령(검수·SNS·블로그 등)은 메인 방에서 보내 주세요."


def origin() -> str:
    return os.environ.get("SKIN_TG_ORIGIN", "").strip()


def _place(target: str) -> tuple[str, str]:
    parts = target.split(":")
    return (parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")


def room() -> str:
    """'reddit' | 'main' | '' (방을 모름 → 나누지 않음)."""
    reddit = importlib.import_module("pipeline.worker").reddit_target()
    here = origin()
    if not reddit or not here.startswith("telegram:"):
        return ""
    (r_chat, r_thread), (h_chat, h_thread) = _place(reddit), _place(here)
    return "reddit" if r_chat == h_chat and (not r_thread or r_thread == h_thread) else "main"


def wrong_room(cmd: dict | None) -> str:
    """이 방에서 받지 않는 명령이면 안내 문구."""
    where, module = room(), ((cmd or {}).get("args") or [""])[0]
    if where == "reddit" and ("show" in cmd or (module and module not in REDDIT_MODULES)):
        return "⚠️ 이 방은 Reddit 전용입니다. 이 명령은 메인 방에서 보내 주세요."
    if where == "main" and module in REDDIT_MODULES:
        return "⚠️ Reddit 명령은 Reddit 방에서 보내 주세요."
    return ""


def reply_to(text: str) -> bool:
    """답장은 메시지가 온 방으로."""
    send = importlib.import_module("pipeline.worker").hermes_send
    return send(text, target=origin()) if origin() else send(text)


def _history_file():
    return content_dir() / "telegram" / ("history_reddit.json" if room() == "reddit" else "history.json")


def load_history() -> list[dict]:
    f = _history_file()
    return load_json(f) if f.exists() else []


def remember(text: str, reply: str) -> None:
    f = _history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    rows = (load_history() + [{"user": text[:500], "bot": reply[:800]}])[-HISTORY_KEEP:]
    f.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def snapshot() -> str:
    """Claude 답변용 현재 상태 (읽기 전용). Reddit 방에서는 Reddit 후보·커뮤니티 기준."""
    if room() == "reddit":
        parts = []
        for name, args in (("Reddit 후보", ["pipeline.reddit", "list"]), ("Reddit 커뮤니티", ["pipeline.reddit", "communities"])):
            try:
                parts.append(f"[{name}]\n{run(args)[:2500]}")
            except (OSError, subprocess.TimeoutExpired) as e:
                parts.append(f"[{name}] 읽지 못함: {type(e).__name__}")
        return "\n\n".join(parts)
    parts = []
    for name, args in (("블로그 일정", ["pipeline.site", "schedule"]), ("검수 대기", ["pipeline.03_review", "list"]), ("데이터 글", ["pipeline.data_notes", "list"])):
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
    if room() == "reddit":
        parts = [(f"log {n}", _log_tail(n)) for n in REDDIT_EVIDENCE_LOGS]
        parts.append(("reddit state.json", _json_text(content_dir() / "reddit" / "state.json")))
        return scrub("\n\n".join(f"--- {title} ---\n{body}" for title, body in parts if body))[:EVIDENCE_CHARS]
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
        data, _ = llm.generate_json("telegram_chat", prompt("telegram_chat", help=REDDIT_HELP if room() == "reddit" else HELP,
                                                            status=snapshot(), history=history,
                                                            message=text, evidence=evidence()))
    except llm.RateLimited as e:
        log.warning("telegram_chat 한도: %s", e)
        return f"⏳ Claude 사용량 한도라 지금은 말로 된 요청을 처리할 수 없습니다 ({scrub(str(e))[:120]}). 시간이 지난 뒤 다시 보내 주세요. 정해진 명령은 그대로 됩니다."
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
    return bool(re.search(r"(수정|되돌리기|검사|방향)\s*[:：]", chunk))


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
    if blocked := wrong_room(cmd):
        return blocked
    if "reply" in cmd:
        return REDDIT_HELP if room() == "reddit" and cmd["reply"] == HELP else cmd["reply"]
    if "show" in cmd:
        return show(cmd["show"], cmd["part"])
    log.info("명령: %s", cmd["args"][:2])
    return run(cmd["args"])


def _reddit_request(text: str) -> list[str] | None:
    """Reddit 글 주소를 보내면 그 글에 답하자는 뜻. 주소만 왔으면 다음 메시지(글 내용)를 이어 받는다."""
    reddit = importlib.import_module("pipeline.reddit")
    if not re.match(r"\s*(레딧|reddit|언급)", text, re.I) and (m := reddit.THREAD.search(text)):
        rest = (text[:m.start()] + " " + text[m.end():]).strip()
        rest = re.sub(r"\?\S*", "", rest, count=1).strip() if rest.startswith("?") else rest  # 공유 링크 꼬리 (?utm…)
        return ["pipeline.reddit", "want", m.group(0), rest]
    if split_commands(text) is None and (url := reddit.waiting_url()):
        return ["pipeline.reddit", "want", url, text.strip()]
    if room() == "reddit" and split_commands(text) is None and len(text.strip()) >= reddit.PASTED_MIN_CHARS \
            and not YES.fullmatch(text.strip()) and not NO.fullmatch(text.strip()):
        return ["pipeline.reddit", "want-text", "--", text.strip()]  # 주소 없이 붙여 넣은 글 (캡처 텍스트 추출 등)
    return None


def handle(text: str) -> str:
    pending = _take_pending()
    if pending and YES.fullmatch(text.strip()):
        return run_chunks(pending)
    if pending and NO.fullmatch(text.strip()):
        return "취소했습니다."
    if args := _reddit_request(text):
        if room() == "main":
            return "⚠️ Reddit 글은 Reddit 방에 보내 주세요."
        return run(args)
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
    load_env_file(override=True)  # 게이트웨이에 남은 옛 키가 아니라 .env 를 쓴다 (크론 스크립트와 같게, 2026-11-02)
    text = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    if not text:
        return 0
    worker = importlib.import_module("pipeline.worker")
    keys = os.environ.get("SKIN_TG_SOURCE_KEYS", "")
    log.info("받은 방: %s%s", origin() or "(모름 → 기본 방으로 답)", f" — 플러그인이 본 속성: {keys}" if keys else "")
    if split_commands(text) is None and not YES.fullmatch(text) and not NO.fullmatch(text) and not _reddit_request(text):
        reply_to("🔎 확인 중입니다")  # 기록을 읽는 데 1분 안팎 걸린다
    try:
        reply = handle(text)
    except subprocess.TimeoutExpired:
        reply = "⚠️ 시간이 너무 오래 걸려 중단했습니다"
    remember(text, reply)
    if not reply_to(reply):
        log.error("답장 전송 실패")
        return 1
    return 0


if __name__ == "__main__":
    run_cli("telegram", main)
