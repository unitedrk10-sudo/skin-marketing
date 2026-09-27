"""2.5b Claude Code 외부 검수 — ANTHROPIC_API_KEY 없이 검수 단계(② 본문 대조, ③ 교차 검수)를 처리한다.

config/models.yaml 에서 source_check / cross_review 가 provider: claude_code 일 때 쓴다.

흐름 (review_cycle() — pipeline/worker.py 가 호출. 모두 Hermes 머신 로컬, git 으로 주고받지 않는다):
  1. 02b_auto_review 실행 결과 ⏳ 대기 초안 (규칙 검사·출처 페이지 수집 완료)
  2. `02c_external_review export` → review-queue/pending/<packet_id>.json
  3. `claude -p` (Claude Code CLI) 가 요청 파일을 검수해 review-queue/done/<packet_id>.result.json 작성
  4. `02c_external_review import` → 등급 재산정, 요청 파일 삭제 → 03_review list 로 텔레그램 검수 요청
review-queue/ 는 미공개 초안·출처 본문이 들어 있어 git 에 올리지 않는다 (.gitignore).

    python -m pipeline.02c_external_review review          # export → claude -p → import 한 사이클
    python -m pipeline.02c_external_review export          # 대기 초안이 없으면 아무것도 만들지 않음
    python -m pipeline.02c_external_review import          # review-queue/done/ 결과 반영 (이미 반영된 건 무시)
    python -m pipeline.02c_external_review import <result.json>
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
from pathlib import Path

from pipeline.common import (
    ROOT,
    blog_text,
    content_dir,
    draft_dirs,
    draft_hash,
    get_logger,
    iso_week,
    log_dir,
    load_draft,
    load_json,
    load_review,
    now_iso,
    prompt,
    run_cli,
    save_json,
    script_text,
)

log = get_logger("02c_external_review")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_REVIEW_TIMEOUT", "1800"))  # 초
review_mod = importlib.import_module("pipeline.02b_auto_review")
Finding = review_mod.Finding


def queue_dir(kind: str) -> Path:
    return ROOT / "review-queue" / kind




def waiting(path: Path) -> bool:
    review = load_review(path)
    return bool(review) and any(f["severity"] == "pending" for f in review["findings"])


def packet_entry(path: Path) -> dict:
    draft, review = load_draft(path), load_review(path)
    return {
        "draft_id": draft["id"],
        "draft_hash": draft_hash(path),
        "title": (draft.get("shortform") or {}).get("title") or draft["topic"]["title"],
        "content_type": draft.get("content_type"),
        "facts": draft.get("facts", []),
        "script": script_text(draft),
        "blog": blog_text(draft),
        "rule_findings": [f"{f['message']}: {f['quote']}" for f in review["findings"] if f["check"] == "rules"],
        "sources": [
            {"url": s["url"], "fact_ids": s["fact_ids"], "page_excerpt": s["page_excerpt"]}
            for s in review.get("sources", []) if "page_excerpt" in s
        ],
        "needs_cross_review": bool((review.get("cross_review") or {}).get("pending")),
        "sponsor": ({k: draft["sponsor"][k] for k in ("name_en", "name_ko", "official_url")}
                    if draft.get("sponsor") else None),
    }


def already_requested(path: Path) -> bool:
    """같은 내용으로 요청했고 그 요청 파일이 아직 처리 대기 중이면 True (실패로 파일이 지워졌으면 다시 요청)."""
    req = (load_review(path) or {}).get("external_request") or {}
    return req.get("draft_hash") == draft_hash(path) and (queue_dir("pending") / f"{req.get('packet_id')}.json").exists()


def export(out_dir: Path | None = None) -> Path | None:
    """대기 초안 중 아직 요청하지 않은 것만 내보낸다 (재생성된 초안은 다시 요청)."""
    paths = [p for p in draft_dirs("drafts") if waiting(p) and not already_requested(p)]
    if not paths:
        return None
    packet_id = f"{iso_week()}-{now_iso()[:19].replace(':', '').replace('-', '')}"
    packet = {
        "packet_id": packet_id,
        "created_at": now_iso(),
        "instructions": prompt("external_review", packet_id=packet_id),
        "drafts": [packet_entry(p) for p in paths],
    }
    path = (out_dir or queue_dir("pending")) / f"{packet_id}.json"
    save_json(path, packet)
    for p, entry in zip(paths, packet["drafts"]):
        review = load_review(p)
        review["external_request"] = {"packet_id": packet_id, "draft_hash": entry["draft_hash"], "at": now_iso()}
        save_json(p / "review.json", review)
    log.info("검수 요청 %d건 내보냄: %s", len(paths), path)
    return path


def merge(path: Path, result: dict, reviewer: str, packet_id: str) -> dict:
    """외부 검수 결과를 review.json 에 반영하고 등급을 다시 매긴다."""
    review = load_review(path)
    findings = [Finding(**f) for f in review["findings"] if f["severity"] != "pending"]
    verdicts = {r.get("id"): r for r in result.get("sources", [])}
    facts = {f["id"]: f for f in load_draft(path).get("facts", [])}
    for source in review.get("sources", []):
        if "page_excerpt" not in source:
            continue
        for fid in source["fact_ids"]:
            r = verdicts.get(fid)
            if r is None:
                findings.append(Finding("sources", "caution", f"출처 대조 결과 누락 [{fid}] — 사람이 확인", source["url"]))
                continue
            source["facts"][fid] = r
            text = facts.get(fid, {}).get("text", "")
            if r.get("verdict") == "unsupported":
                findings.append(Finding("sources", "block", f"출처에 없는 주장 [{fid}]", text))
            elif r.get("verdict") == "weak":
                findings.append(Finding("sources", "caution", f"출처 근거 약함 [{fid}]", text))
        source.pop("page_excerpt")  # 반영 후에는 본문 사본을 남기지 않는다
    if (review.get("cross_review") or {}).get("pending"):
        for f in result.get("findings", []):
            findings.append(Finding("cross", "caution", f"[{f.get('where', '?')}/{f.get('severity', 'minor')}] "
                                    f"{f.get('issue', '')} → {f.get('suggestion', '')}", f.get("quote", "")))
        review["cross_review"] = {"model": reviewer, "overall": result.get("overall", ""), "raw": result.get("findings", [])}
    review["findings"] = [vars(f) for f in findings]
    review["grade"] = review_mod.grade(findings)
    review["external_review"] = {"packet_id": packet_id, "reviewer": reviewer, "imported_at": now_iso()}
    save_json(path / "review.json", review)
    return review


def import_result(result_path: Path) -> list[str]:
    data = load_json(result_path)
    lines = []
    for entry in data.get("drafts", []):
        draft_id = entry.get("draft_id", "")
        path = content_dir("drafts") / draft_id
        if not (path / "draft.json").exists():
            log.warning("drafts/ 에 없음(이미 처리됨?): %s", draft_id)
            continue
        if entry.get("draft_hash") != draft_hash(path):
            log.warning("내보낸 뒤 초안이 바뀜 → 반영 안 함, 다음 export 에 다시 포함: %s", draft_id)
            continue
        if not waiting(path):
            log.info("대기 상태가 아님, 건너뜀: %s", draft_id)
            continue
        review = merge(path, entry, data.get("reviewer", "claude-code"), data.get("packet_id", result_path.stem))
        lines.append(review_mod.summary_line(review, (load_draft(path).get("shortform") or {}).get("title", "")))
    pending = queue_dir("pending") / f"{data.get('packet_id', '')}.json"
    pending.unlink(missing_ok=True)  # 반영이 끝난 요청(출처 본문 사본 포함)은 남기지 않는다
    return lines  # 이미 반영된 초안은 대기 상태가 아니므로 같은 결과를 다시 import 해도 무해하다


class ClaudeReviewError(RuntimeError):
    pass


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def run_claude(packet_path: Path) -> Path:
    """같은 머신의 Claude Code CLI 로 검수 요청을 처리한다. 읽기·쓰기만 허용 (셸·웹 불가)."""
    packet_id = packet_path.stem
    result = queue_dir("done") / f"{packet_id}.result.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        os.environ.get("CLAUDE_BIN", "claude"), "-p",
        f"검수 요청 {_rel(packet_path)} 을 처리해줘. CLAUDE.md 의 '검수 요청 처리' 절차와 파일 안 instructions 를 따르고, "
        f"결과는 {_rel(result)} 한 파일에만 써.",
        "--allowedTools", "Read", "Write", "Glob",
        "--disallowedTools", "Bash", "WebFetch", "WebSearch",
    ]
    if os.environ.get("CLAUDE_REVIEW_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_REVIEW_MODEL"]]
    log_path = log_dir() / f"claude_review_{packet_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as out:
            subprocess.run(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=CLAUDE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        packet_path.unlink(missing_ok=True)
        raise ClaudeReviewError(f"claude 실행 실패: {e}") from e
    if not result.exists() or result.stat().st_size == 0:
        packet_path.unlink(missing_ok=True)  # 요청을 버려 다음 실행 때 같은 초안을 다시 요청하게 한다
        raise ClaudeReviewError(f"Claude Code 검수 결과 없음 (로그: {log_path})")
    return result


def review_cycle(runner=run_claude) -> list[str]:
    """export → claude -p → import. 새로 반영된 초안 요약을 돌려준다 (요청할 것이 없으면 빈 목록)."""
    packet = export()
    if packet is None:
        return []
    return import_result(runner(packet))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude Code 외부 검수 내보내기/반영")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("review")
    sub.add_parser("export")
    imp = sub.add_parser("import")
    imp.add_argument("results", nargs="*", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "review":
        for line in review_cycle():
            print(line)
        return 0
    if args.cmd == "export":
        path = export()
        print(path.relative_to(ROOT) if path and path.is_relative_to(ROOT) else (path or "검수 대기 초안 없음"))
        return 0
    # 새로 반영된 초안 요약만 출력한다 — 출력이 비어 있으면 Hermes 는 텔레그램 검수 요청을 보내지 않는다
    for result_path in args.results or sorted(queue_dir("done").glob("*.result.json")):
        for line in import_result(result_path):
            print(line)
    return 0


if __name__ == "__main__":
    run_cli("02c_external_review", main)
