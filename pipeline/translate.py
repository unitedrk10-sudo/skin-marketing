"""검수용 한국어 번역 — 텔레그램 검수 메시지·첨부 파일을 한국어로 (2026-10-10).

게시되는 글은 영어 그대로다. 번역은 사람 검수를 빠르게 하려는 참고용이라 저장소 밖으로 나가지 않는다.
초안 내용(draft.json)과 검수 결과가 바뀌면 다시 번역한다 (content/<상태>/<id>/review_ko.json).
번역은 쓰기·검수가 아니므로 값싼 Gemini Flash (stage translate). 실패하면 영어 원문으로 보낸다.

    python -m pipeline.translate <draft_id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pipeline import llm
from pipeline.common import shortform_on, draft_dirs, get_logger, load_draft, load_json, load_review, read_prompt, render, run_cli, save_json

log = get_logger("translate")

KEYS = ("findings", "medical", "practice", "script")


def source_texts(path: Path) -> dict:
    """번역할 원문 — 검수 메시지에 들어가는 것과 같은 순서."""
    human = __import__("importlib").import_module("pipeline.03_review")
    draft, review = load_draft(path), load_review(path)
    from pipeline.common import PRACTICE_KIND
    return {"findings": [f["message"] for f in human._issues(review)],
            "medical": [t for t, _ in human.medical_sentences(draft)],
            "practice": [t for t, _ in human.medical_sentences(draft, lambda f: f.get("kind") == PRACTICE_KIND)],
            "script": [ln.get("voice", "") for ln in (draft.get("shortform") or {}).get("lines", [])] if shortform_on() else [],
            "blog": (draft.get("blog") or {}).get("markdown", "")}


def _fingerprint(texts: dict) -> str:
    return hashlib.sha256(json.dumps(texts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def cached(path: Path) -> dict | None:
    """지금 내용과 맞는 번역이 있으면 돌려준다 (LLM 호출 없음)."""
    f = path / "review_ko.json"
    if not f.exists():
        return None
    data = load_json(f)
    try:
        return data if data.get("hash") == _fingerprint(source_texts(path)) else None
    except (OSError, KeyError, ValueError):
        return None


def ensure(path: Path) -> dict | None:
    """번역이 없거나 낡았으면 만든다. 실패하면 None (영어로 보낸다)."""
    if (hit := cached(path)) is not None:
        return hit
    texts = source_texts(path)
    try:
        data, _ = llm.generate_json("translate", render(read_prompt("translate_review"),
                                                       payload=json.dumps(texts, ensure_ascii=False, indent=1)))
    except llm.LLMError as e:
        log.warning("검수용 번역 실패 (영어로 보냄): %s — %s", path.name, e)
        return None
    if not isinstance(data, dict) or any(len(data.get(k) or []) != len(texts[k]) for k in KEYS) \
            or not isinstance(data.get("blog"), str):
        log.warning("검수용 번역 형식 불일치 (영어로 보냄): %s", path.name)
        return None
    out = {"hash": _fingerprint(texts), **{k: [str(x) for x in data[k]] for k in KEYS}, "blog": data["blog"]}
    save_json(path / "review_ko.json", out)
    return out


def ensure_stage(stage: str = "drafts") -> int:
    """그 상태의 초안 전부 (워커가 검수 메시지를 보내기 전에 부른다). 반환: 번역이 있는 초안 수."""
    return sum(1 for p in draft_dirs(stage) if (p / "draft.json").exists() and ensure(p) is not None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검수용 한국어 번역")
    parser.add_argument("draft_id")
    args = parser.parse_args(argv)
    from pipeline.common import find_draft
    _, path = find_draft(args.draft_id)
    data = ensure(path)
    print(data["blog"][:2000] if data else "번역 실패 — 로그 확인")
    return 0 if data else 1


if __name__ == "__main__":
    run_cli("translate", main)
