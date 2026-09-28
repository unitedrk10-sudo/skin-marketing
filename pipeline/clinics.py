"""코스 주변 피부과 목록 — 심평원(건강보험심사평가원) 병원정보서비스 공공데이터 기준.

여행 글(procedure_travel, travel_guide)의 코스에 나오는 관광지 반경 안의 피부과 진료 의료기관을 **빠짐없이, 거리순으로**
보여준다. 우리가 고르거나 순위를 매기지 않는다 — 선택·추천이 들어가면 광고(의료법 §56)·알선 시비가 생기고 중립성이 깨진다.
  - 포함 기준: 심평원 공공데이터에서 진료과목 피부과(dgsbjtCd=14)가 있는 의료기관, 관광지 중심 반경 radius_m 이내 (전부)
  - 정렬: 거리순. 표시 개수를 줄일 때도 가까운 순으로 자르고 "N곳 중 가까운 M곳"이라고 밝힌다
  - 표시: 이름·종별·주소(구·동)·거리·지도 검색 링크. 병원 사이트 링크·가격·후기·평점 없음
  - 스폰서(광고주·파일럿) 병원은 같은 자리(거리순)에 "Advertiser" 표시만 붙는다 — 순서·노출을 돈으로 바꾸지 않는다
  - 목록은 코드가 만든다 (LLM 본문에는 병원명 금지 규칙 그대로)

환경변수: DATA_GO_KR_KEY (공공데이터포털 일반 인증키 — 건강보험심사평가원_병원정보서비스 활용신청)

    python -m pipeline.clinics refresh [--force]   # 좌표 있는 관광지마다 주변 피부과 목록 갱신 → content/clinics/<id>.json
    python -m pipeline.clinics show coex           # 캐시된 목록 확인
"""

from __future__ import annotations

import argparse
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from pipeline import attractions, sponsors
from pipeline.common import content_dir, get_logger, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("clinics")

HIRA_URL = "https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList"
DERMATOLOGY = "14"  # 심평원 진료과목 코드: 피부과
DEFAULTS = {"radius_m": 700, "max_per_stop": 40, "refresh_days": 30}


class ClinicDataError(RuntimeError):
    pass


def settings() -> dict:
    return {**DEFAULTS, **(load_yaml("site.yaml").get("clinic_directory") or {})}


def cache_dir():
    return content_dir() / "clinics"


def configured() -> bool:
    return bool(os.environ.get("DATA_GO_KR_KEY"))


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


def _get(params: dict) -> dict:
    query = urllib.parse.urlencode({"serviceKey": os.environ["DATA_GO_KR_KEY"], "_type": "json", **params})
    try:
        with urllib.request.urlopen(f"{HIRA_URL}?{query}", timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ClinicDataError(f"심평원 병원정보서비스 호출 실패: {e}") from e
    header = body.get("response", {}).get("header", {})
    if header.get("resultCode") not in ("00", "0", None):
        raise ClinicDataError(f"심평원 API 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
    return body.get("response", {}).get("body", {})


def fetch_near(lat: float, lng: float, radius_m: int) -> list[dict]:
    """반경 안 피부과 진료 의료기관 전부 (페이지를 끝까지 읽는다)."""
    out, page = [], 1
    while True:
        body = _get({"pageNo": page, "numOfRows": 500, "dgsbjtCd": DERMATOLOGY,
                     "xPos": f"{lng:.6f}", "yPos": f"{lat:.6f}", "radius": radius_m})
        items = (body.get("items") or {}) if isinstance(body.get("items"), dict) else {}
        rows = items.get("item") or []
        rows = [rows] if isinstance(rows, dict) else rows
        for r in rows:
            try:
                c_lat, c_lng = float(r["YPos"]), float(r["XPos"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"name": r.get("yadmNm", ""), "type": r.get("clCdNm", ""), "addr": r.get("addr", ""),
                        "district": " ".join(x for x in (r.get("sgguCdNm"), r.get("emdongNm")) if x),
                        "url": r.get("hospUrl") or "", "lat": c_lat, "lng": c_lng,
                        "distance_m": distance_m(lat, lng, c_lat, c_lng)})
        total = int(body.get("totalCount") or 0)
        if page * 500 >= total or not rows:
            break
        page += 1
    return sorted(out, key=lambda c: c["distance_m"])


def refresh(force: bool = False) -> dict[str, int]:
    """좌표 있는 관광지마다 주변 목록을 갱신 (refresh_days 안이면 건너뜀)."""
    if not configured():
        raise ClinicDataError("DATA_GO_KR_KEY 가 없습니다 (공공데이터포털 병원정보서비스 인증키)")
    cfg, done = settings(), {}
    for aid, a in attractions.load().items():
        if a.get("lat") is None:
            continue
        path = cache_dir() / f"{aid}.json"
        if path.exists() and not force:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(load_json(path)["fetched_at"])
            if age.days < cfg["refresh_days"]:
                continue
        found = fetch_near(a["lat"], a["lng"], cfg["radius_m"])
        save_json(path, {"attraction": aid, "fetched_at": now_iso(), "radius_m": cfg["radius_m"], "source": "HIRA",
                         "clinics": found})
        done[aid] = len(found)
    return done


def _host(url: str) -> str:
    return urlparse(url if "://" in url else f"https://{url}").netloc.lower().removeprefix("www.")


def advertiser(clinic: dict, registered: list[dict]) -> dict | None:
    """스폰서(광고주·파일럿) 병원이면 그 스폰서. 공식 사이트 도메인 또는 한글 병원명으로 맞춘다."""
    host = _host(clinic["url"]) if clinic.get("url") else ""
    for s in registered:
        official = sponsors.official_host(s)
        if (host and (host == official or host.endswith("." + official))) or s["name_ko"] == clinic["name"]:
            return s
    return None


def stops_for(post_text: str) -> list[str]:
    """글(제목·키워드·본문)에 나오는 관광지 중 좌표가 있는 곳 = 코스 정류장."""
    from pipeline import analytics
    catalog = attractions.load()
    tags = analytics.tag({"title": post_text}, analytics.load_catalog("attractions"))
    return [a for a in tags if a in catalog and catalog[a].get("lat") is not None]


def directory(post_text: str) -> list[dict]:
    """코스 정류장별 주변 피부과 (캐시 기준). 캐시가 없으면 빈 목록 — 빌드는 네트워크를 쓰지 않는다."""
    cfg = settings()
    catalog = attractions.load()
    try:
        registered = list(sponsors.load().values())
    except sponsors.SponsorError:
        registered = []
    out = []
    for aid in stops_for(post_text):
        path = cache_dir() / f"{aid}.json"
        if not path.exists():
            continue
        data = load_json(path)
        clinics = [{**c, "advertiser": advertiser(c, registered)} for c in data["clinics"]]
        out.append({"attraction": aid, "name": catalog[aid]["name"], "radius_m": data["radius_m"], "total": len(clinics),
                    "fetched_at": data["fetched_at"][:10], "clinics": clinics[:cfg["max_per_stop"]]})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="코스 주변 피부과 목록 (심평원 공공데이터)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--force", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("attraction")
    args = parser.parse_args(argv)
    if args.cmd == "refresh":
        if not configured():
            log.info("DATA_GO_KR_KEY 미설정 — 주변 병원 목록 갱신 건너뜀")
            return 0
        done = refresh()
        if done:
            log.info("갱신: %s", ", ".join(f"{a} {n}곳" for a, n in done.items()))
        return 0
    path = cache_dir() / f"{args.attraction}.json"
    if not path.exists():
        print(f"캐시 없음: {path}")
        return 1
    data = load_json(path)
    print(f"{args.attraction}: 반경 {data['radius_m']}m 피부과 {len(data['clinics'])}곳 ({data['fetched_at'][:10]} 기준)")
    for c in data["clinics"]:
        print(f"{c['distance_m']}m\t{c['name']}\t{c['type']}\t{c['district']}")
    return 0


if __name__ == "__main__":
    run_cli("clinics", main)
