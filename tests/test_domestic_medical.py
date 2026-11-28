"""내국인 의료 소비 (pipeline/domestic_medical.py) — 서울 추정매출 파싱·합계."""

import io
import zipfile

from pipeline import domestic_medical as dm

HEADER = ("기준_년분기_코드,행정동_코드,행정동_코드_명,서비스_업종_코드,서비스_업종_코드_명,당월_매출_금액,당월_매출_건수,"
          "남성_매출_금액,여성_매출_금액,연령대_10_매출_금액,연령대_20_매출_금액,연령대_30_매출_금액,연령대_40_매출_금액,"
          "연령대_50_매출_금액,연령대_60_이상_매출_금액")


def _zip(lines: list[str], enc: str = "cp949") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", ("\n".join([HEADER, *lines]) + "\n").encode(enc))
    return buf.getvalue()


def test_parse_keeps_only_medical_categories():
    raw = _zip(["20251,11680510,신사동,CS200006,일반의원,1000000000,100,400000000,600000000,0,1,2,3,4,5",
                "20251,11680510,신사동,CS100001,한식음식점,5,1,1,1,0,0,0,0,0,0"])
    rows = dm.parse_seoul(raw)
    assert [r["cat"] for r in rows] == ["일반의원"] and rows[0]["amount"] == 1e9


def test_quarterly_rows_sum_dongs_into_gu(monkeypatch):
    monkeypatch.setattr(dm, "gu_names", lambda: {"11680": "강남구"})
    rows = dm.parse_seoul(_zip([
        "20251,11680510,신사동,CS200006,일반의원,100000000,10,40000000,60000000,0,0,0,0,0,0",
        "20251,11680640,역삼1동,CS200006,일반의원,300000000,30,100000000,200000000,0,0,0,0,0,0"], enc="utf-8-sig"))
    gu = dm.quarterly_rows({"rows": rows}, "gu")
    assert len(gu) == 1 and gu[0]["name"] == "강남구" and gu[0]["amount_eok"] == 4.0
    assert gu[0]["female_share"] == 0.65


def test_kosis_rows_keep_capital_area_health_only():
    raw = [{"C1_NM": "서울특별시", "C2_NM": "보건업 및 사회복지 서비스업", "ITM_NM": "원지수", "PRD_DE": "202501", "DT": "120.5", "UNIT_NM": "2020=100"},
           {"C1_NM": "부산광역시", "C2_NM": "보건업 및 사회복지 서비스업", "ITM_NM": "원지수", "PRD_DE": "202501", "DT": "110"},
           {"C1_NM": "경기도", "C2_NM": "도매 및 소매업", "ITM_NM": "원지수", "PRD_DE": "202501", "DT": "99"},
           {"C1_NM": "인천광역시", "C2_NM": "보건업 및 사회복지 서비스업", "ITM_NM": "원지수", "PRD_DE": "202501", "DT": "-"}]
    rows = dm.kosis_rows(raw)
    assert [(r["sido"], r["value"]) for r in rows] == [("서울특별시", 120.5)]


def test_fetch_kosis_needs_key(monkeypatch):
    import pytest
    monkeypatch.delenv("KOSIS_API_KEY", raising=False)
    with pytest.raises(dm.FetchError):
        dm.fetch_kosis()
