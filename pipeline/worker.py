"""Hermes 작업 워커 — 텔레그램 답장으로 생긴 요청을 처리하고, 끝나면 검수 요청 메시지를 출력한다.

Hermes 에이전트(텔레그램 대화)는 오래 걸리는 작업을 직접 하지 않고 요청 파일만 남긴다:
    python -m pipeline.worker request-drafts --pick 1,3,4 [--week 2026-W40]   # 주제 선택 답장
    python -m pipeline.worker request-sponsored --sponsor example-clinic --title "..." --angle "..."  # 스폰서 글
    (수정 요청은 03_review apply "2 수정: ..." 가 자동으로 요청 파일을 남긴다)

no-agent 크론이 주기적으로 실행한다 (stdout 이 그대로 텔레그램으로 간다):
    python -m pipeline.worker run
  1. 요청 처리: 초안 생성(02_draft) / 수정 재생성
  2. 자동 검수: 검수 전 초안 → 02b (⛔ 면 1회 자동 재생성)
  3. Claude Code 검수: 02c review_cycle (export → claude -p → import)
  4. 위에서 바뀐 것이 있으면 03_review list 메시지 출력, 없으면 아무것도 출력하지 않음 (조용한 틱)
동시에 두 번 실행되면 나중 것은 바로 끝난다 (잠금). 실패가 있으면 원인을 stderr 에 남기고 종료 코드 1.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
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
    return f"초안 생성 요청 접수 ({week}): {titles}\n생성·자동 검수·Claude 검수가 끝나면 검수 요청을 보냅니다."


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


def run(review_runner=None) -> tuple[str, list[str]]:
    """반환: (텔레그램으로 보낼 메시지 — 없으면 "", 실패 목록)"""
    review_mod = importlib.import_module("pipeline.02b_auto_review")
    external_mod = importlib.import_module("pipeline.02c_external_review")
    human_mod = importlib.import_module("pipeline.03_review")
    changed, failures = False, []

    for path in sorted(requests_dir().glob("*.json")):
        req = load_json(path)
        try:
            _process(req)
            path.unlink()
        except llm.RateLimited as e:  # 사용량 한도·시간 초과 — 요청을 그대로 두고 다음 실행 때 다시
            failures.append(f"{req.get('kind')} 요청 보류 (다음 실행 때 재시도): {e}")
            log.warning("요청 보류: %s — %s", path.name, e)
            break  # 같은 한도에 걸릴 나머지 요청도 다음 실행으로
        except Exception as e:  # noqa: BLE001 — 한 요청 실패가 나머지를 막지 않게
            failures.append(f"{req.get('kind')} 요청 실패: {e}")
            log.exception("요청 실패: %s", path.name)
            failed = requests_dir() / "failed"  # 같은 요청을 매 틱 재시도하며 비용을 쓰지 않도록 격리
            failed.mkdir(parents=True, exist_ok=True)
            path.replace(failed / path.name)
        changed = True

    for path in review_mod.unreviewed_drafts():
        try:
            review_mod.review_with_regeneration(path)
            changed = True
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

    message = human_mod.list_message("drafts") if changed else ""

    render_mod = importlib.import_module("pipeline.04_render_video")
    if render_mod.tts_configured() and render_mod.pending():  # 승인된 초안 → 영상 (한 번에 2건, TTS 비용·시간 제한)
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
