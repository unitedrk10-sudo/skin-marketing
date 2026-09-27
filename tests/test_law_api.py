import pytest

from pipeline import law_sync
from pipeline.law_api import LawApiError, jo_code, parse_document, parse_search

SEARCH_ONE = {
    "LawSearch": {
        "totalCnt": "1",
        "law": {
            "법령명한글": "의료법",
            "법령ID": "001788",
            "법령일련번호": "270001",
            "시행일자": "20260102",
            "공포일자": "20250101",
            "법령구분명": "법률",
        },
    }
}

DOC = {
    "법령": {
        "기본정보": {"법령명_한글": "의료법", "법령ID": "001788", "시행일자": "20260102", "공포일자": "20250101"},
        "조문": {
            "조문단위": [
                {"조문여부": "전문", "조문내용": "제3장 의료기관"},
                {
                    "조문여부": "조문",
                    "조문번호": "27",
                    "조문제목": "무면허 의료행위 등 금지",
                    "조문내용": "제27조(무면허 의료행위 등 금지)",
                    "항": [
                        {
                            "항번호": "③",
                            "항내용": "③ 누구든지 ... 유인하거나 이를 사주하는 행위를 하여서는 아니 된다.",
                            "호": [{"호번호": "2.", "호내용": ["2. 외국인환자를 유치하기 위한 행위"]}],
                        }
                    ],
                },
                {"조문여부": "조문", "조문번호": "27", "조문가지번호": "2", "조문제목": "외국인환자 유치", "조문내용": "제27조의2 ..."},
                {"조문여부": "조문", "조문번호": "56", "조문제목": "의료광고의 금지 등", "조문내용": "제56조 ..."},
            ]
        },
    }
}


def test_jo_code():
    assert jo_code("27") == "002700"
    assert jo_code("27의2") == "002702"
    assert jo_code("제56조") == "005600"
    with pytest.raises(ValueError):
        jo_code("abc")


def test_parse_search_single_result_dict():
    laws = parse_search(SEARCH_ONE)
    assert len(laws) == 1
    assert laws[0].name == "의료법" and laws[0].mst == "270001"


def test_parse_search_empty():
    assert parse_search({"LawSearch": {"totalCnt": "0"}}) == []


def test_parse_document_skips_headings_and_renders_nested():
    doc = parse_document(DOC)
    assert [a.number for a in doc.articles] == ["27", "27의2", "56"]
    art27 = doc.articles[0]
    assert "유인하거나" in art27.text
    assert "  2. 외국인환자를 유치하기 위한 행위" in art27.text


def test_parse_document_bad_payload():
    with pytest.raises(LawApiError):
        parse_document({"error": "x"})


def test_select_and_diff():
    doc = parse_document(DOC)
    selected = law_sync.select_articles(doc, ["56"], ["외국인환자"])
    assert {a.number for a in selected} == {"27", "27의2", "56"}

    entry = {"name": "의료법", "stage": [1], "why": "t"}
    old = law_sync.snapshot(entry, doc, selected)
    assert law_sync.diff(None, old)[0].startswith("신규 추적")
    assert law_sync.diff(old, old) == []

    doc.articles[2].text += " 개정"
    doc.effective_date = "20270101"
    changes = law_sync.diff(old, law_sync.snapshot(entry, doc, selected))
    assert any("시행일 변경" in c for c in changes)
    assert any("제56조 내용 변경" in c for c in changes)


def test_run_with_fake_client(tmp_path, monkeypatch):
    monkeypatch.setattr(law_sync, "SNAPSHOT_DIR", tmp_path)

    class Fake:
        def find(self, name):
            if name != "의료법":
                raise LawApiError("not found")
            return parse_search(SEARCH_ONE)[0]

        def document(self, mst):
            return parse_document(DOC)

    config = {"laws": [{"name": "의료법", "articles": ["27"]}, {"name": "없는법"}]}
    snaps, changes, errors = law_sync.run(Fake(), config)
    assert len(snaps) == 1 and list(snaps[0]["articles"]) == ["27"]
    assert len(changes) == 1 and len(errors) == 1
    assert "제27조" in law_sync.render_digest(snaps)
