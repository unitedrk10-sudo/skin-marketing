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


STAGES = ["1단계 (1~4주): 전부 사람 검수", "2단계 (5~8주): ⚠️·⛔ 전부 + ✅ 20% 무작위",
          "3단계 (9주~): ⚠️·⛔ 전부 + ✅ 10% 무작위"]  # 기획서 6-0 신뢰 단계 — 완전 위임은 하지 않는다


def trust_check(today: date | None = None) -> list[str]:
    """4주마다 신뢰 단계 점검 (channels.yaml review.trust_start, 2026-10-15). 그 4주 동안의 자동 등급 ↔ 사람 판단.
    점검 주가 아니면 빈 목록. 단계 전환은 사람이 정한다 — 여기서는 숫자와 권고만."""
    from pipeline.common import load_yaml
    cfg = (load_yaml("channels.yaml").get("review") or {})
    if not cfg.get("trust_start"):
        return []
    today = today or date.today()
    start = date.fromisoformat(str(cfg["trust_start"]))
    every = int(cfg.get("check_weeks", 4)) * 7
    elapsed = (today - start).days + 1
    if elapsed < every or elapsed % every >= 7:  # 4주가 찬 그 주의 리포트에서만
        return []
    k = elapsed // every
    since, until = start + timedelta(days=every * (k - 1)), start + timedelta(days=every * k - 1)
    path = log_dir() / "review_agreement.csv"
    rows = [r for r in csv.DictReader(path.open(encoding="utf-8"))
            if since.isoformat() <= r.get("date", "") <= until.isoformat()] if path.exists() else []
    if not rows:
        return [f"🧭 신뢰 단계 점검 ({k * 4}주차): 검수 기록 없음"]
    grades = Counter(r["auto_grade"] for r in rows)
    agree = sum(int(r.get("agree") or 0) for r in rows)
    missed = [r["draft_id"] for r in rows if r["auto_grade"] == "pass" and r["human_decision"] != "approve"]
    caution_ok = sum(1 for r in rows if r["auto_grade"] == "caution" and r["human_decision"] == "approve")
    stage = STAGES[min(k, len(STAGES)) - 1]
    lines = [f"🧭 신뢰 단계 점검 ({k * 4}주차, {since:%m/%d}~{until:%m/%d}) — 지금: {stage}",
             f"   검수 {len(rows)}건 · 자동 등급 ✅{grades['pass']} ⚠️{grades['caution']} ⛔{grades['block']} · "
             f"자동-사람 일치 {agree}/{len(rows)} ({round(agree * 100 / len(rows))}%)",
             f"   ✅ 인데 사람이 고치거나 폐기: {len(missed)}건" + (f" ({', '.join(m[:10] for m in missed[:3])})" if missed else ""),
             f"   ⚠️ 인데 그대로 승인: {caution_ok}건 (많으면 ⚠️ 기준이 넓다는 뜻 — 기준 조정 검토)"]
    nxt = STAGES[min(k, len(STAGES) - 1)]
    if missed:
        lines.append(f"   → 권고: 단계 유지 (✅ 에서 놓친 것이 있음). 놓친 항목을 규칙에 추가")
    elif k < len(STAGES):
        lines.append(f"   → 권고: 다음 단계 가능 — {nxt}. 전환은 사람이 결정 (개발 세션에서 반영)")
    return lines


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
        from pipeline import social
        lines += social.reaction_lines(7)  # Threads·X 반응 수
    except Exception as e:  # noqa: BLE001
        log.warning("SNS 반응 집계 실패: %s", e)
    try:
        from pipeline import ai_traffic
        lines += ai_traffic.report_lines(7)  # AI 답변에서 온 방문·AI 봇이 읽어 간 글
    except Exception as e:  # noqa: BLE001
        log.warning("AI 유입 집계 실패: %s", e)
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
    checks = trust_check()
    if checks:  # 4주 점검 주에는 AI 인용 점검도 같이 (Gemini 검색, 질문 20개)
        try:
            from pipeline import citation_check
            checks += citation_check.report_lines(citation_check.check())
        except Exception as e:  # noqa: BLE001
            log.warning("AI 인용 점검 실패: %s", e)
    text = message(collect(args.days), extras() + checks)
    path = ROOT / "reports" / "weekly" / f"{iso_week()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    run_cli("07_report", main)
