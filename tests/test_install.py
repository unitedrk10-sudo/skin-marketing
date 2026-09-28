import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("hermes_install", Path(__file__).parent.parent / "hermes" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

PRINT_ENV = "import os; print(os.environ.get('GEMINI_API_KEY'), os.environ.get('EMPTY', '-'), 'PYTHONPATH' in os.environ)"


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
    [script] = install.write_scripts(tmp_path / "scripts", repo, sys.executable, "claude", "git")
    assert script.name == "skin-test.py"  # .sh 가 아니어야 Hermes 가 bash(WSL) 대신 Python 으로 실행
    proc = run_script(script, PYTHONPATH="/hermes/venv", EMPTY="keep")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["abc", "keep", "False"]  # 빈 값은 기존 값을 덮지 않음, CR 제거


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


def test_skill_paths_are_quoted(tmp_path):
    path = install.install_skill(tmp_path, install.REPO, sys.executable)
    text = path.read_text(encoding="utf-8")
    assert "{{" not in text
    assert f'cd "{install.REPO.as_posix()}" &&' in text
