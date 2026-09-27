"""파이프라인 공통: 경로, 로깅, 프롬프트 렌더링, 초안 파일 입출력.

콘텐츠 상태는 폴더로 표현한다: content/<state>/<draft_id>/draft.json
테스트·스테이징에서는 SKIN_CONTENT_DIR / SKIN_LOG_DIR 로 위치를 바꿀 수 있다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPT_DIR = ROOT / "prompts"

STATES = ("topics", "drafts", "approved", "rendered", "ready_to_publish", "published", "rejected")


def content_dir(state: str | None = None) -> Path:
    base = Path(os.environ.get("SKIN_CONTENT_DIR", ROOT / "content"))
    if state is None:
        return base
    if state not in STATES:
        raise ValueError(f"알 수 없는 상태: {state}")
    return base / state


def log_dir() -> Path:
    return Path(os.environ.get("SKIN_LOG_DIR", ROOT / "logs"))


def get_logger(name: str) -> logging.Logger:
    """stderr + logs/<name>.log. Hermes 실패 알림은 종료 코드와 stderr 를 본다."""
    logger = logging.getLogger(f"skin.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    logger.addHandler(err)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir() / f"{name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger


def run_cli(name: str, fn: Callable[[], int]) -> None:
    """예외를 원인 로그 + 종료 코드 1 로 바꾼다."""
    logger = get_logger(name)
    try:
        code = fn()
    except Exception as e:  # noqa: BLE001 — 운영 스크립트 최상위
        logger.exception("실패: %s", e)
        code = 1
    sys.exit(code)


def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8")) or {}


def read_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **values) -> str:
    """{{key}} 치환. 값이 없는 키가 남으면 오류 (프롬프트 누락 방지)."""
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    missing = re.findall(r"\{\{(\w+)\}\}", out)
    if missing:
        raise KeyError(f"프롬프트 변수 누락: {missing}")
    return out


def prompt(name: str, **values) -> str:
    """생성 프롬프트에는 기획서 7장 규칙(_rules.md)이 항상 들어간다."""
    return render(read_prompt(name), rules=read_prompt("_rules"), **values)


def parse_json(text: str):
    """LLM 응답에서 JSON 을 꺼낸다 (코드펜스·앞뒤 설명 허용)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"JSON 없음: {text[:200]!r}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])


def iso_week(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "untitled"


# ---- 초안 입출력 ----

def draft_dirs(state: str) -> list[Path]:
    base = content_dir(state)
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "draft.json").exists())


def find_draft(draft_id: str) -> tuple[str, Path]:
    for state in STATES:
        path = content_dir(state) / draft_id
        if (path / "draft.json").exists():
            return state, path
    raise FileNotFoundError(f"초안 없음: {draft_id}")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_draft(path: Path) -> dict:
    return load_json(path / "draft.json")


def load_review(path: Path) -> dict | None:
    f = path / "review.json"
    return load_json(f) if f.exists() else None


def script_text(draft: dict) -> str:
    sf = draft.get("shortform") or {}
    lines = [f"TITLE: {sf.get('title', '')}", f"HOOK: {sf.get('hook', '')}"]
    for i, line in enumerate(sf.get("lines", []), 1):
        refs = ",".join(line.get("fact_ids", []))
        lines.append(f"{i}. VOICE: {line.get('voice', '')} | CAPTION: {line.get('caption', '')} | VISUAL: {line.get('visual', '')} [{refs}]")
    lines.append(f"ON-SCREEN: {sf.get('on_screen_disclosure', '')}")
    lines.append("HASHTAGS: " + " ".join(sf.get("hashtags", [])))
    return "\n".join(lines)


def blog_text(draft: dict) -> str:
    blog = draft.get("blog") or {}
    return f"# {blog.get('title', '')}\n\n_{blog.get('meta_description', '')}_\n\n{blog.get('markdown', '')}"


def facts_text(facts: list[dict]) -> str:
    return "\n".join(f"[{f['id']}] {f['text']} (source: {f.get('url', '')})" for f in facts)
