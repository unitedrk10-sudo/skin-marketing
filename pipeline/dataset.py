"""영어 공개 데이터 세트 — 한국 지역별 피부과 진료기관 수 (2026-11-02, docs/ai-citation-plan.md 5단계).

심평원 병원정보서비스(공공누리 제1유형)에서 진료과목 피부과(dgsbjtCd=14)가 있는 기관을 전부 읽어 지역 단위로 합산한다.
공개 파일에는 병원명·주소·전화·사이트·좌표를 넣지 않는다 (합산 숫자만 — 고르거나 순위를 매기지 않는다).
  - total: 피부과 진료과목이 있는 기관 수. 전문의 병원 수가 아니다
  - named_dermatology: 기관 이름에 "피부과" 가 들어간 곳 수 (이름 기준이며 전문의 수를 직접 잰 값이 아니다)
  - near_attractions: 관광지 반경 안 기관 수 (content/clinics 캐시, 이름은 빼고 숫자만)
공개는 config/site.yaml data_page: true 일 때만 (심평원 이용 조건 확인 후). 지역 영문명은 config/regions_en.yaml.

    python -m pipeline.dataset refresh    # 전국 조회 → content/site/dataset.json
    python -m pipeline.dataset regions    # 새 지역의 영문명을 Claude 로 채운다
    python -m pipeline.dataset show
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import time
from collections import Counter
from datetime import date

import yaml

from pipeline import attractions, clinics, llm
from pipeline.common import CONFIG_DIR, content_dir, get_logger, load_json, now_iso, parse_json, run_cli, save_json

log = get_logger("dataset")

PAGE_ROWS = 200
PAGE_TRIES = 4  # 이 API 는 긴 조회에서 연결이 자주 끊긴다
HOSPITAL_TYPES = ("의원",)  # 그 외는 병원급으로 묶는다
EN_NAME = re.compile(r"^[A-Z][A-Za-z' -]+$")
CSV_COLUMNS = ["province", "district", "province_ko", "district_ko", "institutions_with_dermatology", "clinics", "hospitals", "named_dermatology"]


class DatasetError(RuntimeError):
    pass


def data_file():
    return content_dir() / "site" / "dataset.json"


def regions_file():
    return CONFIG_DIR / "regions_en.yaml"


def regions_en() -> dict[str, str]:
    return yaml.safe_load(regions_file().read_text(encoding="utf-8")) if regions_file().exists() else {}


def fetch_rows(get=None) -> list[dict]:
    """전국 피부과 진료기관 원자료 (메모리에서만 쓰고 저장하지 않는다)."""
    get = get or clinics._get
    out, page = [], 1
    while True:
        for attempt in range(PAGE_TRIES):
            try:
                body = get({"pageNo": page, "numOfRows": PAGE_ROWS, "dgsbjtCd": clinics.DERMATOLOGY})
                break
            except clinics.ClinicDataError:
                if attempt == PAGE_TRIES - 1:
                    raise
                time.sleep(5 * (attempt + 1))
        items = body.get("items") if isinstance(body.get("items"), dict) else {}
        rows = items.get("item") or []
        rows = [rows] if isinstance(rows, dict) else rows
        out += rows
        total = int(body.get("totalCount") or 0)
        if page * PAGE_ROWS >= total or not rows:
            if len(out) != total:
                raise DatasetError(f"읽은 수 {len(out)} ≠ 전체 {total}")
            return out
        page += 1


def aggregate(rows: list[dict]) -> list[dict]:
    """시·도 + 시·군·구 합산. 결과에 기관별 정보는 남지 않는다."""
    groups: dict[tuple[str, str], Counter] = {}
    for r in rows:
        key = ((r.get("sidoCdNm") or "").strip(), (r.get("sgguCdNm") or "").strip())
        c = groups.setdefault(key, Counter())
        c["total"] += 1
        c["clinics" if r.get("clCdNm") in HOSPITAL_TYPES else "hospitals"] += 1
        c["named_dermatology"] += "피부과" in (r.get("yadmNm") or "")
    return [{"sido": k[0], "sggu": k[1], **{f: c[f] for f in ("total", "clinics", "hospitals", "named_dermatology")}}
            for k, c in sorted(groups.items(), key=lambda kv: (-kv[1]["total"], kv[0]))]


def near_attractions() -> list[dict]:
    """관광지 주변 기관 수 (이름 없이 숫자만)."""
    out = []
    for aid, a in attractions.load().items():
        path = clinics.cache_dir() / f"{aid}.json"
        if path.exists():
            cached = load_json(path)
            out.append({"name": a.get("name", aid), "radius_m": cached.get("radius_m"), "institutions": len(cached.get("clinics", [])),
                        "as_of": str(cached.get("fetched_at", ""))[:10]})
    return sorted(out, key=lambda x: -x["institutions"])


def refresh(get=None) -> dict:
    rows = aggregate(fetch_rows(get))
    data = {"as_of": date.today().isoformat(), "generated_at": now_iso(), "regions": rows, "attractions": near_attractions(),
            "totals": {f: sum(r[f] for r in rows) for f in ("total", "clinics", "hospitals", "named_dermatology")}}
    save_json(data_file(), data)
    try:
        fill_regions()  # 행정구역이 바뀌어 새 지역이 생기면
    except (DatasetError, llm.LLMError) as e:
        log.warning("지역 영문명 채우기 실패: %s", e)
    return data


def load() -> dict | None:
    return load_json(data_file()) if data_file().exists() else None


def fill_regions() -> list[str]:
    """영문명이 없는 지역만 Claude 로 채운다 (표기 형식은 코드가 검사, 사람이 확인)."""
    data = load()
    if not data:
        raise DatasetError("먼저 refresh 를 실행하세요")
    known = regions_en()
    missing = sorted({x for r in data["regions"] for x in (r["sido"], r["sggu"]) if x and x not in known})
    if not missing:
        return []
    prompt = ("Give the official English name for each Korean administrative area below, in standard romanization, for example "
              '"종로구": "Jongno-gu", "서울": "Seoul", "수원영통구": "Suwon Yeongtong-gu", "제주시": "Jeju-si". '
              "Return ONLY a JSON object mapping each Korean name to its English name.\n" + "\n".join(missing))
    result = parse_json(llm.generate("dataset_regions", prompt).text)
    bad = [k for k in missing if not EN_NAME.match(str(result.get(k, "")))]
    if bad:
        raise DatasetError(f"영문명 확인 필요: {bad[:10]}")
    known.update({k: result[k] for k in missing})
    regions_file().write_text("# 지역 영문명 (pipeline/dataset.py)\n" + yaml.safe_dump(known, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return missing


def english_rows(data: dict) -> list[dict]:
    names = regions_en()
    missing = {x for r in data["regions"] for x in (r["sido"], r["sggu"]) if x and x not in names}
    if missing:
        raise DatasetError(f"지역 영문명이 없습니다: {sorted(missing)[:5]} — python -m pipeline.dataset regions")
    return [{"province": names[r["sido"]], "district": names.get(r["sggu"], ""), "province_ko": r["sido"], "district_ko": r["sggu"],
             "institutions_with_dermatology": r["total"], "clinics": r["clinics"], "hospitals": r["hospitals"],
             "named_dermatology": r["named_dermatology"]} for r in data["regions"]]


def to_csv(data: dict) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(english_rows(data))
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="영어 공개 데이터 세트")
    parser.add_argument("cmd", choices=["refresh", "regions", "show"])
    args = parser.parse_args(argv)
    if args.cmd == "refresh":
        data = refresh()
    elif args.cmd == "regions":
        print(f"새 지역 {len(fill_regions())}개")
        return 0
    else:
        data = load()
        if not data:
            print("아직 데이터가 없습니다")
            return 1
    print(f"기준 {data['as_of']} · 전체 {data['totals']} · 지역 {len(data['regions'])}개 · 관광지 {len(data['attractions'])}곳")
    for r in data["regions"][:10]:
        print(f"  {r['sido']} {r['sggu']}: {r['total']} (이름에 피부과 {r['named_dermatology']})")
    return 0


if __name__ == "__main__":
    run_cli("dataset", main)
