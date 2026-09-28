"""유입 데이터 분석 — 영업용 데이터셋·리포트 (기획서 12-3).

추적기(/api/export)의 일·링크·채널·국가별 집계를 글 메타데이터(content/site/tracked_links.json)·시술 분류
(config/procedures.yaml)와 합쳐 CSV 데이터셋과 요약 리포트를 만든다. 결과는 reports/analytics/ (git 제외).

    python -m pipeline.analytics report [--month 2026-10 | --days 90 | --from 2026-10-01 --to 2026-12-31]

측정 범위 (기획서 12-3 "시스템의 선"): 관심(방문) → 의도(등록기관 목록 클릭) → 도착(스폰서·파일럿 병원 사이트 클릭)까지.
병원 안의 상담·예약·결제(전환)는 병원 자체 측정 영역이며 여기서 추적하지 않는다. 수치는 영업·보고 자료일 뿐 요금은 정액.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from pipeline import tracker
from pipeline.common import ROOT, get_logger, load_yaml, run_cli

log = get_logger("analytics")

KIND_LABEL = {"registry": "의도(등록기관 목록)", "sponsor": "도착(스폰서 병원)", "pilot": "도착(파일럿 병원)", "other": "기타 링크"}
CSV_FIELDS = ["day", "week", "code", "kind", "contract", "sponsor_id", "draft_id", "slug", "title", "axis", "procedures", "attractions",
              "source", "country", "clicks", "unique_visitors"]


def reports_dir() -> Path:
    return ROOT / "reports" / "analytics"


def fetch(start: date, end: date) -> list[dict]:
    return tracker._request("GET", "/api/export", query={"from": start.isoformat(), "to": end.isoformat()})["rows"]


CATALOGS = ("procedures", "attractions")  # config/<이름>.yaml — 시술, 관광지·지역


def load_procedures() -> dict[str, dict]:
    return load_catalog("procedures")


def load_catalog(name: str) -> dict[str, dict]:
    out = {}
    for pid, p in (load_yaml(f"{name}.yaml").get(name) or {}).items():
        words = [str(k).strip().lower() for k in p.get("keywords", []) if str(k).strip()]
        pattern = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?:e?s)?(?![a-z0-9])")
        out[pid] = {"name": p.get("name", pid), "pattern": pattern}
    return out


def tag(meta: dict, procedures: dict[str, dict]) -> list[str]:
    text = " ".join([meta.get("title") or "", meta.get("slug") or "", " ".join(meta.get("keywords") or [])])
    text = re.sub(r"[-_/]", " ", text.lower())
    return [pid for pid, p in procedures.items() if p["pattern"].search(text)] or ["other"]


def _week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def enrich(rows: list[dict], links: dict[str, dict], procedures: dict[str, dict],
           attractions: dict[str, dict] | None = None) -> list[dict]:
    """추적기 집계 행 + 글 메타데이터 + 시술 태그. 사람 클릭만 (봇·미리보기 제외)."""
    from pipeline.site import REGISTRY_URL
    by_code = {v["code"]: v for v in links.values() if v.get("code")}
    out = []
    for r in rows:
        if r.get("is_bot"):
            continue
        meta = by_code.get(r["code"]) or {"title": r.get("label") or "", "sponsor_id": r.get("sponsor_id")}
        kind = meta.get("kind") or ("sponsor" if r.get("sponsor_id")
                                    else "registry" if (r.get("target_url") or "").startswith(REGISTRY_URL) else "other")
        if kind == "sponsor" and meta.get("contract") == "pilot":
            kind = "pilot"
        out.append({"day": r["day"], "week": _week(r["day"]), "code": r["code"], "kind": kind,
                    "contract": meta.get("contract") or "", "sponsor_id": r.get("sponsor_id") or meta.get("sponsor_id") or "",
                    "draft_id": meta.get("draft_id", ""), "slug": meta.get("slug", ""), "title": meta.get("title", ""),
                    "axis": meta.get("axis", ""), "procedures": "|".join(tag(meta, procedures)),
                    "attractions": "|".join(tag(meta, attractions if attractions is not None else load_catalog("attractions"))),
                    "source": r["source"], "country": r.get("country") or "XX",
                    "clicks": int(r["clicks"]), "unique_visitors": int(r.get("unique_visitors") or 0)})
    return out


def published_counts(links: dict[str, dict], end: date) -> Counter:
    """기간 말까지 게시된 글 수 (추적 링크가 있는 글) — 글당 평균의 분모. 클릭 0 인 글도 센다."""
    counts = Counter()
    for v in links.values():
        if v.get("kind") and (v.get("date") or "0000")[:10] <= end.isoformat():
            counts["pilot" if v["kind"] == "sponsor" and v.get("contract") == "pilot" else v["kind"]] += 1
    return counts


def analyze(rows: list[dict], links: dict[str, dict], procedures: dict[str, dict], end: date) -> dict:
    kinds = Counter()
    by_proc = defaultdict(Counter)
    by_place = defaultdict(Counter)
    by_post = defaultdict(lambda: {"clicks": 0, "unique": 0})
    by_source, by_country = defaultdict(Counter), Counter()
    by_week = defaultdict(Counter)
    for r in rows:
        kinds[r["kind"]] += r["clicks"]
        for pid in r["procedures"].split("|"):
            by_proc[pid][r["kind"]] += r["clicks"]
        for aid in (r.get("attractions") or "other").split("|"):
            if aid != "other":
                by_place[aid][r["kind"]] += r["clicks"]
        post = by_post[(r["kind"], r["title"] or r["code"], r["sponsor_id"])]
        post["clicks"] += r["clicks"]
        post["unique"] += r["unique_visitors"]
        by_source[r["source"]][r["kind"]] += r["clicks"]
        by_country[r["country"]] += r["clicks"]
        by_week[r["week"]][r["kind"]] += r["clicks"]
    posts = published_counts(links, end)
    total = sum(kinds.values())
    return {
        "total": total, "kinds": kinds, "posts": posts,
        "per_post": {k: (kinds[k] / posts[k] if posts[k] else None) for k in ("registry", "sponsor", "pilot")},
        "ai": _sum(by_source.get("ai", Counter())),
        "by_procedure": sorted(((procedures.get(p, {}).get("name", "Other" if p == "other" else p), c)
                                for p, c in by_proc.items()), key=lambda x: -_sum(x[1])),
        "by_attraction": sorted((((load_catalog("attractions").get(a) or {}).get("name", a), c)
                                 for a, c in by_place.items()), key=lambda x: -_sum(x[1])),
        "by_post": sorted(((k, v) for k, v in by_post.items()), key=lambda x: -x[1]["clicks"]),
        "by_source": sorted(by_source.items(), key=lambda x: -_sum(x[1])),
        "by_country": by_country.most_common(20),
        "by_week": sorted(by_week.items()),
    }


def _sum(c: Counter) -> int:
    return sum(c.values())


def _avg(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _pct(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "-"


def render(result: dict, start: date, end: date) -> str:
    k, total = result["kinds"], result["total"]
    arrivals = k["sponsor"] + k["pilot"]
    lines = [f"# 유입 데이터 분석 {start} ~ {end}", "",
             "측정 범위: 관심 → **의도**(등록기관 목록 클릭) → **도착**(스폰서·파일럿 병원 사이트 클릭). "
             "병원 안의 전환(상담·예약·결제)은 병원 자체 측정 영역. 봇·미리보기 제외.", "",
             "## 요약", "| 항목 | 값 |", "|---|---|",
             f"| 전체 클릭 (사람) | {total} |",
             f"| 의도: 등록기관 목록 클릭 | {k['registry']} |",
             f"| 도착: 스폰서 병원 사이트 | {k['sponsor']} |",
             f"| 도착: 파일럿 병원 사이트 | {k['pilot']} |",
             f"| AI 검색 답변(ChatGPT·Perplexity·Gemini 등)에서 넘어온 클릭 | {result['ai']} ({_pct(result['ai'], total)}) |", "",
             "## 영업 벤치마크 (글당 평균)", "| 글 종류 | 게시 글 수 | 기간 클릭 | 글당 평균 |", "|---|---|---|---|"]
    for kind in ("registry", "sponsor", "pilot"):
        lines.append(f"| {KIND_LABEL[kind]} | {result['posts'][kind]} | {k[kind]} | {_avg(result['per_post'][kind])} |")
    lines += ["", "- 중립 글 1개가 평균 몇 명을 '병원 찾기'로 보냈는지(의도)와, 스폰서·파일럿 글 1개가 평균 몇 명을 "
              "병원 사이트에 도착시켰는지(도착)를 비교한다. 병원 제안서의 '글 1개당 예상 도착 수' 근거.",
              f"- 도착 합계 {arrivals} (병원이 GA4 등에서 utm_source=skinbound 로 직접 확인 가능).", "",
              "## 시술별 관심도 (시술 = config/procedures.yaml 키워드 분류)",
              "| 시술 | 의도 | 도착(스폰서) | 도착(파일럿) | 합계 |", "|---|---|---|---|---|"]
    lines += [f"| {name} | {c['registry']} | {c['sponsor']} | {c['pilot']} | {_sum(c)} |" for name, c in result["by_procedure"]] or ["| - | 0 | 0 | 0 | 0 |"]
    lines += ["", "## 관광지·지역별 (여행 글이 병원 찾기·병원 도착으로 이어진 정도, config/attractions.yaml 분류)",
              "| 관광지·지역 | 의도 | 도착 | 합계 |", "|---|---|---|---|"]
    lines += [f"| {name} | {c['registry']} | {c['sponsor'] + c['pilot']} | {_sum(c)} |" for name, c in result["by_attraction"]] or ["| - | 0 | 0 | 0 |"]
    lines += ["", "## 글별 (상위 30)", "| 종류 | 글 | 병원 | 클릭 | 하루 고유 방문 합계 |", "|---|---|---|---|---|"]
    lines += [f"| {KIND_LABEL.get(kind, kind)} | {title} | {sponsor or '-'} | {v['clicks']} | {v['unique']} |"
              for (kind, title, sponsor), v in result["by_post"][:30]] or ["| - | - | - | 0 | 0 |"]
    lines += ["", "## 채널별", "| 채널 | 의도 | 도착 | 합계 | 비율 |", "|---|---|---|---|---|"]
    lines += [f"| {tracker.SOURCE_LABEL.get(s, s)} | {c['registry']} | {c['sponsor'] + c['pilot']} | {_sum(c)} | {_pct(_sum(c), total)} |"
              for s, c in result["by_source"]] or ["| - | 0 | 0 | 0 | - |"]
    lines += ["", "## 국가별 (상위 20)", "| 국가 | 클릭 | 비율 |", "|---|---|---|"]
    lines += [f"| {c} | {n} | {_pct(n, total)} |" for c, n in result["by_country"]] or ["| - | 0 | - |"]
    lines += ["", "## 주별 추이", "| 주 | 의도 | 도착(스폰서) | 도착(파일럿) |", "|---|---|---|---|"]
    lines += [f"| {w} | {c['registry']} | {c['sponsor']} | {c['pilot']} |" for w, c in result["by_week"]] or ["| - | 0 | 0 | 0 |"]
    lines += ["", "## 읽는 법·주의",
              "- 클릭 수는 링크를 누른 요청 수, 고유 방문은 하루 단위 합계(여러 날 방문한 사람은 여러 번 셈).",
              "- 앱 내 브라우저는 리퍼러를 안 보내는 경우가 있어 일부가 '직접/앱'으로 잡힌다.",
              "- 파일럿(무상) 병원 수치는 소수 병원의 초기 자료다. 영업 자료로 쓸 때 기간·병원 수·글 수를 함께 밝히고 "
              "특정 병원명을 다른 병원 제안서에 넣지 않는다 (병원 동의 없는 공개 금지).",
              "- 요금은 정액 — 이 수치로 클릭·환자 수 연동 과금을 하지 않는다 (의료법 §27③)."]
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def summary_line(result: dict, name: str) -> str:
    k = result["kinds"]
    top = ", ".join(f"{n} {_sum(c)}" for n, c in result["by_procedure"][:3]) or "-"
    return (f"📊 유입 분석 {name}: 의도(등록기관) {k['registry']} · 도착(스폰서 {k['sponsor']} / 파일럿 {k['pilot']})"
            f" · 시술 상위: {top}")


def run(start: date, end: date, name: str) -> tuple[Path, dict]:
    from pipeline.site import load_tracked_links
    links = load_tracked_links()
    procedures = load_procedures()
    rows = enrich(fetch(start, end), links, procedures, load_catalog("attractions"))
    result = analyze(rows, links, procedures, end)
    out = reports_dir()
    write_csv(rows, out / f"{name}.csv")
    (out / f"{name}.md").write_text(render(result, start, end), encoding="utf-8")
    return out / f"{name}.md", result


def period(args) -> tuple[date, date, str]:
    if args.start and args.end:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        return start, end, f"{start}_{end}"
    if args.days:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
        return start, end, f"{start}_{end}"
    month = args.month or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    start, end = tracker.month_range(month)
    return start, end, month


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="유입 데이터 분석 (영업용)")
    parser.add_argument("cmd", choices=["report"])
    parser.add_argument("--month", help="YYYY-MM (기본: 지난달)")
    parser.add_argument("--days", type=int)
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    args = parser.parse_args(argv)
    if not tracker.configured():
        log.info("추적기 미설정 — 분석 건너뜀")
        return 0
    start, end, name = period(args)
    path, result = run(start, end, name)
    if result["total"]:
        print(summary_line(result, name))
        print(f"- 리포트: {path} (+ 데이터셋 {path.with_suffix('.csv').name})")
    else:
        log.info("기간 내 클릭 없음 — %s", path)
    return 0


if __name__ == "__main__":
    run_cli("analytics", main)
