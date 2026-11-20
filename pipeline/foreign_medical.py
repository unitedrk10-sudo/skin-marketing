"""외국인 의료 카드 소비 — 한국관광 데이터랩 수집 (2026-11-20).

데이터랩 화면이 쓰는 조회(`/visualize/getTempleteData.do`)를 그대로 불러 수도권(서울·경기·인천)만 모은다.
원천은 데이터랩의 외국인(해외 발급) 카드 데이터이고, 금액은 결제 금액이다(병원 매출 전체가 아님).

  1) 시도 × 진료과목 × 월 (의료관광 현황 화면, 단위 천원) — 진료과목 8개: 대학/종합병원·성형외과·안과·
     약국·치과·피부과·한약방·한의학과. "병의원" = 약국·한약방을 뺀 6개 합.
  2) 시군구 × 월 의료웰니스업 (지역별 관광소비 화면, 단위 천원) — 지도용. 경기도 구가 있는 시는 시 코드로
     조회하면 비어 있어 구 코드까지 모두 받는다(합치기는 보는 쪽에서 `city_code`). 업종 분류가 데이터랩의 관광 업종
     대분류라 병의원 외 웰니스(약국·피부관리 등)가 섞일 수 있다.
  3) 동 상위 5곳 × 시도 × 진료과목 × 월 (의료관광 현황 "인기 소비 동네", 단위 원) — 동 단위는 상위 5곳만 공개된다.
  4) 동 × 진료과목 점포 수 (export 때, 심평원 전국 병의원 및 약국 현황 분기 파일 — data.go.kr, 키 없음) — 좌표로 행정동을 정한다.

월 단위 조회는 한 번에 18개월까지라 나눠서 부른다. 결과는 content/foreign_medical/data.json (git 제외).

    python -m pipeline.foreign_medical fetch [--start 202401] [--end 202610]
    python -m pipeline.foreign_medical show
    python -m pipeline.foreign_medical export [--refresh-stores]   # reports/foreign_medical/ 에 CSV·지도 경계·동별 점포 수 (shapely 필요)
"""

from __future__ import annotations

import argparse
import time

import requests

from pipeline.common import ROOT, content_dir, get_logger, load_json, now_iso, run_cli, save_json

log = get_logger("foreign_medical")

BASE = "https://datalab.visitkorea.or.kr"
MED_PAGE = BASE + "/datalab/portal/theme/getMedicalTourSearch.do"
AREA_PAGE = BASE + "/datalab/portal/loc/getAreaDataForm.do"
QUERY = BASE + "/visualize/getTempleteData.do"
SGG_LIST = BASE + "/portal/getSggCdMappingList.do"
DATE_RANGE = BASE + "/portal/getSrchDteDivInitVal.do"
MED_MENU = "10352020000002024120600"

SIDOS = {"11": "서울특별시", "41": "경기도", "28": "인천광역시"}
DEPTS = ["대학/종합병원", "성형외과", "안과", "약국", "치과", "피부과", "한약방", "한의학과"]
CLINIC_DEPTS = [d for d in DEPTS if d not in ("약국", "한약방")]
MEDICAL_BIZ = "의료웰니스업"
MAX_MONTHS = 18
PAUSE = 0.3


class FetchError(RuntimeError):
    pass


def data_file():
    return content_dir() / "foreign_medical" / "data.json"


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:])
    out = []
    while f"{y}{m:02d}" <= end:
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def chunks(start: str, end: str) -> list[tuple[str, str]]:
    ms = months(start, end)
    return [(ms[i], ms[min(i + MAX_MONTHS, len(ms)) - 1]) for i in range(0, len(ms), MAX_MONTHS)]


def city_code(code: str) -> str:
    """경기도 구 코드 → 시 코드 (41281 고양시 덕양구 → 41280). 구가 없는 시·서울·인천은 그대로."""
    return code[:4] + "0" if code.startswith("41") else code


class Client:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (data research)"
        for page in (MED_PAGE, AREA_PAGE):  # 세션 쿠키·Referer 가 있어야 조회가 채워진다
            for attempt in range(4):
                try:
                    self.s.get(page, timeout=60).raise_for_status()
                    break
                except requests.RequestException as e:
                    if attempt == 3:
                        raise FetchError(f"데이터랩 접속 실패: {e}") from e
                    time.sleep(3 * (attempt + 1))

    def post(self, url: str, data: dict, referer: str) -> list[dict]:
        for attempt in range(4):
            try:
                r = self.s.post(url, data=data, headers={"Referer": referer}, timeout=90)
                r.raise_for_status()
                time.sleep(PAUSE)
                return r.json().get("list") or []
            except (requests.RequestException, ValueError) as e:
                log.warning("조회 재시도 %d (%s): %s", attempt + 1, data.get("qid"), e)
                time.sleep(2 * (attempt + 1))
        raise FetchError(f"조회 실패: {data.get('qid')} {data.get('SGG_CD')}")

    def latest_month(self) -> str:
        rows = self.post(DATE_RANGE, {"menuGrpCd": "1", "menuCd": MED_MENU}, MED_PAGE)
        ends = [r.get("endDteVal", "") for r in rows if r.get("cyclDiv") == "1"]
        if not ends:
            raise FetchError("데이터랩 최신 기준월을 읽지 못함")
        return max(ends)[:6]

    def sgg_codes(self, sido: str) -> list[tuple[str, str]]:
        rows = self.post(SGG_LIST, {"sidoCd": sido, "newSidoFlag": "Y", "combineYn": "YN",
                                    "gwangjuJeonnamExcept": "Y"}, AREA_PAGE)
        return [(r["sggCd"], r["sggNm"]) for r in rows]

    def medical(self, qid: str, sido: str, dept: str, ym1: str, ym2: str) -> list[dict]:
        return self.post(QUERY, {"qid": qid, "SGG_CD": sido, "NAT_CD": "", "BASE_YM1": ym1, "BASE_YM2": ym2,
                                 "srchAreaDate": "1", "tabDiv": "4", "KTO_CATE_SCLS_CD": dept,
                                 "TYPE_CODE": "AMT", "TYPE_CDODE": "AMT"}, MED_PAGE)

    def area_spend(self, sgg: str, ym1: str, ym2: str) -> list[dict]:
        return self.post(QUERY, {"qid": "LN_04_01_008_004", "SGG_CD": sgg, "BASE_YM1": ym1, "BASE_YM2": ym2,
                                 "srchAreaDate": "1"}, AREA_PAGE)


def fetch(start: str, end: str | None = None, client: Client | None = None) -> dict:
    c = client or Client()
    end = end or c.latest_month()
    parts = chunks(start, end)
    sido_dept: dict = {}
    top_dongs: list[dict] = []
    for sido in SIDOS:
        for dept in ["ALL", *DEPTS]:
            series = sido_dept.setdefault(sido, {}).setdefault(dept, {})
            for ym1, ym2 in parts:
                for r in c.medical("BY_TH_MEDIC_004_001_AMT", sido, dept, ym1, ym2):
                    series[r["BASE_YM"]] = r["SUM_CARD_COMPT_AMT"]
                for r in c.medical("BY_TH_MEDIC_004_002_AMT", sido, dept, ym1, ym2):
                    top_dongs.append({"ym": r["BASE_YM"], "sido": sido, "dept": dept, "sgg": r["SGG_CD"],
                                      "sgg_nm": r["SGG_NM"], "dong": r["ADONG_CD"], "dong_nm": r["ADONG_NM"],
                                      "amt_won": r["SUM_CARD_COMPT_AMT"]})
            log.info("%s %s: %d개월", SIDOS[sido], dept, len(series))
    sgg: dict = {}
    for sido in SIDOS:
        for code, name in c.sgg_codes(sido):
            med, tot = {}, {}
            for ym1, ym2 in parts:
                for r in c.area_spend(code, ym1, ym2):
                    if r.get("DPTR_REGN_NM") == MEDICAL_BIZ:
                        med[r["BASE_YM"]] = r["TOU_NUM"]
                    elif r.get("DPTR_REGN_NM") == "전체":
                        tot[r["BASE_YM"]] = r["TOU_NUM"]
            sgg[code] = {"name": name, "sido": sido, "medical": med, "total": tot}
            log.info("%s %s: 의료웰니스 %d개월", SIDOS[sido], name, len(med))
    if not any(sido_dept[s]["ALL"] for s in SIDOS):
        raise FetchError("시도 의료 소비가 비어 있음 — 데이터랩 조회 형식이 바뀌었을 수 있다")
    return {"fetched_at": now_iso(), "source": MED_PAGE, "start": start, "end": end,
            "units": {"sido_dept": "천원", "sgg": "천원", "top_dongs": "원"},
            "sido_dept": sido_dept, "sgg": sgg, "top_dongs": top_dongs}


def cmd_fetch(args) -> int:
    data = fetch(args.start, args.end)
    save_json(data_file(), data)
    log.info("저장: %s (%s ~ %s, 시군구 %d, 동 상위 %d행)", data_file(), data["start"], data["end"],
             len(data["sgg"]), len(data["top_dongs"]))
    return 0


def cmd_show(_args) -> int:
    if not data_file().exists():
        log.error("수집본 없음 — 먼저 fetch")
        return 1
    d = load_json(data_file())
    print(f"{d['start']} ~ {d['end']} (수집 {d['fetched_at']})")
    for sido, name in SIDOS.items():
        series = d["sido_dept"][sido]
        for year in sorted({ym[:4] for ym in series["ALL"]}):
            clinic = sum(v for dept in CLINIC_DEPTS for ym, v in series[dept].items() if ym[:4] == year)
            print(f"  {name} {year} 병의원 {clinic / 1e5:,.0f}억원")
    return 0


# ---- 대시보드용 내보내기 ----

BOUNDARY_URL = ("https://raw.githubusercontent.com/vuski/admdongkor/master/ver20261001/"
                "HangJeongDong_ver20261001.geojson")
# 인천 2026-07 행정체제 개편: 개편 전후를 같은 땅으로 비교하려고 묶는다 (중구·동구 → 제물포구·영종구, 서구 → 서해구·검단구)
REGION_GROUPS = {
    "28110": "IC-JD", "28140": "IC-JD", "28125": "IC-JD", "28155": "IC-JD",
    "28260": "IC-SG", "28275": "IC-SG", "28290": "IC-SG",
}
GROUP_NAMES = {"IC-JD": "중구·동구 (현 제물포·영종구)", "IC-SG": "서구 (현 서해·검단구)"}


def region_of(code: str) -> str:
    return REGION_GROUPS.get(code) or city_code(code)


def export_dir():
    return ROOT / "reports" / "foreign_medical"


def eok(thousand_won: float) -> float:
    return round(thousand_won / 1e5, 3)


def sido_dept_rows(d: dict) -> list[dict]:
    rows = []
    for sido, by_dept in d["sido_dept"].items():
        for dept in DEPTS:
            for ym, v in sorted(by_dept.get(dept, {}).items()):
                rows.append({"ym": ym, "year": int(ym[:4]), "month": int(ym[4:]), "sido": SIDOS[sido],
                             "dept": dept, "clinic": dept in CLINIC_DEPTS, "amount_eok": eok(v)})
    return rows


def region_rows(d: dict) -> tuple[list[dict], dict[str, str]]:
    """시군구 의료웰니스업 월 합계를 지도 지역 단위로. 구가 있는 경기 시는 그 달 구 값이 있으면 구 합계만 쓴다."""
    names: dict[str, str] = {}
    by: dict[tuple[str, str], dict] = {}
    for code, s in d["sgg"].items():
        region = region_of(code)
        is_parent = code.startswith("41") and code == region
        names.setdefault(region, GROUP_NAMES.get(region) or s["name"].split(" ")[0])
        for ym in set(s["medical"]) | set(s["total"]):
            cell = by.setdefault((region, ym), {"sido": SIDOS[s["sido"]], "parent": None, "parts": [0.0, 0.0, 0]})
            vals = [s["medical"].get(ym, 0.0), s["total"].get(ym, 0.0)]
            if is_parent:
                cell["parent"] = vals
            else:
                cell["parts"][0] += vals[0]
                cell["parts"][1] += vals[1]
                cell["parts"][2] += 1
    rows = []
    for (region, ym), cell in sorted(by.items()):
        med, tot = (cell["parts"][:2] if cell["parts"][2] else cell["parent"]) or (0.0, 0.0)
        rows.append({"ym": ym, "year": int(ym[:4]), "month": int(ym[4:]), "region": region,
                     "region_nm": names[region], "sido": cell["sido"], "medical_eok": eok(med),
                     "total_eok": eok(tot)})
    return rows, names


def dong_rows(d: dict) -> list[dict]:
    return [{"ym": r["ym"], "year": int(r["ym"][:4]), "month": int(r["ym"][4:]), "sido": SIDOS[r["sido"]],
             "dept": "전체(약국 포함)" if r["dept"] == "ALL" else r["dept"], "sgg_nm": r["sgg_nm"],
             "dong": r["dong"], "dong_nm": r["dong_nm"], "amount_eok": round(r["amt_won"] / 1e8, 3)}
            for r in d["top_dongs"]]


def boundaries(names: dict[str, str], dongs: set[str], tolerance: float = 0.0006) -> tuple[dict, dict]:
    """행정동 경계(vuski/admdongkor, 2026-10 기준)를 지도 지역으로 합치고, 상위 동 경계는 따로 뺀다."""
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    cache = content_dir() / "foreign_medical" / "hangjeongdong.geojson"
    if not cache.exists():
        r = requests.get(BOUNDARY_URL, timeout=300)
        r.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(r.content)
    feats = load_json(cache)["features"]
    parts: dict[str, list] = {}
    dong_feats = []
    for f in feats:
        p = f["properties"]
        if p["sido"] not in SIDOS:
            continue
        geom = shape(f["geometry"])
        region = region_of(p["sgg"])
        parts.setdefault(region, []).append(geom)
        if p["adm_cd2"][:8] in dongs:
            dong_feats.append({"type": "Feature", "properties": {"dong": p["adm_cd2"][:8], "name": p["adm_nm"]},
                               "geometry": _round(mapping(geom.simplify(tolerance / 3)))})
    region_feats = []
    for region, geoms in parts.items():
        merged = unary_union([g.buffer(0) for g in geoms]).simplify(tolerance)
        region_feats.append({"type": "Feature", "properties": {"region": region, "name": names.get(region, region),
                                                               "sido": SIDOS[region[:2]] if region[:2] in SIDOS else "인천광역시"},
                             "geometry": _round(mapping(merged))})
    return ({"type": "FeatureCollection", "features": region_feats},
            {"type": "FeatureCollection", "features": dong_feats})


def _round(geom: dict, nd: int = 4) -> dict:
    def r(c):
        return [round(c[0], nd), round(c[1], nd)] if isinstance(c[0], float) else [r(x) for x in c]
    return {"type": geom["type"], "coordinates": r(geom["coordinates"])}


def _write_csv(path, rows: list[dict]) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ---- 동별 점포 수 (심평원 전국 병의원 및 약국 현황, 분기 파일) ----
# 데이터랩 진료과목과 맞추려는 근사치다. 카드 가맹점 업종을 그대로 알 수 없어 이름·전문의·종별로 나눈다.
#   피부과·성형외과·안과: 의원·병원 중 그 진료과목을 신고했거나 이름에 그 과 이름이 있는 곳 (겹치면 양쪽에 센다).
#     심평원 진료과목은 전문의가 없어도 신고할 수 있어 간판 기준보다 넓다 (서초4동 피부과: 신고 152곳, 이름·전문의 13곳).
#   치과: 치과의원·치과병원 / 한의학과: 한의원·한방병원 / 대학/종합병원: 상급종합·종합병원 / 약국: 약국
#   한약방: 심평원 자료에 없음 / 전체(약국 포함): 위 종별 전부

HIRA_PAGE = "https://www.data.go.kr/data/15051059/fileData.do"
HIRA_TOKEN = "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do"
HIRA_FILE = "https://www.data.go.kr/cmm/cmm/fileDownload.do"
NAME_DEPTS = {"피부과": ("피부", "14"), "성형외과": ("성형", "08"), "안과": ("안과", "12")}
KIND_DEPTS = {"치과의원": "치과", "치과병원": "치과", "한의원": "한의학과", "한방병원": "한의학과",
              "상급종합": "대학/종합병원", "종합병원": "대학/종합병원", "약국": "약국"}
ALL_KINDS = {"의원", "병원", *KIND_DEPTS}
ALL_DEPT = "전체(약국 포함)"


def hira_zip(refresh: bool = False):
    """data.go.kr 파일 데이터 (키 없이 받는다). 첨부 번호가 분기마다 바뀌어 화면이 쓰는 두 단계를 그대로 따른다."""
    import re
    cache = content_dir() / "foreign_medical" / "hira.zip"
    if cache.exists() and not refresh:
        return cache
    s = requests.Session()
    page = s.get(HIRA_PAGE, timeout=60).text
    m = re.search(r"fn_fileDataDown\('15051059', '(uddi:[^']+)'", page)
    if not m:
        raise FetchError("심평원 파일 페이지에서 다운로드 정보를 찾지 못함")
    tok = s.post(HIRA_TOKEN, timeout=60, headers={"Referer": HIRA_PAGE}, data={
        "publicDataDetailPk": m.group(1), "publicDataPk": "15051059", "atchFileId": "", "fileDetailSn": "1",
        "publicDataTyCode": "PR0051"}).json()
    if not tok.get("status"):
        raise FetchError(f"심평원 파일 다운로드 거절: {tok.get('error')}")
    r = s.get(HIRA_FILE, params={"atchFileId": tok["atchFileId"], "fileDetailSn": tok["fileDetailSn"]}, timeout=600)
    r.raise_for_status()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(r.content)
    return cache


def _zip_csv(zf, prefix: str) -> list[dict]:
    import csv
    import io
    name = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1].startswith(prefix))
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("cp949", errors="replace"))))


def store_depts(inst: dict, subjects: set[str]) -> list[str]:
    kind, name = inst["종별코드명"], inst["요양기관명"]
    out = []
    if kind in ("의원", "병원"):
        out += [d for d, (word, code) in NAME_DEPTS.items() if word in name or code in subjects]
    if kind in KIND_DEPTS:
        out.append(KIND_DEPTS[kind])
    if kind in ALL_KINDS:
        out.append(ALL_DEPT)
    return out


def dong_store_rows(zip_path) -> tuple[list[dict], str]:
    """기관 좌표를 행정동 경계(2026-10)에 넣어 동 × 진료과목 점포 수를 센다."""
    import re
    import zipfile
    from shapely.geometry import Point, shape
    from shapely.strtree import STRtree

    with zipfile.ZipFile(zip_path) as zf:
        base = re.search(r"\((\d{4})\.(\d{1,2})\.\)", zf.namelist()[0])
        as_of = f"{base.group(1)}-{int(base.group(2)):02d}" if base else ""
        insts = _zip_csv(zf, "1.") + _zip_csv(zf, "2.")
        subjects: dict[str, set[str]] = {}
        for r in _zip_csv(zf, "5."):
            subjects.setdefault(r["암호화요양기호"], set()).add(r["진료과목코드"])
    feats = [f for f in load_json(content_dir() / "foreign_medical" / "hangjeongdong.geojson")["features"]
             if f["properties"]["sido"] in SIDOS]
    geoms = [shape(f["geometry"]) for f in feats]
    tree = STRtree(geoms)
    counts: dict[tuple[str, str], int] = {}
    unplaced = 0
    for inst in insts:
        if inst["시도코드명"] not in ("서울", "경기", "인천"):
            continue
        depts = store_depts(inst, subjects.get(inst["암호화요양기호"], set()))
        if not depts:
            continue
        try:
            pt = Point(float(inst["좌표(X)"]), float(inst["좌표(Y)"]))
        except ValueError:
            unplaced += 1
            continue
        hit = next((i for i in tree.query(pt) if geoms[i].covers(pt)), None)
        if hit is None:
            unplaced += 1
            continue
        for d in depts:
            key = (feats[hit]["properties"]["adm_cd2"][:8], d)
            counts[key] = counts.get(key, 0) + 1
    if unplaced:
        log.info("좌표가 없거나 경계 밖인 기관 %d곳은 동 점포 수에서 뺌", unplaced)
    rows = [{"dong": dong, "dept": dept, "stores": n, "as_of": as_of} for (dong, dept), n in sorted(counts.items())]
    return rows, as_of


def cmd_export(args) -> int:
    import json
    d = load_json(data_file())
    out = export_dir()
    regions, names = region_rows(d)
    dongs = dong_rows(d)
    _write_csv(out / "sido_dept.csv", sido_dept_rows(d))
    _write_csv(out / "regions_monthly.csv", regions)
    _write_csv(out / "top_dongs.csv", dongs)
    stores, as_of = dong_store_rows(hira_zip(refresh=args.refresh_stores))
    _write_csv(out / "dong_stores.csv", stores)
    log.info("동별 점포 수: %d행 (심평원 %s 기준)", len(stores), as_of)
    region_geo, dong_geo = boundaries(names, {r["dong"] for r in dongs})
    missing = {r["dong"] for r in dongs} - {f["properties"]["dong"] for f in dong_geo["features"]}
    if missing:
        log.warning("경계가 없는 상위 동 %d곳 (동 코드 변경): %s", len(missing), sorted(missing)[:10])
    (out / "map_regions.json").write_text(json.dumps(region_geo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "map_top_dongs.json").write_text(json.dumps(dong_geo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("내보냄: %s (지역 %d, 상위 동 경계 %d)", out, len(region_geo["features"]), len(dong_geo["features"]))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="외국인 의료 카드 소비 (한국관광 데이터랩) 수집")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--start", default="202401")
    f.add_argument("--end", default=None, help="기본: 데이터랩 최신 기준월")
    f.set_defaults(fn=cmd_fetch)
    sub.add_parser("show").set_defaults(fn=cmd_show)
    e = sub.add_parser("export", help="대시보드용 CSV·경계·동별 점포 수 → reports/foreign_medical/")
    e.add_argument("--refresh-stores", action="store_true", help="심평원 병의원 파일을 다시 받는다 (분기 갱신)")
    e.set_defaults(fn=cmd_export)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    run_cli("foreign_medical", main)
