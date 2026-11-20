"""외국인 의료 카드 소비 수집 (pipeline/foreign_medical.py) — 기간 나누기·지역 합치기."""

from pipeline import foreign_medical as fm


def test_chunks_keep_18_month_limit():
    parts = fm.chunks("202401", "202610")
    assert parts == [("202401", "202506"), ("202507", "202610")]
    assert len(fm.months("202401", "202610")) == 34


def test_region_codes():
    assert fm.region_of("41281") == "41280"   # 고양시 덕양구 → 고양시
    assert fm.region_of("41150") == "41150"   # 구 없는 시
    assert fm.region_of("11305") == "11305"   # 서울 구는 그대로
    assert fm.region_of("28110") == fm.region_of("28125") == "IC-JD"  # 인천 개편 전후 같은 땅
    assert fm.region_of("28260") == fm.region_of("28290") == "IC-SG"


def test_region_rows_sum_children_and_skip_empty_parent():
    data = {"sgg": {
        "41280": {"name": "고양시", "sido": "41", "medical": {}, "total": {}},
        "41281": {"name": "고양시 덕양구", "sido": "41", "medical": {"202401": 100.0}, "total": {"202401": 1000.0}},
        "41285": {"name": "고양시 일산동구", "sido": "41", "medical": {"202401": 200.0}, "total": {"202401": 2000.0}},
        "41150": {"name": "의정부시", "sido": "41", "medical": {"202401": 50.0}, "total": {"202401": 500.0}},
        "28110": {"name": "중구", "sido": "28", "medical": {"202401": 10.0}, "total": {}},
        "28140": {"name": "동구", "sido": "28", "medical": {"202401": 5.0}, "total": {}},
    }}
    rows, names = fm.region_rows(data)
    by = {r["region"]: r for r in rows}
    assert by["41280"]["medical_eok"] == fm.eok(300.0) and names["41280"] == "고양시"
    assert by["41150"]["medical_eok"] == fm.eok(50.0)
    assert by["IC-JD"]["medical_eok"] == fm.eok(15.0) and by["IC-JD"]["sido"] == "인천광역시"


def test_clinic_depts_exclude_pharmacy():
    assert "약국" not in fm.CLINIC_DEPTS and "한약방" not in fm.CLINIC_DEPTS
    assert len(fm.CLINIC_DEPTS) == 6


def test_store_depts_rules():
    clinic = {"종별코드명": "의원", "요양기관명": "OO클리닉의원"}
    assert set(fm.store_depts(clinic, {"14", "08"})) == {"피부과", "성형외과", fm.CLINIC_ALL, fm.ALL_DEPT}
    assert fm.store_depts({"종별코드명": "의원", "요양기관명": "OO피부과의원"}, set()) == ["피부과", fm.CLINIC_ALL, fm.ALL_DEPT]
    assert fm.CLINIC_ALL not in fm.store_depts({"종별코드명": "병원", "요양기관명": "OO병원"}, {"14"})
    assert fm.store_depts({"종별코드명": "치과의원", "요양기관명": "OO치과"}, {"49"}) == ["치과", fm.ALL_DEPT]
    assert fm.store_depts({"종별코드명": "약국", "요양기관명": "OO약국"}, set()) == ["약국", fm.ALL_DEPT]
    assert fm.store_depts({"종별코드명": "보건소", "요양기관명": "OO보건소"}, {"14"}) == []


def test_registered_basis():
    derm = {"종별코드명": "의원", "요양기관명": "OO클리닉의원"}
    assert fm.REG + "피부과" in fm.store_depts(derm, {"14": 0}, registered=True)       # 피부과는 신고만으로
    assert fm.REG + "성형외과" not in fm.store_depts(derm, {"08": 0}, registered=True)  # 성형외과는 전문의가 있어야
    assert fm.REG + "성형외과" in fm.store_depts(derm, {"08": 1}, registered=True)
    assert fm.REG + "성형외과" in fm.store_depts({"종별코드명": "의원", "요양기관명": "OO성형외과의원"}, {}, registered=True)
    assert not [d for d in fm.store_depts(derm, {"14": 0}) if d.startswith(fm.REG)]       # 미등록은 등록 칸 없음
    assert not [d for d in fm.store_depts({"종별코드명": "약국", "요양기관명": "OO약국"}, {}, registered=True) if d.startswith(fm.REG)]


def test_match_registry_unique_names_only():
    insts = [{"시도코드명": "서울", "요양기관명": "가나피부과의원", "종별코드명": "의원", "암호화요양기호": "a"},
             {"시도코드명": "서울", "요양기관명": "다라 성형외과의원", "종별코드명": "의원", "암호화요양기호": "b"},
             {"시도코드명": "서울", "요양기관명": "마바한의원", "종별코드명": "한의원", "암호화요양기호": "c"},
             {"시도코드명": "서울", "요양기관명": "마바한의원", "종별코드명": "한의원", "암호화요양기호": "d"}]
    reg = [{"상호명": "가나피부과의원", "기관구분": "의원", "지역": "서울"},
           {"상호명": "다라성형외과", "기관구분": "의원", "지역": "서울"},   # "의원" 빠진 이름
           {"상호명": "마바한의원", "기관구분": "한의원", "지역": "서울"}]   # 동명 2곳 → 버림
    ids, stats = fm.match_registry(insts, reg)
    assert ids == {"a", "b"} and stats == {"matched": 2, "ambiguous": 1, "missing": 0}
