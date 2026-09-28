"""7. 주간 리포트 — 지난 7일 파이프라인 현황을 텔레그램 한 메시지로 (Hermes 크론, 일요일 저녁).

  - 만든 것: 초안 수, 자동 검수 등급(✅⚠️⛔⏳), 수정 요청, 승인·폐기, 렌더링, 게시(채널별)
  - 기다리는 것: 검수 대기 초안, 게시 전 확인 영상, 게시 키트 대기
  - 자동 검수와 사람 판단 일치율 (logs/review_agreement.csv — 신뢰 단계 판단, 기획서 6-0)
  - 블로그 글 수, 링크 유입(추적기 설정 시), 이번 주 수요 상위 시술·관광지
결과는 reports/weekly/<주차>.md 에도 저장 (git 제외).

    python -m pipeline.07_report [--days 7]
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from pipeline.common import ROOT, STATES, content_dir, draft_dirs, get_logger, iso_week, load_draft, load_json, log_dir, run_cli

log = get_logger("07_report")

GRADE_ICON = {"pass": "✅", "caution": "⚠️", "block": "⛔", "pending": "⏳"}


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _at(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _recent(value: str | None, since: datetime) -> bool:
    at = _at(value)
    return bool(at and at >= since)


def collect(days: int = 7) -> dict:
    since = _since(days)
    created, grades, revisions, moves, posts = 0, Counter(), 0, Counter(), Counter()
    for state in STATES[1:]:
        for path in draft_dirs(state):
            draft = load_draft(path)
            if _recent(draft.get("created_at"), since):
                created += 1
                review = path / "review.json"
                if review.exists():
                    grades[load_json(review).get("grade", "pending")] += 1
            revisions += sum(1 for r in draft.get("revisions", []) if not r.get("auto") and _recent(r.get("at"), since))
            history = path / "history.json"
            for ev in load_json(history) if history.exists() else []:
                if _recent(ev.get("at"), since):
                    moves[ev["to"]] += 1
            log_file = path / "publish_log.json"
            for ch, rec in (load_json(log_file) if log_file.exists() else {}).items():
                if _recent(rec.get("at"), since):
                    posts[ch] += 1
    waiting = {"drafts": len(draft_dirs("drafts")), "rendered": len(draft_dirs("rendered")),
               "ready_to_publish": len(draft_dirs("ready_to_publish"))}
    return {"days": days, "created": created, "grades": grades, "revisions": revisions, "moves": moves,
            "posts": posts, "waiting": waiting, "agreement": agreement(since.date())}


def agreement(since: date) -> tuple[int, int]:
    path = log_dir() / "review_agreement.csv"
    if not path.exists():
        return 0, 0
    rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r.get("date", "") >= since.isoformat()]
    return sum(int(r.get("agree") or 0) for r in rows), len(rows)


def extras() -> list[str]:
    """블로그·유입·수요 — 각각 실패해도 리포트는 나간다."""
    lines = []
    try:
        from pipeline import site
        posts = site.collect_posts()
        lines.append(f"🌐 블로그 글 {len(posts)}개 (스폰서 {sum(1 for p in posts if p['draft'].get('sponsor'))})")
    except Exception as e:  # noqa: BLE001
        log.warning("블로그 집계 실패: %s", e)
    try:
        from pipeline import tracker
        if tracker.configured():
            end = date.today() - timedelta(days=1)
            t = tracker.stats(end - timedelta(days=6), end)["totals"]
            lines.append(f"🔗 링크 클릭 {t['clicks']} (하루 고유 {t['unique_daily']})")
    except Exception as e:  # noqa: BLE001
        log.warning("유입 집계 실패: %s", e)
    try:
        from pipeline import demand
        tables = demand.all_scores()
        for name, label in (("procedures", "시술"), ("attractions", "관광지")):
            top = sorted((s for pid, s in tables[name].items() if pid != "other"), key=lambda s: -s["score"])[:3]
            lines.append(f"📈 수요 상위 {label}: " + ", ".join(s["name"] for s in top))
    except Exception as e:  # noqa: BLE001
        log.warning("수요 집계 실패: %s", e)
    return lines


def message(data: dict, extra: list[str]) -> str:
    g, m, w = data["grades"], data["moves"], data["waiting"]
    grades = " ".join(f"{GRADE_ICON[k]}{g[k]}" for k in GRADE_ICON if g[k]) or "-"
    posts = ", ".join(f"{ch} {n}" for ch, n in sorted(data["posts"].items())) or "0"
    agree, total = data["agreement"]
    lines = [f"[주간 리포트 {iso_week()} · 지난 {data['days']}일]",
             f"✍️ 초안 {data['created']}건 (자동 검수 {grades}) · 수정 요청 {data['revisions']}",
             f"👍 승인 {m['approved']} · 폐기 {m['rejected']} · 🎬 렌더링 {m['rendered']} · 게시 OK {m['ready_to_publish']}",
             f"📤 게시 {posts} · 모든 채널 완료 {m['published']}",
             f"⏳ 대기: 검수 {w['drafts']} · 영상 확인 {w['rendered']} · 게시 키트 {w['ready_to_publish']}"]
    if total:
        lines.append(f"🤝 자동 검수-사람 판단 일치 {agree}/{total} ({round(agree * 100 / total)}%)")
    return "\n".join(lines + extra)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주간 리포트")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    text = message(collect(args.days), extras())
    path = ROOT / "reports" / "weekly" / f"{iso_week()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    run_cli("07_report", main)
