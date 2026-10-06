"""Hermes 운영 등록 — Hermes 가 도는 머신에서 한 번 실행한다 (다시 실행해도 안전). Windows·macOS·Linux 공통.

    python hermes/install.py            # Windows: .venv\\Scripts\\python hermes\\install.py

하는 일:
  1. 파이썬 의존성 설치, .env 준비, Claude Code CLI 확인
  2. <HERMES_HOME>/scripts/ 에 no-agent 크론 스크립트 생성 (저장소 경로·python·claude 경로 고정)
  3. <HERMES_HOME>/skills/skin-marketing/ 에 텔레그램 답장 처리 스킬 설치
  4. hermes cron 작업 등록 (이미 있으면 건너뜀)

크론 스크립트는 .py 로 만든다. Hermes 는 .sh 를 PATH 의 bash 로 실행하는데, Windows 에서는 그 bash 가
WSL 실행기(system32\\bash.exe)라 저장소·파이썬을 못 찾는다. .py 는 Hermes 자신의 Python 이 실행한다.

바꿀 수 있는 값 (옵션 또는 환경변수): --python/PYTHON, --claude/CLAUDE_BIN, --hermes-home/HERMES_HOME,
--deliver/HERMES_DELIVER(기본 telegram)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (이름, 스케줄, 단계) — 스케줄은 '10m'·'every 1h' 같은 간격 또는 cron 식 (로컬 시간, "0 9 * * 1" 형식은 Hermes 가 거부)
# 단계: {"cmd": [...], "on_fail": None(중단·종료 코드 전달) | "메시지"(stderr 에 남기고 계속),
#                               "quiet": True(출력을 stderr 로 — 텔레그램에 안 감)}
# "{python}", "{claude}", "{git}" 은 설치 시 실제 경로로 바뀐다.
def _py(*args: str) -> dict:
    return {"cmd": ["{python}", "-m", *args]}


JOBS: list[tuple[str, str | None, list[dict]]] = [
    ("skin-law-sync", "0 8 * * 1", [_py("pipeline.law_sync")]),
    # 주간 주제: 먼저 코드 업데이트 (law_sync 가 갱신한 legal/ 은 버리고 받음 — 다음 동기화 때 다시 생성됨)
    ("skin-weekly-topics", "0 9 * * 1", [
        {"cmd": ["{git}", "checkout", "-q", "--", "legal/"], "on_fail": "", "quiet": True},
        {"cmd": ["{git}", "pull", "-q", "--ff-only"], "on_fail": "(코드 업데이트 실패 — 기존 코드로 진행)", "quiet": True},
        _py("pipeline.01_topics", "--from-seed", "6"),  # 초기 주제 목록 소진 후 자동으로 Gemini 조사
    ]),
    ("skin-worker", "every 10m", [_py("pipeline.worker", "run")]),
    ("skin-traffic-report", "0 10 * * 1", [_py("pipeline.tracker", "report", "--days", "7")]),
    # 블로그: 승인된 글이 바뀌었을 때만 Cloudflare Pages 배포 (없으면 조용)
    ("skin-site", "every 1h", [_py("pipeline.site", "deploy")]),
    ("skin-weekly-report", "0 20 * * 0", [_py("pipeline.07_report")]),
    # 코스 주변 피부과 목록 갱신 (심평원 공공데이터, 30일 지난 것만, 키 없으면 조용)
    ("skin-clinics", "0 7 * * 1", [_py("pipeline.clinics", "refresh")]),
    # 월간 유입 분석 (영업용 데이터셋·리포트, 추적기 미설정·클릭 없으면 조용)
    ("skin-analytics", "0 11 1 * *", [_py("pipeline.analytics", "report")]),  # 매월 1일 11:00 (지난달)
    # 점검용 (크론 아님): 크론과 같은 환경에서 키·모델·claude 확인
    ("skin-check", None, [
        {**_py("pipeline.llm", "check"), "on_fail": ""},
        {**_py("pipeline.sponsors", "check"), "on_fail": ""},
        {"cmd": ["{claude}", "-p", "Reply with exactly: ok"], "on_fail": "claude -p 실패 — Claude Code 로그인 확인"},
    ]),
]

SCRIPT_TEMPLATE = '''\
# skin-marketing — hermes/install.py 가 생성. 수정은 저장소의 install.py 에서.
import os
import subprocess
import sys

REPO = {repo!r}
PYTHON = {python!r}
CLAUDE_BIN = {claude!r}
HERMES_BIN = {hermes!r}  # 워커가 초안별 메시지를 따로 보낼 때 쓴다 (hermes send)
STEPS = {steps!r}

# Hermes 가 얹은 파이썬 경로(자기 venv)는 빼고 저장소 파이썬만 쓰게 한다
env = {{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}}
env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", CLAUDE_BIN=CLAUDE_BIN)
if HERMES_BIN:
    env["HERMES_BIN"] = HERMES_BIN
env["PATH"] = os.pathsep.join([os.path.dirname(CLAUDE_BIN), os.path.dirname(PYTHON), env.get("PATH", "")])
# .env 의 값이 있는 항목만 환경변수로 (빈 값이 기존 환경변수를 덮지 않게)
dotenv = os.path.join(REPO, ".env")
if os.path.exists(dotenv):
    with open(dotenv, encoding="utf-8-sig") as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() and not key.startswith("#") and value.strip():
                env[key.strip()] = value.strip()

for step in STEPS:
    sys.stdout.flush()
    try:
        code = subprocess.run(step["cmd"], cwd=REPO, env=env,
                              stdout=sys.stderr if step.get("quiet") else None).returncode
    except OSError as e:
        print(f"{{step['cmd'][0]}} 실행 실패: {{e}}", file=sys.stderr)
        code = 127
    if code != 0:
        if step.get("on_fail") is None:
            sys.exit(code)
        if step["on_fail"]:
            print(step["on_fail"], file=sys.stderr)
'''


def default_hermes_home() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def default_python() -> str:
    if os.environ.get("PYTHON"):
        return os.environ["PYTHON"]
    venv = REPO / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(venv if venv.exists() else Path(sys.executable))


def write_scripts(scripts_dir: Path, repo: Path, python: str, claude: str, git: str, hermes: str = "") -> list[Path]:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    paths = {"{python}": python, "{claude}": claude, "{git}": git}
    written = []
    for name, _schedule, steps in JOBS:
        steps = [{**s, "cmd": [paths.get(a, a) for a in s["cmd"]]} for s in steps]
        path = scripts_dir / f"{name}.py"
        path.write_text(SCRIPT_TEMPLATE.format(repo=str(repo), python=python, claude=claude, hermes=hermes, steps=steps),
                        encoding="utf-8")
        written.append(path)
    return written


def install_skill(skills_dir: Path, repo: Path, python: str) -> Path:
    # 스킬 명령은 Hermes 터미널(Windows 는 Git Bash)에서 돈다 — 슬래시 경로가 양쪽에서 다 통한다
    text = (repo / "hermes/skills/skin-marketing/SKILL.md").read_text(encoding="utf-8")
    text = text.replace("{{REPO}}", repo.as_posix()).replace("{{PYTHON}}", Path(python).as_posix())
    path = skills_dir / "skin-marketing" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def register_jobs(deliver: str, only: set[str] | None = None) -> None:
    hermes = shutil.which("hermes")
    if not hermes:
        print("⚠️  hermes 명령이 없어 크론 등록을 건너뜁니다. hermes/README.md 의 명령으로 직접 등록하세요.")
        return
    def listed() -> str:
        return subprocess.run([hermes, "cron", "list"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout

    existing = listed()
    failed = []
    for name, schedule, _steps in JOBS:
        if schedule is None or (only and name not in only):
            continue
        if name in existing:
            print(f"크론 있음: {name} (건너뜀)")
            continue
        subprocess.run([hermes, "cron", "create", schedule, "--no-agent", "--script", f"{name}.py",
                        "--deliver", deliver, "--name", name])
        # hermes 는 생성에 실패해도 종료 코드 0 일 수 있어 목록으로 확인한다
        if name in listed():
            print(f"크론 등록: {name} ({schedule})")
        else:
            failed.append(name)
            print(f"⚠️  크론 등록 실패: {name} ({schedule}) — 위 hermes 메시지 확인")
    if failed:
        raise SystemExit(f"크론 등록 실패: {', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes 운영 등록")
    parser.add_argument("--python", default=default_python())
    parser.add_argument("--claude", default=os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "")
    parser.add_argument("--hermes-home", type=Path, default=default_hermes_home())
    parser.add_argument("--deliver", default=os.environ.get("HERMES_DELIVER", "telegram"))
    parser.add_argument("--jobs", help="이 크론만 등록 (쉼표 구분, 예: skin-worker,skin-weekly-topics). "
                                       "키·배포가 준비 안 된 크론은 빼 두면 실패 알림이 안 온다. 기본: 전부")
    args = parser.parse_args()
    only = {j.strip() for j in args.jobs.split(",") if j.strip()} if args.jobs else None
    unknown = (only or set()) - {name for name, schedule, _ in JOBS if schedule}
    if unknown:
        parser.error(f"없는 크론: {', '.join(sorted(unknown))}")
    print(f"저장소: {REPO}\npython: {args.python}\nHermes: {args.hermes_home}")

    # 1. 준비
    if subprocess.run([args.python, "-m", "pip", "install", "-q", "-r", str(REPO / "requirements.txt")]).returncode:
        print("⚠️  pip 설치 실패 — --python <venv 파이썬> 으로 다시 실행")
    env_file = REPO / ".env"
    if not env_file.exists():
        shutil.copy(REPO / ".env.example", env_file)
        if sys.platform != "win32":
            env_file.chmod(0o600)
        print(f"⚠️  .env 를 만들었습니다. GEMINI_API_KEY, LAW_API_OC 값을 채워주세요: {env_file}")
        print("⚠️  (Hermes 에 등록한 Gemini 키는 크론 스크립트로 전달되지 않아 여기에 따로 넣어야 합니다)")
    claude = args.claude
    if not claude:
        print("⚠️  claude 명령을 찾지 못했습니다. Claude Code 설치·로그인 후 --claude <경로> 로 다시 실행하세요.")
        claude = "claude"

    # 2. 크론 스크립트
    for path in write_scripts(args.hermes_home / "scripts", REPO, args.python, claude, shutil.which("git") or "git",
                              shutil.which("hermes") or ""):
        print(f"스크립트: {path}")
    # 3. 스킬
    print(f"스킬: {install_skill(args.hermes_home / 'skills', REPO, args.python)}")
    # 4. 크론 등록
    register_jobs(args.deliver, only)

    check = args.hermes_home / "scripts" / "skin-check.py"
    print(f"""
완료. 확인:
  1. {env_file} 에 GEMINI_API_KEY, LAW_API_OC 입력
  2. "{args.python}" "{check}"   (키·Claude Code 점검)
  3. hermes cron run skin-weekly-topics   (지금 바로 주제 후보 받아보기)""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
