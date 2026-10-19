"""Hermes 게이트웨이 지킴이 — Windows 작업 스케줄러가 10분마다 실행 (2026-10-19).

게이트웨이가 꺼져 있거나(절전·네트워크 오류 뒤 비정상 종료) 크론 심장박동이 멈춰 있으면 다시 켜고,
텔레그램으로 알린다. 2026-10-18 00:34 게이트웨이가 종료 기록 없이 사라져 하루 넘게 예약 작업이 멈춘 일이 있었다.

    pythonw hermes/watchdog.py          # 작업 스케줄러 Hermes_Watchdog 이 실행 (창 없음)
    python hermes/watchdog.py --check   # 상태만 출력
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
LOG = REPO / "logs" / "watchdog.log"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # 작업 스케줄러에서 콘솔 창이 깜빡이지 않게


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def hermes(*args: str, timeout: int = 120) -> str:
    proc = subprocess.run([str(HERMES), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)
    return (proc.stdout or "") + (proc.stderr or "")


def state() -> str:
    """running | stalled | down"""
    out = hermes("cron", "status")
    if "not running" in out.lower():
        return "down"
    if "stalled" in out.lower():
        return "stalled"
    return "running"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not HERMES.exists():
        log(f"hermes 없음: {HERMES}")
        return 1
    try:
        now = state()
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"상태 확인 실패: {e}")
        return 1
    if "--check" in argv:
        print(now)
        return 0
    if now == "running":
        return 0
    log(f"게이트웨이 {now} → {'다시 시작' if now == 'stalled' else '시작'}")
    try:
        out = hermes("gateway", "restart" if now == "stalled" else "start", timeout=180)
        log(out.strip().splitlines()[-1] if out.strip() else "(출력 없음)")
        hermes("send", "-t", "telegram", "-q",
               f"⚠️ Hermes 게이트웨이가 {'멈춰' if now == 'stalled' else '꺼져'} 있어서 자동으로 다시 켰습니다 "
               f"({datetime.now():%m/%d %H:%M}). 밀린 예약 작업은 바로 이어서 돕니다.")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"재시작 실패: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
