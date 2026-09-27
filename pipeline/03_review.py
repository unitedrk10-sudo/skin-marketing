"""3. 사람 검수 처리 — 텔레그램 답장을 해석해 파일 상태를 옮긴다 (기획서 6-1, 6-2).

상태 이동은 이 스크립트만 한다. 허용 이동: drafts→approved, drafts→rejected, rendered→ready_to_publish, rendered→rejected.

    python -m pipeline.03_review list [--stage drafts|rendered]        # 검수 요청 메시지 출력 + 번호표 저장
    python -m pipeline.03_review apply "1,3,4 승인" [--stage drafts]
    python -m pipeline.03_review apply "2 수정: 가격 출처 다시"
    python -m pipeline.03_review apply "5 폐기"
    python -m pipeline.03_review apply "전체 승인" [--confirm]           # ⚠️/⛔ 건은 --confirm 있어야 승인
    python -m pipeline.03_review apply "게시 OK" --stage rendered        # 또는 "1,2 게시 OK"

번호는 마지막 `list` 가 만든 번호표(<stage>/_batch.json) 기준이다.
종료 코드: 0 처리 완료, 2 답장을 해석하지 못함(사람에게 다시 묻기), 1 실행 오류.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pipeline.common import (
    content_dir,
    draft_dirs,
    get_logger,
    load_draft,
    load_json,
    load_review,
    log_dir,
    now_iso,
    run_cli,
    save_json,
)

log = get_logger("03_review")

ALLOWED_MOVES = {("drafts", "approved"), ("drafts", "rejected"), ("rendered", "ready_to_publish"), ("rendered", "rejected")}
GRADE_ICON = {"pass": "✅", "caution": "⚠️", "pending": "⏳", "block": "⛔", None: "❔"}
NUMS = r"(\d+(?:\s*[,，]\s*\d+|\s*~\s*\d+|\s*-\s*\d+|\s+\d+)*)"


class ReplyError(ValueError):
    pass


@dataclass
class Command:
    action: str  # approve | revise | reject | publish_ok
    numbers: list[int] = field(default_factory=list)  # 빈 목록 = 전체
    note: str = ""


def _numbers(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,，\s]+", text.strip()):
        rng = re.fullmatch(r"(\d+)[~-](\d+)", part)
        if rng:
            out += list(range(int(rng.group(1)), int(rng.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def parse_reply(reply: str) -> list[Command]:
    """권장 형식의 답장을 명령 목록으로. 자연어 해석은 Hermes 스킬(hermes/skills/skin-marketing/SKILL.md)이 이 형식으로 바꿔서 넘긴다."""
    commands: list[Command] = []
    # 수정 요청은 줄 끝까지가 메모이므로 줄 단위로 나눈다
    for raw in re.split(r"[\n;]+", reply):
        line = raw.strip()
        if not line:
            continue
        if m := re.fullmatch(r"(?:전체|모두|all)\s*(?:승인|approve)", line, re.I):
            commands.append(Command("approve"))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:승인|approve|ok)", line, re.I):
            commands.append(Command("approve", _numbers(m.group(1))))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:폐기|reject|drop)", line, re.I):
            commands.append(Command("reject", _numbers(m.group(1))))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:[가-힣]+\s*)?(?:수정|revise|fix)\s*[:：]\s*(.+)", line, re.I):
            commands.append(Command("revise", _numbers(m.group(1)), m.group(2).strip()))
        elif m := re.fullmatch(r"(?:" + NUMS + r"\s*(?:번)?\s*)?게시\s*ok|(?:" + NUMS + r"\s*)?publish\s*ok", line, re.I):
            commands.append(Command("publish_ok", _numbers(m.group(1) or m.group(2) or "")))
        else:
            raise ReplyError(f"해석할 수 없는 답장: {line!r}")
    if not commands:
        raise ReplyError("빈 답장")
    return commands


# ---------------- 번호표 ----------------

def batch_path(stage: str) -> Path:
    return content_dir(stage) / "_batch.json"


def build_batch(stage: str) -> list[str]:
    ids = [p.name for p in draft_dirs(stage)]
    save_json(batch_path(stage), {"stage": stage, "created_at": now_iso(), "ids": ids})
    return ids


def load_batch(stage: str) -> list[str]:
    path = batch_path(stage)
    if not path.exists():
        raise ReplyError(f"번호표가 없습니다. 먼저 `03_review list --stage {stage}` 로 검수 요청을 보내세요.")
    return load_json(path)["ids"]


def list_message(stage: str) -> str:
    ids = build_batch(stage)
    if not ids:
        return "검수 대기 없음" if stage == "drafts" else "게시 전 확인 대기 없음"
    lines = []
    if stage == "drafts":
        lines.append(f"[초안 검수 {len(ids)}건 (숏폼+블로그)] 답장 예: `1,3 승인` · `2 수정: 가격 출처 다시` · `4 폐기` · `전체 승인`")
    else:
        lines.append(f"[게시 전 확인 {len(ids)}건] 답장 예: `게시 OK` · `1,2 게시 OK` · `3 폐기`")
    for i, draft_id in enumerate(ids, 1):
        path = content_dir(stage) / draft_id
        draft, review = load_draft(path), load_review(path)
        sf = draft.get("shortform") or {}
        grade = (review or {}).get("grade")
        secs = sf.get("estimated_seconds")
        head = f"{i}. {sf.get('title') or draft['topic']['title']}" + (f" ({secs}s)" if secs else "")
        line = f"{head} {GRADE_ICON[grade]}"
        if review:
            issues = [f for f in review["findings"] if f["severity"] in ("block", "caution", "pending")]
            if issues:
                line += f" {issues[0]['message']}" + (f" 외 {len(issues) - 1}건" if len(issues) > 1 else "")
            if review.get("always_human"):
                line += f" 👤{', '.join(review['always_human'])}"
        else:
            line += " 자동검수 전"
        lines.append(line)
        lines.append(f"   📄 {path / 'script.md'}  📝 {path / 'blog.md'}")
    return "\n".join(lines)


# ---------------- 이동·기록 ----------------

def move(draft_id: str, src: str, dst: str) -> Path:
    if (src, dst) not in ALLOWED_MOVES:
        raise ValueError(f"허용되지 않은 이동: {src} → {dst}")
    source = content_dir(src) / draft_id
    target = content_dir(dst) / draft_id
    if not (source / "draft.json").exists():
        raise FileNotFoundError(f"{src}/{draft_id} 없음")
    if target.exists():
        raise FileExistsError(f"{dst}/{draft_id} 이미 있음")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    history = target / "history.json"
    events = load_json(history) if history.exists() else []
    events.append({"at": now_iso(), "from": src, "to": dst})
    save_json(history, events)
    log.info("이동: %s %s → %s", draft_id, src, dst)
    return target


def record_agreement(draft_id: str, auto_grade: str | None, decision: str) -> None:
    """신뢰 단계 판단용: 자동 등급과 사람 판단 일치 여부 (기획서 6-0)."""
    agree = (auto_grade == "pass") == (decision == "approve")
    path = log_dir() / "review_agreement.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "draft_id", "auto_grade", "human_decision", "agree"])
        w.writerow([date.today().isoformat(), draft_id, auto_grade or "", decision, int(agree)])


def _targets(cmd: Command, batch: list[str]) -> list[str]:
    if not cmd.numbers:
        return list(batch)
    bad = [n for n in cmd.numbers if not 1 <= n <= len(batch)]
    if bad:
        raise ReplyError(f"없는 번호: {bad} (1~{len(batch)})")
    return [batch[n - 1] for n in cmd.numbers]


def apply(reply: str, stage: str = "drafts", confirm: bool = False) -> list[str]:
    """답장을 처리하고 텔레그램으로 돌려줄 결과 줄들을 반환한다."""
    commands = parse_reply(reply)
    batch = load_batch(stage)
    # 전부 검증한 뒤에 실행한다 (한 줄이 틀려 일부만 처리되는 일이 없도록)
    stage_of = {"approve": {"drafts"}, "revise": {"drafts"}, "reject": {"drafts", "rendered"}, "publish_ok": {"rendered"}}
    hint = {"approve": "렌더링 영상은 `게시 OK` 로 답해주세요",
            "revise": "렌더링 영상 수정(재렌더링)은 아직 미구현입니다",
            "publish_ok": "`게시 OK` 는 렌더링 영상 확인(--stage rendered) 단계에서만 씁니다"}
    plan = []
    for cmd in commands:
        if stage not in stage_of[cmd.action]:
            raise ReplyError(f"{stage} 단계에서 쓸 수 없는 명령: {cmd.action} — {hint.get(cmd.action, '')}")
        plan += [(cmd, draft_id) for draft_id in _targets(cmd, batch)]

    out: list[str] = []
    for cmd, draft_id in plan:
        path = content_dir(stage) / draft_id
        if not path.exists():
            out.append(f"- {draft_id}: 이미 처리됨")
            continue
        grade = (load_review(path) or {}).get("grade")
        if cmd.action == "approve":
            if grade == "pending" and not confirm:  # 번호로 골라도 Claude Code 검수 전에는 보류
                out.append(f"- {draft_id}: ⏳ Claude Code 검수 대기 — 결과 반영 후 다시 승인하거나 `--confirm`")
                continue
            if not cmd.numbers and grade != "pass" and not confirm:
                out.append(f"- {draft_id}: {GRADE_ICON[grade]} 자동검수 {grade or '미실시'} — 번호로 따로 승인하거나 `전체 승인` 재확인 필요")
                continue
            move(draft_id, "drafts", "approved")
            record_agreement(draft_id, grade, "approve")
            out.append(f"- {draft_id}: 승인 → approved")
        elif cmd.action == "reject":
            move(draft_id, stage, "rejected")
            if stage == "drafts":
                record_agreement(draft_id, grade, "reject")
            out.append(f"- {draft_id}: 폐기 → rejected")
        elif cmd.action == "revise":
            # 재생성은 오래 걸리므로 워커가 처리한다 (재생성 → 자동 검수 → Claude 검수 → 검수 요청 재전송)
            record_agreement(draft_id, grade, "revise")
            importlib.import_module("pipeline.worker").enqueue("revise", draft_id=draft_id, note=cmd.note)
            out.append(f"- {draft_id}: 수정 요청 접수 — 재생성·검수 후 다시 보내드립니다")
        elif cmd.action == "publish_ok":
            move(draft_id, "rendered", "ready_to_publish")
            out.append(f"- {draft_id}: 게시 OK → ready_to_publish")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="텔레그램 검수 답장 처리")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--stage", choices=["drafts", "rendered"], default="drafts")
    ap = sub.add_parser("apply")
    ap.add_argument("reply")
    ap.add_argument("--stage", choices=["drafts", "rendered"], default="drafts")
    ap.add_argument("--confirm", action="store_true", help="`전체 승인` 시 ⚠️/⛔ 건도 승인")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(list_message(args.stage))
        return 0
    try:
        lines = apply(args.reply, args.stage, args.confirm)
    except ReplyError as e:
        print(f"❓ {e}")
        return 2
    print("\n".join(lines) or "처리할 항목 없음")
    return 0


if __name__ == "__main__":
    run_cli("03_review", main)
