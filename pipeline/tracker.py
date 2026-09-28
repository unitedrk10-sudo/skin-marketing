"""스폰서 병원 링크 유입 추적 — Cloudflare Worker(tracker/) 관리 API 클라이언트 + 리포트.

환경변수: TRACKER_URL (예: https://go.example.com), TRACKER_TOKEN (Worker 의 ADMIN_TOKEN)

    python -m pipeline.tracker add --sponsor glow [--target https://...] --label "Rejuran guide"
    python -m pipeline.tracker list
    python -m pipeline.tracker report [--days 7]            # 주간 요약 (텔레그램용 stdout, 미설정이면 조용히 종료)
    python -m pipeline.tracker sponsor-report --sponsor glow [--month 2026-10]   # 병원 제출용 월간 리포트 파일

주의 (기획서 12-1-1): 유입 수치는 영업·성과 보고 자료로만 쓴다. 요금은 정액 — 클릭·방문 수에 비례한 과금 금지 (의료법 §27③).
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from pipeline import sponsors
from pipeline.common import ROOT, get_logger, run_cli

log = get_logger("tracker")

CHANNELS = {"tt": "TikTok", "ig": "Instagram", "yt": "YouTube Shorts", "blog": "Blog"}
SOURCE_LABEL = {
    "tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "blog": "블로그", "ai": "AI 검색 답변",
    "search": "검색엔진", "social": "기타 SNS", "direct": "직접/앱(리퍼러 없음)", "other": "기타 사이트",
}


class TrackerError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("TRACKER_URL") and os.environ.get("TRACKER_TOKEN"))


def _request(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    if not configured():
        raise TrackerError("TRACKER_URL, TRACKER_TOKEN 환경변수가 없습니다 (tracker/README.md)")
    url = os.environ["TRACKER_URL"].rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "authorization": f"Bearer {os.environ['TRACKER_TOKEN']}", "content-type": "application/json",
        "user-agent": "skin-marketing-tracker-client/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise TrackerError(f"{method} {path} → {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise TrackerError(f"{method} {path} 연결 실패: {e}") from e


def add_link(sponsor_id: str | None, target: str | None, label: str) -> dict:
    sponsor = sponsors.get(sponsor_id) if sponsor_id else None
    target = target or (sponsor["official_url"] if sponsor else None)
    if not target:
        raise TrackerError("--target 또는 --sponsor 가 필요합니다")
    host = urllib.parse.urlparse(target).netloc.lower().removeprefix("www.")
    official = sponsors.official_host(sponsor) if sponsor else ""
    if sponsor and not (host == official or host.endswith("." + official)):
        raise TrackerError(f"스폰서 링크 대상은 {sponsor['name_en']} 공식 사이트여야 합니다 ({sponsor['official_url']})")
    return _request("POST", "/api/links", {"target_url": target, "sponsor_id": sponsor_id, "label": label})


def stats(start: date, end: date, sponsor_id: str | None = None) -> dict:
    return _request("GET", "/api/stats", query={"from": start.isoformat(), "to": end.isoformat(), "sponsor": sponsor_id})


def _pct(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "-"


def weekly_message(data: dict, previous: dict | None = None) -> str:
    t = data["totals"]
    if not t["clicks"] and not (previous or {}).get("totals", {}).get("clicks"):
        return ""  # 아직 클릭이 없으면 조용히
    lines = [f"[링크 유입 {data['from']} ~ {data['to']}] 클릭 {t['clicks']} · 하루 고유 방문 {t['unique_daily']}"
             + (f" (봇 {t['bots']} 제외)" if t["bots"] else "")]
    if previous:
        prev = previous["totals"]["clicks"]
        diff = t["clicks"] - prev
        lines[0] += f" · 지난주 대비 {'+' if diff >= 0 else ''}{diff}"
    lines.append("채널: " + ", ".join(f"{SOURCE_LABEL.get(r['source'], r['source'])} {r['clicks']}"
                                     for r in data["by_source"]) if data["by_source"] else "채널: -")
    for r in data["by_link"][:5]:
        lines.append(f"- {r.get('sponsor_id') or '일반'} / {r.get('label') or r['code']}: {r['clicks']}")
    ai = sum(r["clicks"] for r in data["by_source"] if r["source"] == "ai")
    if ai:
        lines.append(f"🤖 AI 검색 답변에서 넘어온 클릭 {ai}건")
    return "\n".join(lines)


def month_range(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _rows(items: list[dict], name, total: int) -> str:
    return "\n".join(f"| {name(r) or '-'} | {r['clicks']} | {_pct(r['clicks'], total)} |" for r in items) or "| - | 0 | - |"


def sponsor_report(sponsor: dict, month: str, data: dict) -> str:
    t = data["totals"]
    total = t["clicks"]
    return f"""# {sponsor['name_ko']} ({sponsor['name_en']}) — 링크 유입 리포트 {month}

기간: {data['from']} ~ {data['to']} (UTC) · 대상: {sponsor['official_url']}

## 요약
| 항목 | 값 |
|---|---|
| 병원 사이트로 이동한 클릭 | **{total}** |
| 하루 단위 고유 방문자 합계 | {t['unique_daily']} |
| 제외한 봇·미리보기 요청 | {t['bots']} |
| AI 검색 답변(ChatGPT·Perplexity·Gemini 등)에서 넘어온 클릭 | {sum(r['clicks'] for r in data['by_source'] if r['source'] == 'ai')} |

## 채널별
| 채널 | 클릭 | 비율 |
|---|---|---|
{_rows(data['by_source'], lambda r: SOURCE_LABEL.get(r['source'], r['source']), total)}

## 콘텐츠(링크)별
| 링크 | 클릭 | 비율 |
|---|---|---|
{_rows(data['by_link'], lambda r: r.get('label') or r['code'], total)}

## 국가별 (상위 20)
| 국가 | 클릭 | 비율 |
|---|---|---|
{_rows(data['by_country'], lambda r: r['country'], total)}

## 일별
| 날짜 | 클릭 | 비율 |
|---|---|---|
{_rows(data['by_day'], lambda r: r['day'], total)}

## 측정 방법
- 우리 채널(숏폼 프로필·설명란, 블로그)에 건 추적 링크를 누른 뒤 병원 사이트로 이동한 요청을 셉니다.
- 병원 사이트 방문 URL 에 `utm_source`·`utm_medium`(채널)·`utm_campaign`(콘텐츠) 이 붙어 있어, 병원의 애널리틱스(GA4 등)에서도 같은 유입을 직접 확인할 수 있습니다.
- 봇·링크 미리보기 요청은 제외했습니다. IP 는 저장하지 않으며 고유 방문자는 하루 단위로만 셉니다.
- 채널은 링크 종류(?s=)와 리퍼러로 분류합니다. 앱 내 브라우저는 리퍼러를 보내지 않는 경우가 있어 일부가 "직접/앱"으로 잡힐 수 있습니다.
- 본 수치는 광고 게재 성과 보고 자료이며, 광고비는 계약에 따른 정액입니다.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="링크 유입 추적")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--sponsor")
    a.add_argument("--target")
    a.add_argument("--label", required=True)
    sub.add_parser("list")
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=7)
    sr = sub.add_parser("sponsor-report")
    sr.add_argument("--sponsor", required=True)
    sr.add_argument("--month", help="YYYY-MM (기본: 지난달)")
    args = parser.parse_args(argv)

    if args.cmd == "report":
        if not configured():
            log.info("추적기 미설정 — 리포트 건너뜀")
            return 0
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
        current = stats(start, end)
        previous = stats(start - timedelta(days=args.days), start - timedelta(days=1))
        message = weekly_message(current, previous)
        if message:
            print(message)
        return 0
    if args.cmd == "add":
        link = add_link(args.sponsor, args.target, args.label)
        print(f"추적 링크 {link['code']} ({args.label})")
        if args.sponsor:
            # 스폰서 병원 링크는 블로그 스폰서 글 안에만 건다 (SNS 프로필 링크 = 브랜디드 콘텐츠로 볼 여지, 틱톡은 금지)
            print(f"- 블로그 스폰서 글 전용: {link['url']}?s=blog")
            print("  (SNS 프로필·설명란에는 걸지 마세요 — 프로필에는 우리 블로그 주소를 겁니다)")
        else:
            for key, name in CHANNELS.items():
                print(f"- {name}: {link['url']}?s={key}")
        return 0
    if args.cmd == "list":
        for link in _request("GET", "/api/links")["links"]:
            state = "" if link["active"] else " (꺼짐)"
            print(f"{link['code']}\t{link.get('sponsor_id') or '-'}\t{link.get('label') or ''}\t{link['target_url']}{state}")
        return 0
    sponsor = sponsors.get(args.sponsor)
    month = args.month or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    start, end = month_range(month)
    report = sponsor_report(sponsor, month, stats(start, end, sponsor["id"]))
    path = ROOT / "reports" / "sponsors" / f"{sponsor['id']}-{month}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    run_cli("tracker", main)
