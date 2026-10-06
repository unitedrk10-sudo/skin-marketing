import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("hermes_install", Path(__file__).parent.parent / "hermes" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

PRINT_ENV = ("import os; print(os.environ.get('GEMINI_API_KEY'), os.environ.get('EMPTY', '-'), 'PYTHONPATH' in os.environ, "
             "os.environ.get('HERMES_BIN'))")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo with space"  # 실제 저장소 경로에도 공백이 있다
    repo.mkdir()
    (repo / ".env").write_text("# 주석\nGEMINI_API_KEY=abc\r\nEMPTY=\n", encoding="utf-8")
    return repo


def run_script(path: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, **env})


def test_script_loads_env_and_drops_hermes_pythonpath(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "JOBS", [("skin-test", "every 1h", [{"cmd": ["{python}", "-c", PRINT_ENV]}])])
    [script] = install.write_scripts(tmp_path / "scripts", repo, sys.executable, "claude", "git", "C:/hermes/hermes.exe")
    assert script.name == "skin-test.py"  # .sh 가 아니어야 Hermes 가 bash(WSL) 대신 Python 으로 실행
    proc = run_script(script, PYTHONPATH="/hermes/venv", EMPTY="keep")
    assert proc.returncode == 0, proc.stderr
    # 빈 값은 기존 값을 덮지 않음, CR 제거, 워커가 초안별 메시지를 보낼 hermes 경로 전달
    assert proc.stdout.split() == ["abc", "keep", "False", "C:/hermes/hermes.exe"]


def test_script_failure_handling(repo, tmp_path, monkeypatch):
    fail = ["{python}", "-c", "import sys; sys.exit(3)"]
    monkeypatch.setattr(install, "JOBS", [
        ("skin-soft", None, [{"cmd": fail, "on_fail": "건너뜀 알림"}, {"cmd": ["{python}", "-c", "print('next')"]}]),
        ("skin-hard", None, [{"cmd": fail}, {"cmd": ["{python}", "-c", "print('never')"]}]),
    ])
    soft, hard = install.write_scripts(tmp_path / "scripts", repo, sys.executable, "claude", "git")
    proc = run_script(soft)
    assert (proc.returncode, proc.stdout.strip()) == (0, "next") and "건너뜀 알림" in proc.stderr
    proc = run_script(hard)
    assert proc.returncode == 3 and "never" not in proc.stdout  # 실패 코드가 Hermes 실패 알림으로


def fake_hermes(monkeypatch, existing: set[str], reject: set[str] = frozenset()):
    """hermes cron list/create 흉내. reject 에 든 이름은 생성에 실패하지만 종료 코드는 0 (실제 hermes 동작)."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["cron", "create"] and cmd[cmd.index("--name") + 1] not in reject:
            existing.add(cmd[cmd.index("--name") + 1])
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(existing))

    monkeypatch.setattr(install.shutil, "which", lambda name: name)
    monkeypatch.setattr(install.subprocess, "run", run)
    return calls


def test_register_only_selected_jobs(monkeypatch):
    calls = fake_hermes(monkeypatch, {"skin-worker"})
    install.register_jobs("telegram", {"skin-worker", "skin-weekly-topics"})
    created = [c for c in calls if c[1:3] == ["cron", "create"]]
    assert [c[c.index("--name") + 1] for c in created] == ["skin-weekly-topics"]  # worker 는 이미 있음
    assert created[0][3] == "0 9 * * 1" and created[0][created[0].index("--script") + 1] == "skin-weekly-topics.py"


def test_register_reports_silent_hermes_failures(monkeypatch):
    fake_hermes(monkeypatch, set(), reject={"skin-weekly-report"})
    with pytest.raises(SystemExit, match="skin-weekly-report"):
        install.register_jobs("telegram", {"skin-worker", "skin-weekly-report"})


def test_job_schedules_use_formats_hermes_accepts():
    for name, schedule, _ in install.JOBS:
        if schedule:
            assert re.fullmatch(r"(every )?\d+[mhd]|(\S+ ){4}\S+", schedule), (name, schedule)


def test_skill_paths_are_quoted(tmp_path):
    path = install.install_skill(tmp_path, install.REPO, sys.executable)
    text = path.read_text(encoding="utf-8")
    assert "{{" not in text
    assert f'cd "{install.REPO.as_posix()}" &&' in text
