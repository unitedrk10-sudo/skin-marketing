"""내국인 의료 소비 트렌드 — 공개 카드 추정매출 수집 (2026-11-22).

외국인 의료 결제(pipeline/foreign_medical.py)와 별개로, 지역 의료 시장 자체의 추이를 본다.
지역마다 자료·카드사·주기가 달라 금액을 그대로 더하지 않고, 합칠 때는 기준 시점 대비 지수로 맞춘다.

  1) 서울: 서울시 상권분석서비스(추정매출-행정동, 열린데이터광장 OA-22175) — 가맹점 소재 행정동 × 서비스 업종 × 분기.
     의료 관련 업종: 일반의원·치과의원·한의원·의약품·의료기기·피부관리실. "일반의원"은 진료과목 구분이 없다
     (내과·소아과 등이 모두 섞인다). 연도별 zip 파일만 받는다(로그인·키 없음).
  2) 수도권 시도: 국가데이터처 KOSIS "시도별 서비스업생산지수(2020=100.0)"(orgId 101, tblId DT_1KC2023) 중 보건업 관련
     산업 — 시도 × 분기 지수. OpenAPI 키가 필요하다(환경변수 `KOSIS_API_KEY`, 키는 주소 매개변수로만 받는 API 라
     프록시 헤더 주입을 쓸 수 없다). 환경 네트워크에 kosis.kr 허용 필요.

    python -m pipeline.domestic_medical fetch-seoul [--years 2021-2025]
    python -m pipeline.domestic_medical fetch-kosis [--start 2019]   # KOSIS_API_KEY 필요
    python -m pipeline.domestic_medical export      # reports/domestic_medical/ 에 CSV
    python -m pipeline.domestic_medical show
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import zipfile

import requests

from pipeline.common import ROOT, content_dir, get_logger, load_json, now_iso, run_cli, save_json

log = get_logger("domestic_medical")

SEOUL_INF = "OA-22175"
SEOUL_PAGE = f"https://data.seoul.go.kr/dataList/{SEOUL_INF}/S/1/datasetView.do"
SEOUL_FILE = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false"
MED_CATS = ["일반의원", "치과의원", "한의원", "의약품", "의료기기", "피부관리실"]
CLINIC_CATS = ["일반의원", "치과의원", "한의원"]
AGE_COLS = ["10", "20", "30", "40", "50", "60_이상"]


class FetchError(RuntimeError):
    pass


def data_file():
    return content_dir() / "domestic_medical" / "seoul.json"


def export_dir():
    return ROOT / "reports" / "domestic_medical"


def _get(session: requests.Session, url: str, method: str = "GET", tries: int = 4, **kw) -> requests.Response:
    """열린데이터광장은 연결이 자주 끊기고 빈 응답을 주기도 해서 몇 번 다시 시도한다."""
    import time
    for attempt in range(tries):
        try:
            r = session.request(method, url, timeout=300, **kw)
            r.raise_for_status()
            if r.content:
                return r
            log.warning("빈 응답 %d (%s)", attempt + 1, url[:60])
        except requests.RequestException as e:
            log.warning("재시도 %d (%s): %s", attempt + 1, url[:60], e)
        time.sleep(3 * (attempt + 1))
    raise FetchError(f"받기 실패: {url}")


def seoul_files(session: requests.Session) -> dict[int, str]:
    """연도 → 파일 순번(seq). 화면의 다운로드 목록에서 읽는다."""
    page = _get(session, SEOUL_PAGE).text
    out = {}
    for seq, title in re.findall(r"downloadFile\('(\d+)'\);\" title=\"([^\"]+)\"", page):
        m = re.search(r"_(\d{4})년", title)
        if m:
            out[int(m.group(1))] = seq
    if not out:
        raise FetchError("서울 추정매출 파일 목록을 찾지 못함 — 화면 구조가 바뀌었을 수 있다")
    return out


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp949", errors="replace")


def parse_seoul(raw_zip: bytes) -> list[dict]:
    """zip 안 CSV 에서 의료 관련 업종 행만 남긴다."""
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            for r in csv.DictReader(io.StringIO(_decode(zf.read(name)))):
                cat = r.get("서비스_업종_코드_명")
                if cat not in MED_CATS:
                    continue
                num = lambda k: float(r.get(k) or 0)
                rows.append({
                    "quarter": r["기준_년분기_코드"], "dong": r["행정동_코드"], "dong_nm": r["행정동_코드_명"], "cat": cat,
                    "amount": num("당월_매출_금액"), "count": num("당월_매출_건수"),
                    "male": num("남성_매출_금액"), "female": num("여성_매출_금액"),
                    **{f"age_{a}": num(f"연령대_{a}_매출_금액") for a in AGE_COLS},
                })
    return rows


def fetch_seoul(years: list[int] | None = None) -> dict:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (data research)"
    files = seoul_files(s)
    years = years or sorted(files)
    rows = []
    for y in years:
        if y not in files:
            log.warning("서울 추정매출 %d년 파일 없음", y)
            continue
        r = _get(s, SEOUL_FILE, method="POST", data={"infId": SEOUL_INF, "seqNo": "", "seq": files[y], "infSeq": "3"})
        part = parse_seoul(r.content)
        log.info("서울 %d년: 의료 관련 %d행", y, len(part))
        rows += part
    if not rows:
        raise FetchError("서울 추정매출에서 의료 관련 행을 하나도 못 읽음")
    return {"fetched_at": now_iso(), "source": SEOUL_PAGE, "years": years, "rows": rows}


# ---- KOSIS 시도별 서비스업생산지수 ----

KOSIS_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
KOSIS_TABLE = {"orgId": "101", "tblId": "DT_1KC2023"}
KOSIS_SIDO = ("전국", "서울", "경기", "인천")
KOSIS_INDUSTRY = "보건"   # 산업 이름에 이 말이 들어간 항목만 (예: 보건업 및 사회복지 서비스업)


def kosis_file():
    return content_dir() / "domestic_medical" / "kosis_service_index.json"


def kosis_rows(raw: list[dict]) -> list[dict]:
    """KOSIS 응답에서 수도권·전국 × 보건 관련 산업만. 시도·산업 칸 위치(C1/C2)는 표마다 달라 이름으로 찾는다."""
    out = []
    for r in raw:
        names = [r.get(f"C{i}_NM", "") for i in range(1, 5)]
        sido = next((n for n in names if any(n.startswith(x) for x in KOSIS_SIDO)), None)
        industry = next((n for n in names if KOSIS_INDUSTRY in n), None)
        if not sido or not industry:
            continue
        try:
            value = float(r.get("DT"))
        except (TypeError, ValueError):
            continue
        out.append({"period": r.get("PRD_DE"), "sido": sido, "industry": industry, "item": r.get("ITM_NM"),
                    "value": value, "unit": r.get("UNIT_NM")})
    return out


def fetch_kosis(start: str = "2019") -> dict:
    import os
    key = os.environ.get("KOSIS_API_KEY")
    if not key:
        raise FetchError("KOSIS_API_KEY 가 없음 — KOSIS 공유서비스(OpenAPI)에서 무료로 발급받아 환경변수로 넣는다")
    params = {"method": "getList", "apiKey": key, "itmId": "ALL", "objL1": "ALL", "objL2": "ALL", "objL3": "",
              "format": "json", "jsonVD": "Y", "prdSe": "Q", "startPrdDe": f"{start}01", "endPrdDe": "209904",
              **KOSIS_TABLE}
    r = requests.get(KOSIS_URL, params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):  # 오류는 {"err": "...", "errMsg": "..."} 로 온다 (키 값은 로그에 남기지 않는다)
        raise FetchError(f"KOSIS 오류: {data.get('err')} {data.get('errMsg')}")
    rows = kosis_rows(data)
    if not rows:
        raise FetchError("KOSIS 응답에서 수도권 보건 관련 산업 행을 찾지 못함 — 표 구조를 확인")
    return {"fetched_at": now_iso(), "table": KOSIS_TABLE, "rows": rows}


def cmd_fetch_kosis(args) -> int:
    d = fetch_kosis(args.start)
    save_json(kosis_file(), d)
    log.info("저장: %s (%d행, %s)", kosis_file(), len(d["rows"]), sorted({r["industry"] for r in d["rows"]}))
    return 0


def gu_names() -> dict[str, str]:
    """행정동 코드 앞 5자리 → 구 이름 (foreign_medical 이 받아 둔 행정동 경계에서)."""
    p = content_dir() / "foreign_medical" / "hangjeongdong.geojson"
    if not p.exists():
        return {}
    return {f["properties"]["adm_cd2"][:5]: f["properties"]["sggnm"] for f in load_json(p)["features"]
            if f["properties"]["sido"] == "11"}


def quarterly_rows(d: dict, level: str = "gu") -> list[dict]:
    names = gu_names()
    agg: dict[tuple, dict] = {}
    for r in d["rows"]:
        key_id = r["dong"][:5] if level == "gu" else r["dong"]
        key = (r["quarter"], key_id, r["cat"])
        o = agg.setdefault(key, {"amount": 0.0, "count": 0.0, "female": 0.0, "male": 0.0,
                                 **{f"age_{a}": 0.0 for a in AGE_COLS},
                                 "name": names.get(key_id, key_id) if level == "gu" else r["dong_nm"]})
        for k in ["amount", "count", "female", "male", *[f"age_{a}" for a in AGE_COLS]]:
            o[k] += r[k]
    out = []
    for (q, key_id, cat), o in sorted(agg.items()):
        out.append({"quarter": q, "year": int(q[:4]), "q": int(q[4:]), "code": key_id, "name": o["name"], "cat": cat,
                    "amount_eok": round(o["amount"] / 1e8, 3), "count": round(o["count"]),
                    "female_share": round(o["female"] / (o["female"] + o["male"]), 4) if o["female"] + o["male"] else None,
                    **{f"age_{a}_eok": round(o[f"age_{a}"] / 1e8, 3) for a in AGE_COLS}})
    return out


def _write_csv(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def cmd_fetch_seoul(args) -> int:
    years = None
    if args.years:
        a, _, b = args.years.partition("-")
        years = list(range(int(a), int(b or a) + 1))
    d = fetch_seoul(years)
    save_json(data_file(), d)
    log.info("저장: %s (%d행)", data_file(), len(d["rows"]))
    return 0


def cmd_export(_args) -> int:
    d = load_json(data_file())
    _write_csv(export_dir() / "seoul_gu_quarterly.csv", quarterly_rows(d, "gu"))
    _write_csv(export_dir() / "seoul_dong_quarterly.csv", quarterly_rows(d, "dong"))
    if kosis_file().exists():
        _write_csv(export_dir() / "kosis_health_index.csv", load_json(kosis_file())["rows"])
    log.info("내보냄: %s", export_dir())
    return 0


def cmd_show(_args) -> int:
    d = load_json(data_file())
    by: dict[tuple[str, str], float] = {}
    for r in d["rows"]:
        by[(r["quarter"][:4], r["cat"])] = by.get((r["quarter"][:4], r["cat"]), 0) + r["amount"]
    for y in sorted({k[0] for k in by}):
        print(y, " | ".join(f"{c} {by.get((y, c), 0) / 1e8:,.0f}억" for c in MED_CATS))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="내국인 의료 소비 트렌드 (공개 카드 추정매출)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-seoul")
    f.add_argument("--years", default=None, help="예: 2021-2025 (기본: 올라온 연도 전부)")
    f.set_defaults(fn=cmd_fetch_seoul)
    k = sub.add_parser("fetch-kosis", help="KOSIS 시도별 서비스업생산지수 중 보건 관련 (KOSIS_API_KEY 필요)")
    k.add_argument("--start", default="2019")
    k.set_defaults(fn=cmd_fetch_kosis)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    sub.add_parser("show").set_defaults(fn=cmd_show)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    run_cli("domestic_medical", main)
