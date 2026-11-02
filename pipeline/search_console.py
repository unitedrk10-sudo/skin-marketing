"""구글·Bing 검색 현황 — 글별 노출·클릭·순위, 상위 검색어, 색인 여부 (2026-11-02, docs/ai-citation-plan.md 2단계).

설정이 없으면 주간 리포트에 "연결 안 됨" 한 줄만 나온다. 키·파일 내용은 로그에 남기지 않는다.
  - 구글: GSC_SERVICE_ACCOUNT_FILE (서비스 계정 JSON 경로, 저장소 밖). 이 계정 이메일을 Search Console 에 사용자로 추가(읽기)
  - Bing: BING_WEBMASTER_KEY (Bing Webmaster Tools → API Access)

    python -m pipeline.search_console [--days 7]
    python -m pipeline.search_console --bing-submit    # 게시된 글 주소를 Bing 에 직접 제출 (하루 100개)
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta

from pipeline.common import get_logger, load_yaml, run_cli

log = get_logger("search_console")

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_QUERY = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
GSC_INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
BING_API = "https://ssl.bing.com/webmaster/api.svc/json/{method}"
GSC_LAG_DAYS = 3  # 구글 숫자는 2~3일 늦게 들어온다
TOP = 5
INSPECT_LIMIT = 30
BING_SUBMIT_MAX = 100  # 하루 한도
INDEX_ACTION = date(2026, 11, 2)  # 구글 색인 요청·Bing 제출 (docs/ai-citation-plan.md 색인 기준선)
EFFECT_WEEKS = 3
EFFECT_MIN_IMPRESSIONS = 30
INDEX_REQUESTED = {"how-many-days-korea-skin-treatment-trip", "flying-after-skin-treatment-korea-timing-guide",
                   "juvelook-pdlla-collagen-booster-explained", "thermage-korea-monopolar-rf-vs-hifu",
                   "rejuran-korea-downtime-sessions-aftercare", "how-to-read-korean-cosmetic-labels-aftercare",
                   "jeju-busan-after-skin-treatment-sun-sea-sauna"}
INDEX_CONTROL = {"gangnam-sinsa-apgujeong-area-guide", "bukchon-hanok-village-walk-after-skin-booster-laser-downtime",
                 "gyeongbokgung-hanbok-day-skin-treatment-order", "laser-treatment-korea-sun-aftercare-autumn-seoul"}


class SearchError(RuntimeError):
    pass


def _domain() -> str:
    return load_yaml("site.yaml").get("domain") or ""


def gsc_configured() -> bool:
    return bool(os.environ.get("GSC_SERVICE_ACCOUNT_FILE"))


def bing_configured() -> bool:
    return bool(os.environ.get("BING_WEBMASTER_KEY"))


def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(os.environ["GSC_SERVICE_ACCOUNT_FILE"], scopes=[GSC_SCOPE])
    return AuthorizedSession(creds)


def _post(session, url: str, body: dict) -> dict:
    resp = session.post(url, json=body, timeout=60)
    if resp.status_code >= 400:
        raise SearchError(f"Search Console 오류 {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def gsc_stats(days: int = 7, today: date | None = None, session=None) -> dict:
    """{"totals", "previous", "pages": [...], "queries": [...]}"""
    session = session or _session()
    url = GSC_QUERY.format(site=urllib.parse.quote(f"sc-domain:{_domain()}", safe=""))
    end = (today or date.today()) - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    prev_end, prev_start = start - timedelta(days=1), start - timedelta(days=days)

    def rows(a: date, b: date, dimensions: list[str]) -> list[dict]:
        body = {"startDate": a.isoformat(), "endDate": b.isoformat(), "dimensions": dimensions, "rowLimit": 200}
        return _post(session, url, body).get("rows", [])

    def total(items: list[dict]) -> dict:
        return {"clicks": sum(r["clicks"] for r in items), "impressions": sum(r["impressions"] for r in items)}
    pages = rows(start, end, ["page"])
    return {"from": start, "to": end, "totals": total(pages), "previous": total(rows(prev_start, prev_end, ["page"])),
            "pages": sorted(pages, key=lambda r: -r["impressions"]), "queries": sorted(rows(start, end, ["query"]), key=lambda r: -r["impressions"])}


def index_status(urls: list[str], session=None) -> Counter:
    """글 주소별 구글 색인 상태 (coverageState 별 개수)."""
    session = session or _session()
    out: Counter = Counter()
    for url in urls[:INSPECT_LIMIT]:
        data = _post(session, GSC_INSPECT, {"inspectionUrl": url, "siteUrl": f"sc-domain:{_domain()}"})
        out[data.get("inspectionResult", {}).get("indexStatusResult", {}).get("coverageState", "unknown")] += 1
    return out


def bing_submit(urls: list[str], key: str | None = None) -> int:
    """글 주소를 Bing URL 제출 API 로 보낸다. 색인을 보장하지는 않는다."""
    key = key or os.environ["BING_WEBMASTER_KEY"]
    urls = urls[:BING_SUBMIT_MAX]
    req = urllib.request.Request(f"{BING_API.format(method='SubmitUrlBatch')}?{urllib.parse.urlencode({'apikey': key})}",
                                 data=json.dumps({"siteUrl": f"https://{_domain()}/", "urlList": urls}).encode(),
                                 headers={"content-type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60):
            return len(urls)
    except OSError as e:  # 주소에 키가 들어 있어 원문 오류는 남기지 않는다
        raise SearchError(f"Bing 제출 실패 ({type(e).__name__} {getattr(e, 'code', '')})") from None


def bing_index(key: str | None = None) -> dict | None:
    """Bing 의 최근 색인 페이지 수·외부 링크 수 (GetCrawlStats 마지막 줄)."""
    key = key or os.environ["BING_WEBMASTER_KEY"]
    query = urllib.parse.urlencode({"siteUrl": f"https://{_domain()}/", "apikey": key})
    try:
        with urllib.request.urlopen(f"{BING_API.format(method='GetCrawlStats')}?{query}", timeout=60) as resp:
            rows = json.loads(resp.read().decode()).get("d") or []
    except OSError as e:
        raise SearchError(f"Bing GetCrawlStats 호출 실패 ({type(e).__name__})") from None
    return {"in_index": rows[-1].get("InIndex", 0), "in_links": rows[-1].get("InLinks", 0)} if rows else None


def bing_stats(key: str | None = None) -> dict:
    """Bing 글별 누적 통계 (최근 기간은 Bing 이 정한다)."""
    key = key or os.environ["BING_WEBMASTER_KEY"]
    site = f"https://{_domain()}/"

    def call(method: str) -> list[dict]:
        query = urllib.parse.urlencode({"siteUrl": site, "apikey": key})
        try:
            with urllib.request.urlopen(f"{BING_API.format(method=method)}?{query}", timeout=60) as resp:
                return json.loads(resp.read().decode()).get("d") or []
        except OSError as e:  # 주소에 키가 들어 있어 원문 오류는 남기지 않는다
            raise SearchError(f"Bing {method} 호출 실패 ({type(e).__name__})") from None
    pages, queries = call("GetPageStats"), call("GetQueryStats")
    return {"clicks": sum(r.get("Clicks", 0) for r in pages), "impressions": sum(r.get("Impressions", 0) for r in pages),
            "pages": sorted(pages, key=lambda r: -r.get("Impressions", 0)), "queries": sorted(queries, key=lambda r: -r.get("Impressions", 0))}


def slope(values: list[float]) -> float:
    """주당 증가량 (최소제곱 기울기)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x, mean_y = (n - 1) / 2, sum(values) / n
    return sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / sum((i - mean_x) ** 2 for i in range(n))


def index_effect(today: date | None = None, session=None) -> dict:
    """색인 조치(2026-11-02) 효과: 조치 전 3주와 후 3주의 구글 주간 노출 기울기. 요청한 7편과 이미 색인돼 있던 4편(비교군)을 나눈다."""
    session = session or _session()
    url = GSC_QUERY.format(site=urllib.parse.quote(f"sc-domain:{_domain()}", safe=""))
    last = (today or date.today()) - timedelta(days=GSC_LAG_DAYS)
    start = INDEX_ACTION - timedelta(days=7 * EFFECT_WEEKS)
    rows = _post(session, url, {"startDate": start.isoformat(), "endDate": last.isoformat(), "dimensions": ["date", "page"], "rowLimit": 25000}).get("rows", [])
    done = min(EFFECT_WEEKS, max(0, ((last - INDEX_ACTION).days + 1) // 7))
    weeks = {name: [0] * (EFFECT_WEEKS + done) for name in ("requested", "control")}
    for r in rows:
        week = (date.fromisoformat(r["keys"][0]) - start).days // 7
        slug = _path(r["keys"][1]).strip("/")
        group = "requested" if slug in INDEX_REQUESTED else "control" if slug in INDEX_CONTROL else ""
        if group and 0 <= week < len(weeks[group]):
            weeks[group][week] += r["impressions"]
    return {"done": done, "weeks": weeks,
            **{group: {"before": slope(v[:EFFECT_WEEKS]), "after": slope(v[EFFECT_WEEKS:])} for group, v in weeks.items()}}


def effect_verdict(data: dict) -> str:
    """전후 기울기로 판정. 숫자가 작으면 한 번의 튐으로 결과가 뒤집히므로 노출 하한을 둔다."""
    if data["done"] < EFFECT_WEEKS:
        return f"조치 후 {data['done']}/{EFFECT_WEEKS}주 — 판정 전"
    req, ctl = data["requested"], data["control"]
    if sum(data["weeks"]["requested"][EFFECT_WEEKS:]) < EFFECT_MIN_IMPRESSIONS:
        return "노출이 적어 판정 불가"
    if req["after"] > req["before"] and req["after"] - req["before"] > ctl["after"] - ctl["before"]:
        return "효과 가능성 있음 (요청 글의 기울기가 비교군보다 더 늘었음)"
    if req["after"] > req["before"]:
        return "기울기는 늘었지만 비교군도 함께 늘어 요청 효과로 보기 어려움"
    return "기울기 변화 없음"


def effect_lines(today: date | None = None, session=None) -> list[str]:
    data = index_effect(today, session)
    req, ctl = data["requested"], data["control"]
    return [f"📐 색인 효과 (구글 주간 노출 기울기, 11/2 전후): {effect_verdict(data)}",
            f"   요청 7편 {data['weeks']['requested']} · 전 {req['before']:+.1f}/주 → 후 {req['after']:+.1f}/주",
            f"   비교 4편 {data['weeks']['control']} · 전 {ctl['before']:+.1f}/주 → 후 {ctl['after']:+.1f}/주"]


def _path(url: str) -> str:
    return urllib.parse.urlparse(url).path or url


def report_lines(days: int = 7, today: date | None = None) -> list[str]:
    lines = []
    if gsc_configured():
        try:
            g = gsc_stats(days, today)
            t, p = g["totals"], g["previous"]
            lines.append(f"🔍 구글 검색 ({g['from']:%m/%d}~{g['to']:%m/%d}): 노출 {p['impressions']} → {t['impressions']} · 클릭 {p['clicks']} → {t['clicks']}")
            for r in g["pages"][:TOP]:
                lines.append(f"   노출 {r['impressions']} · 클릭 {r['clicks']} · 평균 {r['position']:.1f}위  {_path(r['keys'][0])}")
            if g["queries"]:
                lines.append("   검색어: " + ", ".join(f"{r['keys'][0]} ({r['impressions']})" for r in g["queries"][:TOP]))
            from pipeline import site
            base = f"https://{_domain()}"
            status = index_status([f"{base}/{post['slug']}/" for post in site.collect_posts()])
            lines.append("   색인: " + ", ".join(f"{k} {v}" for k, v in status.most_common()))
            lines += effect_lines(today)
        except Exception as e:  # noqa: BLE001 — 검색 현황 실패가 리포트를 막지 않게
            log.warning("구글 검색 현황 실패: %s", e)
            lines.append("🔍 구글 검색: 확인 못 함 (로그 확인)")
    else:
        lines.append("🔍 구글 검색: 연결 안 됨")
    if bing_configured():
        try:
            b = bing_stats()
            index = bing_index()
            lines.append(f"🔍 Bing 검색 (최근 집계): 노출 {b['impressions']} · 클릭 {b['clicks']}"
                         + (f" · 색인 {index['in_index']}페이지 · 외부 링크 {index['in_links']}" if index else ""))
            if b["queries"]:
                lines.append("   검색어: " + ", ".join(f"{r.get('Query')} ({r.get('Impressions', 0)})" for r in b["queries"][:TOP]))
        except Exception as e:  # noqa: BLE001
            log.warning("Bing 검색 현황 실패: %s", e)
            lines.append("🔍 Bing 검색: 확인 못 함 (로그 확인)")
    else:
        lines.append("🔍 Bing 검색: 연결 안 됨")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="구글·Bing 검색 현황")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--bing-submit", action="store_true")
    args = parser.parse_args(argv)
    if args.bing_submit:
        from pipeline import social
        base = f"https://{_domain()}"
        urls = [f"{base}/{slug}/" for slug in social.live_slugs().values()]
        print(f"Bing 제출 {bing_submit(urls)}개")
        return 0
    print("\n".join(report_lines(args.days)))
    return 0


if __name__ == "__main__":
    run_cli("search_console", main)
