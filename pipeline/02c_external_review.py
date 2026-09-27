"""2.5b Claude Code 외부 검수 — ANTHROPIC_API_KEY 없이 검수 단계(② 본문 대조, ③ 교차 검수)를 처리한다.

config/models.yaml 에서 source_check / cross_review 가 provider: claude_code 일 때 쓴다.

흐름 (hermes/cron_jobs.md):
  1. Hermes: 02b_auto_review 실행 → ⏳ 대기 초안 생김 (규칙 검사·출처 페이지 수집 완료)
  2. Hermes: `02c_external_review export` → review-queue/pending/<packet_id>.json 을 커밋·푸시
  3. Claude Code: 요청 파일을 검수해 review-queue/done/<packet_id>.result.json 을 커밋·푸시
  4. Hermes: git pull → `02c_external_review import` → 03_review list 로 텔레그램 검수 요청

    python -m pipeline.02c_external_review export          # 대기 초안이 없으면 아무것도 만들지 않음
    python -m pipeline.02c_external_review import          # review-queue/done/ 결과 반영 (이미 반영된 건 무시)
    python -m pipeline.02c_external_review import <result.json>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
from pathlib import Path

from pipeline.common import (
    ROOT,
    blog_text,
    content_dir,
    draft_dirs,
    get_logger,
    iso_week,
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
review_mod = importlib.import_module("pipeline.02b_auto_review")
Finding = review_mod.Finding


def queue_dir(kind: str) -> Path:
    return ROOT / "review-queue" / kind


def draft_hash(path: Path) -> str:
    """내보낸 뒤 초안이 재생성되면 결과를 반영하지 않기 위한 지문."""
    return hashlib.sha256((path / "draft.json").read_bytes()).hexdigest()[:16]


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
    }


def already_requested(path: Path) -> bool:
    req = (load_review(path) or {}).get("external_request") or {}
    return req.get("draft_hash") == draft_hash(path)


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
    return lines  # 이미 반영된 초안은 대기 상태가 아니므로 같은 결과를 다시 import 해도 무해하다


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude Code 외부 검수 내보내기/반영")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export")
    imp = sub.add_parser("import")
    imp.add_argument("results", nargs="*", type=Path)
    args = parser.parse_args(argv)

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
