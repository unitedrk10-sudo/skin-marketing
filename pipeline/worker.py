"""Hermes 작업 워커 — 텔레그램 답장으로 생긴 요청을 처리하고, 끝나면 검수 요청 메시지를 출력한다.

Hermes 에이전트(텔레그램 대화)는 오래 걸리는 작업을 직접 하지 않고 요청 파일만 남긴다:
    python -m pipeline.worker request-drafts --pick 1,3,4 [--week 2026-W40]   # 주제 선택 답장
    python -m pipeline.worker request-sponsored --sponsor example-clinic --title "..." --angle "..."  # 스폰서 글
    (수정 요청은 03_review apply "2 수정: ..." 가 자동으로 요청 파일을 남긴다)

no-agent 크론이 주기적으로 실행한다 (stdout 이 그대로 텔레그램으로 간다):
    python -m pipeline.worker run
  1. 요청 처리: 초안 생성(02_draft — Gemini 출처 찾기 → Claude 작성) / 수정 재생성.
     Claude 사용량 한도·시간 초과(llm.RateLimited)면 요청을 그대로 두고 다음 실행 때 다시 한다.
  2. 자동 검수: 검수 전 초안 → 02b (Gemini, ⛔ 면 1회 자동 재생성)
  3. (선택) Claude Code 외부 검수: 검수 단계가 claude_code 일 때만 02c review_cycle
  4. 위에서 바뀐 것이 있으면 03_review list 메시지 출력, 없으면 아무것도 출력하지 않음 (조용한 틱)
동시에 두 번 실행되면 나중 것은 바로 끝난다 (잠금). 실패가 있으면 원인을 stderr 에 남기고 종료 코드 1.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO

from pipeline import llm
from pipeline.common import content_dir, get_logger, load_json, now_iso, run_cli, save_json

log = get_logger("worker")


def requests_dir() -> Path:
    return content_dir() / "requests"


def try_lock(f: IO) -> bool:
    """배타 잠금을 비차단으로 시도. 다른 프로세스가 잡고 있으면 False. 파일을 닫으면 풀린다."""
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError(fcntl) / PermissionError(msvcrt)
        return False
    return True


def enqueue(kind: str, **data) -> Path:
    path = requests_dir() / f"{time.strftime('%Y%m%dT%H%M%S')}-{time.monotonic_ns() % 10**6:06d}-{kind}.json"
    save_json(path, {"kind": kind, "created_at": now_iso(), **data})
    log.info("요청 접수: %s", path.name)
    return path


def latest_week() -> str:
    files = sorted(content_dir("topics").glob("*.json"))
    if not files:
        raise ValueError("주제 후보가 없습니다 (01_topics 먼저)")
    return files[-1].stem


def request_drafts(pick: str, week: str | None = None) -> str:
    """주제 번호를 바로 검증하고(잘못된 번호는 대화에서 즉시 알려주도록) 요청을 남긴다."""
    draft_mod = importlib.import_module("pipeline.02_draft")
    week = week or latest_week()
    topics = load_json(content_dir("topics") / f"{week}.json")["topics"]
    nums = draft_mod.parse_pick(pick, len(topics))
    enqueue("drafts", week=week, pick=nums)
    titles = ", ".join(f"{n}. {topics[n - 1]['title']}" for n in nums)
    return f"초안 생성 요청 접수 ({week}): {titles}\n출처 확보·작성·자동 검수가 끝나면 검수 요청을 보냅니다 (편당 5~10분)."


def request_sponsored(sponsor_id: str, title: str, angle: str, keywords: list[str] | None = None, course: bool = False) -> str:
    """스폰서 등록·계약 기간을 바로 확인하고 요청을 남긴다."""
    sponsors = importlib.import_module("pipeline.sponsors")
    sponsor = sponsors.get(sponsor_id)
    if not sponsors.contract_active(sponsor):
        raise ValueError(f"{sponsor_id}: 계약 기간이 아닙니다 ({sponsor['contract']['start']} ~ {sponsor['contract']['end']})")
    if course and not sponsor.get("zone"):
        raise ValueError(f"{sponsor_id}: 코스 글에는 sponsors.yaml 의 zone(병원 권역)이 필요합니다")
    enqueue("sponsored", sponsor=sponsor_id, title=title, angle=angle, keywords=keywords or [], course=course)
    return (f"💼 스폰서 글 요청 접수: {sponsor['name_ko']} — {title}\n"
            "생성·검수가 끝나면 검수 요청을 보냅니다. 병원 확인(`N 병원확인`) 후에 승인할 수 있습니다.")


def _process(req: dict) -> None:
    draft_mod = importlib.import_module("pipeline.02_draft")
    if req["kind"] == "drafts":
        topics = load_json(content_dir("topics") / f"{req['week']}.json")["topics"]
        for n in req["pick"]:
            draft_mod.create(req["week"], n, topics[n - 1])
    elif req["kind"] == "sponsored":
        draft_mod.create_sponsored(req["sponsor"], req["title"], req["angle"], req.get("keywords"), req.get("course", False))
    elif req["kind"] == "revise":
        draft_mod.revise(req["draft_id"], req["note"])
    else:
        raise ValueError(f"알 수 없는 요청: {req['kind']}")


def hermes_send(text: str) -> bool:
    """Hermes 로 텔레그램(홈 채널)에 메시지 하나를 보낸다 — 길면 Hermes 가 나눠 보낸다."""
    hermes = os.environ.get("HERMES_BIN") or shutil.which("hermes")
    if not hermes:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as f:
        f.write(text)
    try:
        proc = subprocess.run([hermes, "send", "-t", os.environ.get("SKIN_DELIVER", "telegram"), "-f", f.name, "-q"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("hermes send 실패: %s", e)
        return False
    finally:
        os.unlink(f.name)
    if proc.returncode != 0:
        log.warning("hermes send 실패 (%s): %s", proc.returncode, (proc.stderr or proc.stdout)[-300:])
    return proc.returncode == 0


def send_each(messages: list[str], sender=None) -> bool:
    """전부 보냈으면 True. 하나라도 실패하면 False (워커가 한 메시지 + 첨부 방식으로 다시 보낸다)."""
    sender = sender or hermes_send
    return bool(messages) and all(sender(m) for m in messages)


RESET_AT = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.I)
HOLD_DEFAULT = timedelta(minutes=30)


def hold_file() -> Path:
    return content_dir() / "claude_hold.json"  # requests/ 밖에 둔다 (워커가 요청으로 읽지 않게)


def claude_hold(now: datetime | None = None) -> datetime | None:
    """Claude 사용량 한도로 쉬는 중이면 다시 시도할 시각, 아니면 None."""
    path = hold_file()
    if not path.exists():
        return None
    until = datetime.fromisoformat(load_json(path)["until"])
    return until if (now or datetime.now()) < until else None


def set_hold(error: str, now: datetime | None = None) -> datetime:
    """한도 메시지의 초기화 시각("resets 11:30am")까지 쉰다 (+2분 여유). 시각을 못 읽으면 30분."""
    now = now or datetime.now()
    m = RESET_AT.search(error)
    until = now + HOLD_DEFAULT
    if m:
        hour, minute = int(m.group(1)) % 12 if m.group(3) else int(m.group(1)), int(m.group(2) or 0)
        hour += 12 if (m.group(3) or "").lower() == "pm" else 0
        until = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        until += timedelta(days=1) if until <= now else timedelta(0)
    until += timedelta(minutes=2)
    save_json(hold_file(), {"until": until.isoformat(timespec="minutes"), "reason": error[:200], "at": now_iso()})
    return until


def run(review_runner=None, sender=None) -> tuple[str, list[str]]:
    """반환: (텔레그램으로 보낼 메시지 — 없으면 "", 실패 목록)"""
    review_mod = importlib.import_module("pipeline.02b_auto_review")
    external_mod = importlib.import_module("pipeline.02c_external_review")
    human_mod = importlib.import_module("pipeline.03_review")
    changed, failures = False, []

    if claude_hold():  # 사용량 한도로 쉬는 중 — Claude 를 부를 수 있는 단계(작성·재생성)는 조용히 건너뛴다
        log.info("Claude 사용량 한도 대기 중 (%s 까지) — 건너뜀", claude_hold().strftime("%H:%M"))
        return "", []
    hold_file().unlink(missing_ok=True)
    notices: list[str] = []

    for path in sorted(requests_dir().glob("*.json")):
        req = load_json(path)
        try:
            _process(req)
            path.unlink()
        except llm.RateLimited as e:  # 사용량 한도·시간 초과 — 요청을 그대로 두고 초기화 시각 이후 다시
            until = set_hold(str(e))
            log.warning("요청 보류: %s — %s", path.name, e)
            notices.append(f"⏳ Claude 사용량 한도 — 초안 작성은 {until:%H:%M} 이후 자동으로 다시 시작합니다.")
            break  # 같은 한도에 걸릴 나머지 요청도 다음 실행으로
        except Exception as e:  # noqa: BLE001 — 한 요청 실패가 나머지를 막지 않게
            failures.append(f"{req.get('kind')} 요청 실패: {e}")
            log.exception("요청 실패: %s", path.name)
            failed = requests_dir() / "failed"  # 같은 요청을 매 틱 재시도하며 비용을 쓰지 않도록 격리
            failed.mkdir(parents=True, exist_ok=True)
            path.replace(failed / path.name)
        changed = True

    for path in [] if claude_hold() else review_mod.unreviewed_drafts():
        try:
            review_mod.review_with_regeneration(path)
            changed = True
        except llm.RateLimited as e:  # 차단 → 재생성하다 한도 (02b 가 검수 기록을 지워 다음에 다시 한다)
            until = set_hold(str(e))
            log.warning("재생성 보류: %s — %s", path.name, e)
            notices.append(f"⏳ Claude 사용량 한도 — 차단된 초안 재생성은 {until:%H:%M} 이후 다시 합니다.")
            break
        except Exception as e:  # noqa: BLE001
            failures.append(f"{path.name} 자동 검수 실패: {e}")
            log.exception("자동 검수 실패: %s", path.name)

    if llm.is_external("source_check") or llm.is_external("cross_review"):
        try:
            lines = external_mod.review_cycle(review_runner or external_mod.run_claude)
            changed = changed or bool(lines)
        except Exception as e:  # noqa: BLE001
            failures.append(f"Claude Code 검수 실패 (다음 실행 때 재요청): {e}")
            log.exception("Claude Code 검수 실패")
            changed = True  # ⏳ 상태라도 알려준다

    message = ""
    if changed:
        # 초안마다 메시지를 따로 보내고, 크론 출력(마지막에 도착)은 번호·답장 예시만 담은 요약으로.
        # 따로 보낼 수 없으면 한 메시지 + 초안별 첨부 파일로.
        summary = human_mod.list_message("drafts", compact=True)
        sent = summary != "검수 대기 없음" and send_each(human_mod.draft_messages("drafts"), sender)
        message = summary if sent else human_mod.list_message("drafts")
    if notices:  # 한도 대기 안내는 실패가 아니다 — 대기에 들어갈 때 한 번만 나간다 (이후 실행은 조용히 건너뜀)
        message = "\n".join(dict.fromkeys(notices)) + ("\n\n" + message if message else "")

    render_mod = importlib.import_module("pipeline.04_render_video")
    if render_mod.ready() and render_mod.pending():  # 승인된 초안 → 사진 넘기기형·영상 (한 번에 2건)
        done, render_failures = render_mod.render_pending(limit=2)
        failures += [f"렌더링 실패 {f}" for f in render_failures]
        if done:
            message = (message + "\n\n" if message else "") + human_mod.list_message("rendered")
    publish_mod = importlib.import_module("pipeline.06_publish")
    try:
        kits = publish_mod.kits_message()  # 게시 OK 된 영상 → 채널별 게시 키트
    except Exception as e:  # noqa: BLE001
        failures.append(f"게시 키트 생성 실패: {e}")
        kits = ""
    if kits:
        message = (message + "\n\n" if message else "") + kits

    social_mod = importlib.import_module("pipeline.social")
    try:
        if social_mod.pending_paths():  # 블로그에 게시된 새 글 → X·Threads 글 3종 (한 번에 3건, 보내기는 오늘의 글로)
            made, social_failures = social_mod.compose_pending(limit=3)
            failures += [f"SNS 글 생성 실패 {f}" for f in social_failures]
            blocked = [f"⛔ SNS 글 검사 실패: {d['title']} [{social_mod.LABEL[k]}] — {'; '.join(i['problems'][:2])}"
                       for name in made for d in [social_mod.load_social(social_mod._file(name).parent)]
                       for k, i in d["items"].items() if i["status"] == "blocked"]
            if blocked:
                message = (message + "\n\n" if message else "") + "\n".join(blocked)
        daily = social_mod.daily_message()  # 오전 9시(한국 시간) 이후 하루 한 번 — 오늘 올릴 글 1개
        if daily:
            message = (message + "\n\n" if message else "") + daily
    except Exception as e:  # noqa: BLE001 — SNS 글 실패가 워커 전체를 멈추지 않게
        failures.append(f"SNS 글 처리 실패: {e}")
        log.exception("SNS 글 처리 실패")
    if failures:
        message = (message + "\n\n" if message else "") + "\n".join(f"⚠️ {f}" for f in failures)
    return message, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes 작업 워커")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rd = sub.add_parser("request-drafts")
    rd.add_argument("--pick", required=True, help="주제 번호, 예: 1,3,4")
    rd.add_argument("--week", help="기본: 가장 최근 주제 후보")
    rs = sub.add_parser("request-sponsored")
    rs.add_argument("--sponsor", required=True)
    rs.add_argument("--title", required=True)
    rs.add_argument("--angle", required=True)
    rs.add_argument("--keywords", default="", help="쉼표로 구분")
    rs.add_argument("--course", action="store_true", help="병원 권역 중심 여행 코스 글")
    sub.add_parser("run")
    args = parser.parse_args(argv)

    if args.cmd == "request-sponsored":
        try:
            keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
            print(request_sponsored(args.sponsor, args.title, args.angle, keywords, args.course))
        except ValueError as e:  # SponsorError 포함
            print(f"❓ {e}")
            return 2
        return 0

    if args.cmd == "request-drafts":
        try:
            print(request_drafts(args.pick, args.week))
        except ValueError as e:
            print(f"❓ {e}")
            return 2
        return 0

    requests_dir().mkdir(parents=True, exist_ok=True)
    with (requests_dir() / ".lock").open("w") as lock:
        if not try_lock(lock):
            log.info("이전 실행이 진행 중 — 건너뜀")
            return 0
        message, failures = run()
    if message:
        print(message)
    for f in failures:
        log.error(f)
    return 1 if failures else 0


if __name__ == "__main__":
    run_cli("worker", main)
