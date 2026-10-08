"""01 → 02 → 02b → 03 흐름을 가짜 LLM·가짜 웹 응답으로 검증한다 (네트워크·API 키 불필요)."""

import importlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from pipeline import common, llm, web

topics_mod = importlib.import_module("pipeline.01_topics")
draft_mod = importlib.import_module("pipeline.02_draft")
review_mod = importlib.import_module("pipeline.02b_auto_review")
human_mod = importlib.import_module("pipeline.03_review")

URL = "https://pubmed.ncbi.nlm.nih.gov/12345/"
DISCLOSURE = "This article was produced with AI assistance and is based on publicly available sources. It is not medical advice."

TOPICS = {"topics": [
    {"title": "What is Rejuran?", "axis": "procedure", "angle": "How salmon DNA boosters work", "why_now": "trend",
     "keywords": ["rejuran"], "hook": "Salmon DNA on your face?", "has_price": False, "sources": []},
    {"title": "Skin booster price range", "axis": "price_guide", "angle": "Korea vs US", "why_now": "search",
     "keywords": ["price"], "hook": "Why so different?", "has_price": True, "sources": []},
]}
QUOTE = "Polynucleotide injections fact"  # ok_fetch 페이지에 들어 있는 문구
FACTS = {"facts": [
    {"id": f"F{i + 1}", "text": f"Polynucleotide injections fact {i}", "url": URL, "source_title": "PubMed", "kind": "mechanism"}
    for i in range(6)
] + [{"id": "F9", "text": "no url fact", "url": "", "source_title": ""}]}


def script(voice="Rejuran uses polynucleotides.", fact_ids=("F1",), disclosure="AI-generated content"):
    return {"title": "What is Rejuran? 45s explainer", "hook": "Salmon DNA on your face?",
            "lines": [{"voice": voice, "caption": "Polynucleotides", "visual": "macro", "fact_ids": list(fact_ids)}],
            "on_screen_disclosure": disclosure, "hashtags": ["#kbeauty"], "estimated_seconds": 45}


def blog(markdown=None):
    return {"title": "Rejuran explained", "meta_description": "What it is", "slug": "rejuran",
            "markdown": markdown or f"# Rejuran\n\nIt uses polynucleotides [F1]. Results vary; ask a licensed doctor.\n\n{DISCLOSURE}"}


class FakeLLM:
    """responses["research"] 의 사실(url 별)로 출처 후보를 만들고, 작성 단계는 그 사실 + shortform + blog 를 돌려준다.
    작성 결과를 통째로 바꾸려면 responses["write"] 를 넣는다."""

    def __init__(self):
        self.responses = {
            "topics": TOPICS, "research": FACTS, "shortform": script(), "blog": blog(),
            "source_check": {"results": [{"id": "F1", "verdict": "supported", "evidence": "..."}]},
            "cross_review": {"findings": [], "overall": "fine"},
        }
        self.calls = []

    def response(self, stage):
        facts = self.responses["research"]["facts"]
        if stage == "sources" and "sources" not in self.responses:
            return {"sources": [{"url": f["url"], "title": f.get("source_title", "")} for f in facts if f.get("url")]}
        if stage in ("write", "write_search"):
            return self.responses.get("write") or {
                "facts": [{**f, "quote": QUOTE} for f in facts],
                "shortform": self.responses["shortform"], "blog": self.responses["blog"]}
        return self.responses[stage]

    def __call__(self, stage, cfg, system, prompt):
        self.calls.append((stage, prompt))
        data = json.loads(json.dumps(self.response(stage)))  # 호출마다 새 사본 (작성 결과를 코드가 고친다)
        return llm.LLMResult(text=json.dumps(data), model=f"fake-{cfg.get('model', cfg['provider'])}", stage=stage)


_real_stage_config = llm.stage_config


def _external_stage_config(stage):
    """검수 단계를 Claude Code 외부 검수(02c, 선택 기능)로 시험 — 기본 설정은 Gemini API 검수."""
    if stage in ("source_check", "cross_review"):
        return {"provider": llm.EXTERNAL}
    return _real_stage_config(stage)


def ok_fetch(url):
    return 200, "Polynucleotide injections fact " * 20


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    """테스트가 실제 hermes send 로 텔레그램에 보내지 않게 한다 (기본: 보내기 실패 → 한 메시지 + 첨부 방식)."""
    monkeypatch.setattr(importlib.import_module("pipeline.worker"), "hermes_send", lambda text: False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("SKIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKIN_SPONSORS_FILE", str(tmp_path / "sponsors.yaml"))  # 기본: 스폰서 없음
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # 워커가 실제 Gemini TTS 로 렌더링하지 않게
    fake = FakeLLM()
    llm.set_backend(fake)
    web.set_fetcher(ok_fetch)  # 출처 페이지 수집도 네트워크 없이
    yield fake
    llm.set_backend(None)
    web.set_fetcher(None)


def make_draft(env, **overrides):
    common.save_json(common.content_dir("topics") / "2026-W40.json", TOPICS)
    env.responses.update(overrides)
    return draft_mod.create("2026-W40", 1, TOPICS["topics"][0])


# ---- 프롬프트 ----

@pytest.mark.parametrize("name", ["topic_research", "source_search", "write_draft", "cross_review"])
def test_generation_prompts_include_rules(name):
    text = common.read_prompt(name)
    assert "{{rules}}" in text
    rules = common.read_prompt("_rules")
    assert "source URL" in rules and "clinic" in rules and "AI-generated" in rules


def test_render_rejects_missing_variables():
    with pytest.raises(KeyError):
        common.render("{{a}} {{b}}", a="x")


def test_parse_json_handles_fences_and_prose():
    assert common.parse_json('Here:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert common.parse_json('sure {"a": [1, 2]} done') == {"a": [1, 2]}


def test_llm_stage_models_come_from_config():
    assert llm.stage_config("sources")["provider"] == "gemini" and llm.stage_config("sources")["search"]
    assert llm.stage_config("write")["provider"] == llm.CLAUDE_CLI and llm.stage_config("write")["tools"] == []
    assert set(llm.stage_config("write_search")["tools"]) == {"WebSearch", "WebFetch"}
    assert not llm.is_external("cross_review") and not llm.is_external("source_check")


def test_writing_and_review_use_different_vendors():
    """기획서 6-0: 같은 회사 모델이 쓰고 검수하지 않는다 (작성 Claude ↔ 검수 Gemini)."""
    vendor = {"claude_cli": "anthropic", "claude_code": "anthropic", "anthropic": "anthropic", "gemini": "google"}
    writers = {vendor[llm.stage_config(s)["provider"]] for s in ("write", "write_search")}
    reviewers = {vendor[llm.stage_config(s)["provider"]] for s in ("source_check", "cross_review")}
    assert writers.isdisjoint(reviewers)


def test_external_stage_is_never_called_as_api(monkeypatch):
    monkeypatch.setattr(llm, "stage_config", _external_stage_config)
    with pytest.raises(llm.LLMError):
        llm.generate("cross_review", "x")


# ---- 01 / 02 ----

def test_topics_saved_and_message(env):
    data = topics_mod.build("2026-W40", 2)
    assert [t["title"] for t in data["topics"]] == ["What is Rejuran?", "Skin booster price range"]
    msg = topics_mod.telegram_message("2026-W40", data["topics"])
    assert "1. What is Rejuran?" in msg and "💲가격" in msg


def test_draft_created_with_sourced_facts_only(env):
    draft_id = make_draft(env)
    path = common.content_dir("drafts") / draft_id
    draft = common.load_draft(path)
    assert draft_id == "2026-W40-01-what-is-rejuran"
    assert [f["id"] for f in draft["facts"]] == ["F1", "F2", "F3", "F4", "F5", "F6"]  # 출처 없는 F9 제외
    assert all(f["url"] == URL and f["quote"] == QUOTE for f in draft["facts"])
    assert draft["sources"] == [{"id": "S1", "url": URL, "title": "PubMed"}] and draft["writer"]
    assert (path / "script.md").exists() and (path / "blog.md").exists()
    # 출처가 MIN_SOURCES 보다 적으면 작성 모델이 추가 검색까지 하는 단계로
    assert [c[0] for c in env.calls] == ["sources", "write_search"]
    # 작성 프롬프트에는 받아 둔 페이지 본문과 규칙이 들어간다
    write_prompt = env.calls[1][1]
    assert URL in write_prompt and QUOTE in write_prompt and "NON-NEGOTIABLE CONTENT RULES" in write_prompt


def test_revise_keeps_identity_and_records_note(env):
    draft_id = make_draft(env)
    draft_mod.revise(draft_id, "가격 출처 다시")
    draft = common.load_draft(common.content_dir("drafts") / draft_id)
    assert draft["id"] == draft_id and draft["revisions"][0]["note"] == "가격 출처 다시"
    assert all("가격 출처 다시" in p for _, p in env.calls[-2:])  # 출처 찾기·작성 프롬프트에 수정 요청 반영


def test_parse_pick():
    assert draft_mod.parse_pick("1, 3", 5) == [1, 3]
    with pytest.raises(ValueError):
        draft_mod.parse_pick("7", 5)


def write_result(facts, **kw):
    return {"facts": facts, "shortform": kw.get("shortform", script()), "blog": kw.get("blog", blog())}


def test_grounding_redirects_fetched_at_real_source_urls(env):
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    resolved = {f"{redirect}{i}": f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{i}/" for i in range(5)}  # 5번은 해석 실패
    env.responses["sources"] = {"sources": [{"url": f"{redirect}{i}", "title": f"t{i}"} for i in range(6)]}
    env.responses["write"] = write_result([{"id": f"F{i + 1}", "source": f"S{i + 1}", "quote": QUOTE, "text": f"fact {i}"}
                                           for i in range(5)])
    web.set_resolver(resolved.get)
    try:
        draft = common.load_draft(common.content_dir("drafts") / make_draft(env))
    finally:
        web.set_resolver(None)
    assert [s["url"] for s in draft["sources"]] == list(resolved.values())  # 만료되는 리디렉션 주소는 쓰지 않는다
    assert [f["url"] for f in draft["facts"]] == list(resolved.values())
    assert env.calls[-1][0] == "write"  # 출처가 충분하면 추가 검색 없이 작성


def test_sources_drop_clinic_sites_and_dead_pages(env):
    pages = {"https://www.aad.org/a": (200, QUOTE * 30), "https://gangnam-derma-clinic.com/rejuran": (200, QUOTE * 30),
             "https://news.example/gone": (404, ""), "https://js-only.example/": (200, "Loading..."),
             "https://www.mayoclinic.org/b": (200, QUOTE * 30)}
    web.set_fetcher(lambda url: pages.get(url, (0, "")))
    sources = draft_mod.fetch_sources([{"url": u, "title": ""} for u in pages] + [{"url": "https://www.aad.org/a", "title": "dup"}])
    assert [s["url"] for s in sources] == ["https://www.aad.org/a", "https://www.mayoclinic.org/b"]
    assert [s["id"] for s in sources] == ["S1", "S2"]


def test_facts_kept_only_when_quote_is_on_the_page(env):
    sources = [{"id": "S1", "url": URL, "title": "PubMed", "text": "Downtime is usually 1-3 days with mild swelling. " * 3}]
    facts = draft_mod.verify_facts([
        {"id": "F1", "source": "S1", "quote": "Downtime is usually 1-3 days with mild swelling", "text": "ok"},
        {"id": "F2", "source": "S1", "quote": "Downtime is usually 5-7 days", "text": "number changed"},  # 페이지에 없음
        {"id": "F3", "source": "S9", "quote": "Downtime is usually 1-3 days", "text": "unknown source"},
        {"id": "F1", "source": "S1", "quote": "Downtime is usually 1-3 days with mild swelling", "text": "duplicate id"},
        {"id": "F4", "source": "S1", "quote": "1-3 days", "text": "quote too short"},
    ], sources)
    assert [(f["id"], f["text"]) for f in facts] == [("F1", "ok")]
    assert facts[0]["url"] == URL and facts[0]["source_title"] == "PubMed"


def test_quote_matching_ignores_typography():
    page = "Patients may experience “mild” swelling —\nusually for 1–3 days."
    assert web.quote_in_page('Patients may experience "mild" swelling - usually for 1-3 days', page)
    assert not web.quote_in_page("Patients may experience severe swelling", page)


def test_writer_found_page_is_fetched_and_checked(env):
    extra = "https://www.fda.gov/rejuran-note"
    pages = {extra: (200, "The agency notes injections can cause bruising at the site. " * 12),
             "https://some-clinic-seoul.com/x": (200, "Injections can cause bruising at the site. " * 12)}
    web.set_fetcher(lambda url: pages.get(url, (0, "")))
    sources = []
    facts = draft_mod.verify_facts([
        {"id": "F1", "url": extra, "quote": "injections can cause bruising at the site", "text": "bruising"},
        {"id": "F2", "url": "https://some-clinic-seoul.com/x", "quote": "Injections can cause bruising at the site", "text": "clinic"},
    ], sources)
    assert [f["id"] for f in facts] == ["F1"] and [s["url"] for s in sources] == [extra]  # 병원 사이트는 받지 않는다


def test_sentences_citing_unverified_facts_are_repaired(env):
    good = [{"id": f"F{i}", "source": "S1", "quote": QUOTE, "text": f"fact {i}"} for i in range(1, 6)]
    bad = {"id": "F6", "source": "S1", "quote": "a sentence that is not on the page at all", "text": "made up"}
    env.responses["write"] = write_result(good + [bad], blog=blog(f"It works [F1]. It lasts years [F6].\n\n{DISCLOSURE}"))
    fake = env

    def backend(stage, cfg, system, prompt):
        if "removed some facts" in prompt:
            assert "F6" in prompt and "[F1] fact 1" in prompt and "[F6]" not in prompt.split("REMAINING FACTS")[1].split("CURRENT")[0]
            fake.calls.append(("repair", prompt))
            return llm.LLMResult(text=json.dumps({"shortform": script(), "blog": blog()}), model="fake", stage=stage)
        return fake(stage, cfg, system, prompt)

    llm.set_backend(backend)
    draft = common.load_draft(common.content_dir("drafts") / make_draft(env))
    assert [c[0] for c in fake.calls][-1] == "repair"
    assert "[F6]" not in draft["blog"]["markdown"] and [f["id"] for f in draft["facts"]] == ["F1", "F2", "F3", "F4", "F5"]


def test_no_repair_when_all_cited_facts_verified(env):
    make_draft(env)
    assert not any("removed some facts" in p for _, p in env.calls)


def test_medical_societies_are_not_clinic_sites():
    assert not web.is_clinic_host("https://www.plasticsurgery.org/cosmetic-procedures/laser-skin-resurfacing")
    assert web.is_clinic_host("https://www.seoul-plastic-surgery.co.kr/rejuran")


CONSULT = "The first visit starts with a consultation before any treatment is scheduled."


def _clinic_pages(n):
    return [{"id": f"C{i}", "url": f"https://clinic{i}.example/en/faq", "title": "FAQ", "text": f"Welcome. {CONSULT} " * 20}
            for i in range(1, n + 1)]


def test_practice_fact_needs_three_different_clinics():
    pages = _clinic_pages(3) + [{"id": "C4", "url": "https://clinic1.example/en/other", "title": "", "text": CONSULT * 20}]
    raw = [
        {"id": "F1", "kind": "practice", "text": "Many clinics start with a consultation.",
         "sources": [{"source": "C1", "quote": CONSULT}, {"source": "C2", "quote": CONSULT}, {"source": "C3", "quote": CONSULT}]},
        {"id": "F2", "kind": "practice", "text": "same clinic twice",  # C4 는 C1 과 같은 병원 → 2곳뿐
         "sources": [{"source": "C1", "quote": CONSULT}, {"source": "C4", "quote": CONSULT}, {"source": "C2", "quote": "not on page at all ok"}]},
        {"id": "F3", "source": "C1", "quote": CONSULT, "text": "single clinic as a normal source"},  # 병원 페이지는 일반 출처가 아니다
    ]
    facts = draft_mod.verify_facts(raw, [], pages)
    assert [f["id"] for f in facts] == ["F1"]
    assert facts[0]["kind"] == "practice" and len(facts[0]["urls"]) == 3 and facts[0]["source_title"] == "several clinic websites"


def test_clinic_pages_need_three_clinics_and_skip_sponsored(env):
    pages = {f"https://clinic{i}.example/faq": (200, CONSULT * 20) for i in range(1, 3)}
    web.set_fetcher(lambda url: pages.get(url, (0, "")))
    assert draft_mod.fetch_clinic_pages([{"url": u, "title": ""} for u in pages]) == []  # 2곳뿐 → 공통 정보 못 씀
    pages["https://clinic3.example/faq"] = (200, CONSULT * 20)
    assert len(draft_mod.fetch_clinic_pages([{"url": u, "title": ""} for u in pages])) == 3


def test_practice_facts_publish_without_clinic_links(env):
    facts = [{"id": "F1", "url": "https://www.aad.org/a", "source_title": "AAD", "kind": "downtime"},
             {"id": "F2", "url": "https://clinic1.example/en/faq", "urls": ["https://clinic1.example/en/faq"],
              "source_title": "several clinic websites", "kind": "practice"}]
    out, sources = site_mod._numbered_sources("Redness may last days [F1]. Clinics start with a consultation [F2].", facts)
    assert "clinic1.example" not in out and "[clinic websites]" in out
    assert [s["url"] for s in sources] == ["https://www.aad.org/a"]  # 출처 목록·JSON-LD 에도 병원 주소 없음
    draft = {"facts": facts, "shortform": script(fact_ids=("F1",)), "blog": blog(f"Clinics start with a consultation [F2]. Ok [F1].\n\n{DISCLOSURE}")}
    msgs, human = review_mod.check_rules(draft)
    assert not any("병원 사이트" in m.message for m in msgs) and "병원 사이트 공통 정보 포함" in human
    assert human_mod.medical_sentences(draft, lambda f: f.get("kind") == "practice") == [
        ("Clinics start with a consultation.", ["https://clinic1.example/en/faq"])]


def test_too_few_verified_facts_fails_the_draft(env):
    env.responses["write"] = write_result([{"id": "F1", "source": "S1", "quote": "not on the page at all, made up", "text": "x"}])
    with pytest.raises(llm.LLMError, match="사실이 부족"):
        make_draft(env)


def test_script_fact_ids_normalized(env):
    nested = script()
    nested["lines"] = [{**nested["lines"][0], "fact_ids": [["F1", "F2"]]},
                       {**nested["lines"][0], "fact_ids": "F3, F4"}, {**nested["lines"][0], "fact_ids": None}]
    env.responses["shortform"] = nested
    draft = common.load_draft(common.content_dir("drafts") / make_draft(env))
    assert [ln["fact_ids"] for ln in draft["shortform"]["lines"]] == [["F1", "F2"], ["F3", "F4"], []]


def test_blog_leading_title_heading_removed(env):
    draft = common.load_draft(common.content_dir("drafts") / make_draft(env))
    assert not draft["blog"]["markdown"].lstrip().startswith("#")  # 제목은 title 로 한 번만
    assert draft["blog"]["markdown"].startswith("It uses polynucleotides")


# ---- 02b 규칙 검사 ----

def rules_for(**kw):
    draft = {"facts": [{"id": "F1", "text": "x", "url": URL}], "shortform": script(**kw.get("script", {})),
             "blog": blog(kw.get("markdown"))}
    findings, human = review_mod.check_rules(draft)
    return [f.message for f in findings], human


def test_clean_draft_has_no_rule_findings():
    msgs, _ = rules_for()
    assert msgs == []


@pytest.mark.parametrize("voice,expected", [
    ("The best clinic for Rejuran.", "금지 표현"),
    ("Results are guaranteed.", "금지 표현"),
    ("Visit Glow Skin Clinic in Gangnam.", "병원명"),
    ("Dr. Kim recommends it.", "의사명"),
    ("Call +82 2 123 4567.", "전화번호"),
    ("I tried it last spring.", "체험담"),
    ("Is it painful? I tried it and my skin peeled.", "체험담"),  # 질문 뒤의 체험담은 잡는다
    ("Written by a board-certified dermatologist.", "자격"),
    ("Message us at pf.kakao.com/_abc for a quote.", "예약"),
    ("See before and after photos.", "금지 표현"),
    ("Real patients' results, before and after.", "금지 표현"),
    ("The result is guaranteed, not a guess.", "금지 표현"),  # 부정어가 뒤에 있으면 그대로 차단
    ("No other way: it is the best option.", "금지 표현"),     # 최상급은 부정문 예외 없음
    ("Visit Seoul Glow Dermatology today.", "병원명"),
])
def test_rule_violations_block(voice, expected):
    msgs, _ = rules_for(script={"voice": voice})
    assert any(expected in m for m in msgs), msgs


@pytest.mark.parametrize("voice", [
    "The best time to get it is winter.",
    "According to the American Academy of Dermatology, results vary.",
    "Clinics in the Apgujeong area often ask about booking early.",
    "How To Choose A Clinic in Seoul",
    "Most Korean Dermatology Clinic visits take an hour.",
    "How should I protect my skin if I go outside for a walk?",  # FAQ 독자 질문
    "Keep strict sun protection for four weeks before and after treatment.",  # 시기 표현 (전후 사진 아님)
    "Results can't be guaranteed, and lasers aren't risk-free.",  # 부정문 단서 표현
    "The fall foliage is at its best in late October.",  # 관용 표현
    "That part is best confirmed directly with your treating clinic.",  # 권고 (best + 과거분사)
    "None of this is a guarantee of a pain-free session.",  # none 부정문
])
def test_allowed_phrases_pass(voice):
    msgs, _ = rules_for(script={"voice": voice})
    assert msgs == []


def test_missing_disclosures_block():
    msgs, _ = rules_for(script={"disclosure": ""}, markdown="Text [F1].")
    assert any("AI-generated" in m for m in msgs)
    assert any("AI 활용" in m for m in msgs)


def test_unsourced_numbers_and_unknown_fact_ids_block():
    msgs, _ = rules_for(script={"voice": "Downtime is 3 days.", "fact_ids": []})
    assert any("수치에 출처 없음" in m for m in msgs)
    msgs, _ = rules_for(markdown=f"Claim [F7].\n{DISCLOSURE}")
    assert any("F7" in m for m in msgs)


def test_urls_not_in_sources_block():
    msgs, _ = rules_for(markdown=f"See https://some-clinic.example/price [F1].\n{DISCLOSURE}")
    assert any("출처 목록에 없는 URL" in m for m in msgs)


def test_price_flags_human_review():
    _, human = rules_for(script={"voice": "It typically costs $300-500 in Seoul."})
    assert "가격 포함" in human


@pytest.mark.parametrize("url,flagged", [
    ("https://www.glowclinic.co.kr/blog/rejuran", True),
    ("https://abplasticsurgerykorea.com/ab-blog/rejuran", True),
    ("https://www.mayoclinic.org/tests-procedures/rejuran", False),  # 공신력 있는 의학 정보원
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", False),
])
def test_clinic_website_sources_flagged_for_review(url, flagged):
    draft = {"facts": [{"id": "F1", "text": "x", "url": url}, {"id": "F2", "text": "y", "url": url}],
             "shortform": script(fact_ids=("F1",)), "blog": blog(f"It works [F1][F2].\n\n{DISCLOSURE}")}
    findings, _ = review_mod.check_rules(draft)
    hits = [f for f in findings if "병원 사이트" in f.message]
    assert len(hits) == (1 if flagged else 0)  # 같은 사이트는 한 건으로
    assert all(f.severity == "caution" and "F1,F2" in f.message for f in hits)


def test_auto_review_continues_after_one_draft_fails(env, monkeypatch, capsys):
    make_draft(env)
    draft_mod.create("2026-W40", 2, TOPICS["topics"][1])
    real = review_mod.review

    def flaky(path, fetch=review_mod.fetch_page):
        if path.name.startswith("2026-W40-01"):
            raise llm.LLMError("Gemini 빈 응답 (RECITATION)")
        return real(path, fetch=ok_fetch)

    monkeypatch.setattr(review_mod, "review", flaky)
    assert review_mod.main([]) == 1  # 실패는 종료 코드로 알린다
    out = capsys.readouterr().out
    assert "❗ 2026-W40-01" in out
    assert (common.content_dir("drafts") / "2026-W40-02-skin-booster-price-range" / "review.json").exists()  # 다음 초안은 검수됨


# ---- 02b 출처 검증 · 등급 ----

def test_full_review_pass(env):
    draft_id = make_draft(env)
    path = common.content_dir("drafts") / draft_id
    result = review_mod.review(path, fetch=ok_fetch)
    assert result["grade"] == "pass", result["findings"]
    assert "새 유형 첫 게시물(procedure)" in result["always_human"]
    assert (path / "review.json").exists()


@pytest.mark.parametrize("fetch,verdict,grade", [
    (lambda u: (404, ""), "supported", "block"),
    (lambda u: (0, ""), "supported", "block"),
    (lambda u: (403, ""), "supported", "caution"),
    (ok_fetch, "unsupported", "block"),
    (ok_fetch, "weak", "caution"),
])
def test_source_verification_grades(env, fetch, verdict, grade):
    draft_id = make_draft(env)
    env.responses["source_check"] = {"results": [{"id": "F1", "verdict": verdict}]}
    result = review_mod.review(common.content_dir("drafts") / draft_id, fetch=fetch)
    assert result["grade"] == grade


def test_cross_review_findings_are_caution(env):
    draft_id = make_draft(env)
    env.responses["cross_review"] = {"findings": [{"severity": "minor", "where": "blog", "quote": "q",
                                                   "issue": "soften", "suggestion": "s"}]}
    result = review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
    assert result["grade"] == "caution"
    assert "⚠️" in review_mod.summary_line(result)


# ---- 03 사람 검수 ----

def test_parse_reply_formats():
    cmds = human_mod.parse_reply("1,3,4 승인\n2 수정: 가격 출처 다시\n5 폐기")
    assert [(c.action, c.numbers) for c in cmds] == [("approve", [1, 3, 4]), ("revise", [2]), ("reject", [5])]
    assert cmds[1].note == "가격 출처 다시"
    assert human_mod.parse_reply("전체 승인")[0].numbers == []
    assert human_mod.parse_reply("1~3 승인")[0].numbers == [1, 2, 3]
    assert human_mod.parse_reply("게시 OK")[0].action == "publish_ok"
    assert human_mod.parse_reply("2번 자막 수정: 더 크게")[0].note == "더 크게"
    with pytest.raises(human_mod.ReplyError):
        human_mod.parse_reply("좋아 보이네")


def _two_reviewed_drafts(env):
    make_draft(env)
    second = draft_mod.create("2026-W40", 2, TOPICS["topics"][1])
    first_path = common.content_dir("drafts") / "2026-W40-01-what-is-rejuran"
    review_mod.review(first_path, fetch=ok_fetch)
    env.responses["cross_review"] = {"findings": [{"severity": "minor", "issue": "x"}]}
    review_mod.review(common.content_dir("drafts") / second, fetch=ok_fetch)
    return "2026-W40-01-what-is-rejuran", second


def test_list_and_approve_moves_files_and_logs_agreement(env, tmp_path):
    first, second = _two_reviewed_drafts(env)
    msg = human_mod.list_message("drafts")
    assert "1. What is Rejuran?" in msg and "✅" in msg and "⚠️" in msg
    out = human_mod.apply("1 승인\n2 폐기")
    assert (common.content_dir("approved") / first / "draft.json").exists()
    assert (common.content_dir("rejected") / second / "history.json").exists()
    rows = (tmp_path / "logs" / "review_agreement.csv").read_text(encoding="utf-8").splitlines()
    assert rows[1].endswith("pass,approve,1") and rows[2].endswith("caution,reject,1")
    assert len(out) == 2


def test_approve_all_holds_caution_without_confirm(env):
    first, second = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    out = human_mod.apply("전체 승인")
    assert (common.content_dir("approved") / first).exists()
    assert (common.content_dir("drafts") / second).exists()
    assert any("재확인" in line for line in out)
    human_mod.apply("전체 승인", confirm=True)
    assert (common.content_dir("approved") / second).exists()


def test_review_message_readable_on_phone(env):
    first, second = _two_reviewed_drafts(env)
    msg = human_mod.list_message("drafts")
    assert "🎬 대본:" in msg and "· Rejuran uses polynucleotides." in msg  # 대본 전문
    assert "📝 블로그: Rejuran explained — What it is" in msg
    assert "⚠️ [?/minor] x" in msg  # 걸린 항목을 메시지에서 바로
    assert "script.md" not in msg  # PC 경로 대신 첨부
    media = re.findall(r"^MEDIA:`(.+)`$", msg, re.M)
    assert len(media) == 2 and all(Path(m).exists() for m in media)
    doc = Path(media[1]).read_text(encoding="utf-8")
    assert doc.startswith(f"# {second}") and "## 검수에서 걸린 항목" in doc and "## 블로그" in doc
    assert f"> {QUOTE}" in doc and URL in doc  # 사실별 원문 인용·출처


def test_approved_draft_can_be_withdrawn_before_publishing(env):
    first, second = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    human_mod.apply("전체 승인", confirm=True)
    msg = human_mod.list_message("approved")
    assert msg.startswith("[승인됨 2건") and "1. What is Rejuran?" in msg
    with pytest.raises(human_mod.ReplyError):
        human_mod.apply("1 승인", stage="approved")  # 승인됨 단계에서는 철회(폐기)만
    out = human_mod.apply("2 폐기", stage="approved")
    assert "rejected" in out[0] and (common.content_dir("rejected") / second / "draft.json").exists()
    assert (common.content_dir("approved") / first).exists()
    rows = (common.log_dir() / "review_agreement.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3  # 헤더 + 승인 2건 — 철회는 자동/사람 일치 기록에 넣지 않는다


def test_invalid_reply_moves_nothing(env):
    first, _ = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    with pytest.raises(human_mod.ReplyError):
        human_mod.apply("1 승인\n9 폐기")  # 9번 없음 → 1번도 처리하지 않는다
    assert (common.content_dir("drafts") / first).exists()


def test_revise_reply_enqueues_worker_request(env):
    first, _ = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    out = human_mod.apply("1 수정: 톤 부드럽게")
    reqs = list((common.content_dir() / "requests").glob("*.json"))
    assert len(reqs) == 1 and "접수" in out[0]
    req = common.load_json(reqs[0])
    assert req == {**req, "kind": "revise", "draft_id": first, "note": "톤 부드럽게"}


def test_publish_ok_only_from_rendered(env):
    first, _ = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    with pytest.raises(human_mod.ReplyError):
        human_mod.apply("게시 OK")  # drafts 단계에서는 불가
    human_mod.apply("1 승인")
    # 04 렌더링 단계 대신 직접 rendered 로 옮겨 게시 전 확인을 시험
    (common.content_dir("rendered")).mkdir(parents=True)
    (common.content_dir("approved") / first).rename(common.content_dir("rendered") / first)
    human_mod.list_message("rendered")
    human_mod.apply("게시 OK", stage="rendered")
    assert (common.content_dir("ready_to_publish") / first / "draft.json").exists()


def test_disallowed_moves_rejected(env):
    with pytest.raises(ValueError):
        human_mod.move("x", "drafts", "ready_to_publish")
    with pytest.raises(ValueError):
        human_mod.move("x", "approved", "published")


def test_visual_directions_are_checked():
    draft = {"facts": [{"id": "F1", "text": "x", "url": URL}], "shortform": script(), "blog": blog()}
    draft["shortform"]["lines"][0]["visual"] = "split-screen before and after of a patient"
    findings, _ = review_mod.check_rules(draft)
    assert any("before and after" in f.message for f in findings)


# ---- 02c Claude Code 외부 검수 ----

external_mod = importlib.import_module("pipeline.02c_external_review")


@pytest.fixture
def external(env, monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "stage_config", _external_stage_config)  # 검수 단계 = claude_code
    monkeypatch.setattr(external_mod, "queue_dir", lambda kind: tmp_path / "review-queue" / kind)
    return env


def _pending_draft(external):
    draft_id = make_draft(external)
    path = common.content_dir("drafts") / draft_id
    result = review_mod.review(path, fetch=ok_fetch)
    return draft_id, path, result


def test_external_mode_marks_pending_without_api_calls(external):
    _, path, result = _pending_draft(external)
    assert result["grade"] == "pending"
    assert not any(stage in ("source_check", "cross_review") for stage, _ in external.calls)
    assert result["sources"][0]["page_excerpt"].startswith("Polynucleotide")
    assert "⏳" in review_mod.summary_line(result)


def test_rule_block_outranks_pending(external):
    external.responses["shortform"] = script(voice="The best clinic for Rejuran.")
    _, _, result = _pending_draft(external)
    assert result["grade"] == "block"


def test_export_packet_and_no_duplicate_export(external):
    draft_id, path, _ = _pending_draft(external)
    packet_path = external_mod.export()
    packet = common.load_json(packet_path)
    entry = packet["drafts"][0]
    assert entry["draft_id"] == draft_id and entry["sources"][0]["fact_ids"] == ["F1"]
    assert "page_excerpt" in entry["sources"][0] and entry["needs_cross_review"]
    assert "NON-NEGOTIABLE CONTENT RULES" in packet["instructions"] and packet["packet_id"] in packet["instructions"]
    assert external_mod.export() is None  # 이미 요청한 초안은 다시 내보내지 않는다
    packet_path.unlink()  # claude -p 실패 시 스크립트가 요청 파일을 버린다
    assert external_mod.export() is not None  # → 다음 실행 때 재요청


def _result(packet, verdict="supported", findings=()):
    entry = packet["drafts"][0]
    return {"packet_id": packet["packet_id"], "reviewer": "claude-code", "drafts": [{
        "draft_id": entry["draft_id"], "draft_hash": entry["draft_hash"],
        "sources": [{"id": "F1", "verdict": verdict, "evidence": "..."}],
        "findings": list(findings), "overall": "ok"}]}


@pytest.mark.parametrize("verdict,findings,grade", [
    ("supported", [], "pass"),
    ("weak", [], "caution"),
    ("unsupported", [], "block"),
    ("supported", [{"severity": "minor", "where": "blog", "quote": "q", "issue": "soften", "suggestion": "s"}], "caution"),
])
def test_import_regrades(external, tmp_path, verdict, findings, grade):
    _, path, _ = _pending_draft(external)
    packet = common.load_json(external_mod.export())
    result_path = tmp_path / "r.result.json"
    common.save_json(result_path, _result(packet, verdict, findings))
    lines = external_mod.import_result(result_path)
    review = common.load_review(path)
    assert review["grade"] == grade and len(lines) == 1
    assert review["cross_review"]["model"] == "claude-code"
    assert "page_excerpt" not in review["sources"][0]
    assert not any(f["severity"] == "pending" for f in review["findings"])
    assert external_mod.import_result(result_path) == []  # 두 번 반영해도 무해
    assert not list((tmp_path / "review-queue" / "pending").glob("*.json"))  # 반영 후 요청 파일 삭제


def test_import_skips_drafts_changed_after_export(external, tmp_path):
    draft_id, path, _ = _pending_draft(external)
    packet = common.load_json(external_mod.export())
    draft_mod.revise(draft_id, "tone")
    review_mod.review(path, fetch=ok_fetch)
    result_path = tmp_path / "r.result.json"
    common.save_json(result_path, _result(packet))
    assert external_mod.import_result(result_path) == []
    assert common.load_review(path)["grade"] == "pending"
    assert external_mod.export() is not None  # 바뀐 초안은 다시 요청


def test_missing_verdict_needs_human(external, tmp_path):
    _, path, _ = _pending_draft(external)
    packet = common.load_json(external_mod.export())
    result = _result(packet)
    result["drafts"][0]["sources"] = []
    common.save_json(tmp_path / "r.json", result)
    external_mod.import_result(tmp_path / "r.json")
    assert common.load_review(path)["grade"] == "caution"


def test_pending_drafts_are_not_approved_without_confirm(external):
    draft_id, _, _ = _pending_draft(external)
    human_mod.list_message("drafts")
    out = human_mod.apply("1 승인")
    assert "검수 대기" in out[0]
    assert (common.content_dir("drafts") / draft_id).exists()


# ---- worker (Hermes 크론) ----

worker_mod = importlib.import_module("pipeline.worker")


def fake_claude(verdict="supported"):
    """claude -p 대신: 요청 파일을 읽어 결과 파일을 쓴다."""
    def runner(packet_path):
        packet = common.load_json(packet_path)
        result = {"packet_id": packet["packet_id"], "reviewer": "claude-code", "drafts": [
            {"draft_id": d["draft_id"], "draft_hash": d["draft_hash"],
             "sources": [{"id": fid, "verdict": verdict} for s in d["sources"] for fid in s["fact_ids"]],
             "findings": [], "overall": "ok"} for d in packet["drafts"]]}
        out = packet_path.parent.parent / "done" / f"{packet['packet_id']}.result.json"
        common.save_json(out, result)
        return out
    return runner


@pytest.fixture
def worker_env(external, monkeypatch):
    monkeypatch.setattr(review_mod, "fetch_page", ok_fetch)
    monkeypatch.setattr(review_mod.review_with_regeneration, "__defaults__", (ok_fetch,))
    common.save_json(common.content_dir("topics") / "2026-W40.json", TOPICS)
    return external


def test_request_drafts_validates_and_enqueues(worker_env):
    msg = worker_mod.request_drafts("1, 2")
    assert "2026-W40" in msg and "What is Rejuran?" in msg
    assert len(list(worker_mod.requests_dir().glob("*.json"))) == 1
    with pytest.raises(ValueError):
        worker_mod.request_drafts("9")


def test_worker_full_cycle_then_silent(worker_env):
    worker_mod.request_drafts("1")
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert failures == []
    assert "1. What is Rejuran?" in message and "✅" in message
    assert not list(worker_mod.requests_dir().glob("*.json"))
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert message == "" and failures == []  # 할 일 없으면 조용한 틱


def test_worker_claude_failure_reported_and_retried(worker_env):
    worker_mod.request_drafts("1")

    def broken(packet_path):
        packet_path.unlink()
        raise external_mod.ClaudeReviewError("no result")

    message, failures = worker_mod.run(review_runner=broken)
    assert failures and "⏳" in message and "Claude Code 검수 실패" in message
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert failures == [] and "✅" in message  # 다음 틱에 재요청돼 반영


def test_worker_failed_request_is_quarantined(worker_env):
    worker_mod.enqueue("revise", draft_id="nope", note="x")
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert failures and (worker_mod.requests_dir() / "failed").exists()
    assert not list(worker_mod.requests_dir().glob("*.json"))


def test_caution_findings_are_fixed_once_before_human_review(env):
    env.responses["cross_review"] = {"findings": [
        {"severity": "major", "where": "blog", "quote": "It uses polynucleotides", "issue": "adds a detail", "suggestion": "cut"}],
        "overall": "x"}
    path = common.content_dir("drafts") / make_draft(env)
    result = review_mod.review_with_regeneration(path, fetch=ok_fetch)
    fixes = [p for stage, p in env.calls if stage == "write" and "REVIEW FINDINGS" in p]
    assert len(fixes) == 1  # ⚠️ 지적 → 자동 수정 1회만 (고친 뒤에도 남으면 사람에게)
    assert "adds a detail" in fixes[0] and "quoted: It uses polynucleotides" in fixes[0]
    draft = common.load_draft(path)
    assert draft["autofixed"] == 1 and draft["revisions"][-1]["kind"] == "fix"
    assert result["grade"] == "caution" and common.load_review(path) == result  # 고친 버전으로 다시 검수
    human_mod.list_message("drafts")
    assert "🛠 검수 지적 1건 자동 수정" in human_mod.draft_messages("drafts")[0]
    review_mod.review_with_regeneration(path, fetch=ok_fetch)  # 같은 버전은 다시 고치지 않는다
    assert len([p for stage, p in env.calls if stage == "write" and "REVIEW FINDINGS" in p]) == 1


def test_unfixable_cautions_go_straight_to_human(env):
    path = common.content_dir("drafts") / make_draft(env)
    review_mod.review_with_regeneration(path, fetch=lambda u: (403, ""))  # 봇 차단 출처 → 문장으로 못 고친다
    assert common.load_review(path)["grade"] == "caution"
    assert not any(stage == "write" and "REVIEW FINDINGS" in p for stage, p in env.calls)


def test_worker_sends_each_draft_then_summary(worker_env):
    worker_mod.request_drafts("1, 2")
    sent = []
    message, failures = worker_mod.run(review_runner=fake_claude(), sender=lambda text: sent.append(text) or True)
    assert not failures and len(sent) == 2
    assert sent[0].startswith("[초안 1/2]") and "🎬 숏폼 대본" in sent[0] and "📝 블로그: Rejuran explained" in sent[0]
    # 남은 지적 → 의료 문장과 출처 → 대본 순, 블로그 전문은 요청할 때만
    assert sent[0].index("🔎 남은 지적") < sent[0].index("🩺 의료 정보 확인") < sent[0].index("🎬 숏폼 대본")
    assert f"• It uses polynucleotides.\n   ↳ {URL}" in sent[0] and f"• (대본) Rejuran uses polynucleotides.\n   ↳ {URL}" in sent[0]
    assert "Results vary; ask a licensed doctor." not in sent[0] and "`1번 블로그 보여줘`" in sent[0]
    assert "↑ 초안 전문은 위 메시지에" in message and "MEDIA:" not in message and "🎬" not in message  # 요약만
    assert "1. What is Rejuran?" in message


def test_medical_sentences_list_only_medical_citations():
    draft = {"facts": [{"id": "F1", "url": "https://www.aad.org/a", "kind": "downtime"},
                       {"id": "F2", "url": "https://english.visitkorea.or.kr/x", "kind": "travel"}],
             "blog": {"markdown": "## Recovery\n\n- **Redness** may last 1-3 days [F1]. The palace opens at 9:00 [F2]. "
                                  "Ask your clinic.\n\nBoth [F1][F2] apply."},
             "shortform": {"lines": [{"voice": "Redness may last days.", "fact_ids": ["F1"]},
                                     {"voice": "Palace opens at nine.", "fact_ids": ["F2"]}]}}
    assert human_mod.medical_sentences(draft) == [
        ("Redness may last 1-3 days.", ["https://www.aad.org/a"]),  # 마크다운 기호·[F#] 제거
        ("Both apply.", ["https://www.aad.org/a"]),                    # 여행 출처는 빼고 의료 출처만
        ("(대본) Redness may last days.", ["https://www.aad.org/a"])]


def test_worker_falls_back_to_attachments_when_send_fails(worker_env):
    worker_mod.request_drafts("1")
    message, _ = worker_mod.run(review_runner=fake_claude(), sender=lambda text: False)
    assert "MEDIA:" in message and "🎬 대본:" in message


def test_worker_keeps_request_when_writer_is_rate_limited(worker_env):
    worker_mod.request_drafts("1")
    real = worker_env.__class__.__call__

    def limited(self, stage, cfg, system, prompt):
        if stage.startswith("write"):
            raise llm.RateLimited("Claude 사용량 한도: usage limit reached")
        return real(self, stage, cfg, system, prompt)

    llm.set_backend(limited.__get__(worker_env))
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert not failures and "⏳ Claude 사용량 한도" in message  # 실패가 아니다 — 대기 안내 한 번
    assert len(list(worker_mod.requests_dir().glob("*.json"))) == 1  # 버리지 않고 나중에 재시도
    assert not (worker_mod.requests_dir() / "failed").exists()
    assert worker_mod.claude_hold() is not None
    assert worker_mod.run(review_runner=fake_claude()) == ("", [])  # 대기 중에는 조용히 건너뛴다 (알림·비용 없음)
    llm.set_backend(worker_env)
    worker_mod.hold_file().unlink()  # 초기화 시각이 지났다고 치고
    message, failures = worker_mod.run(review_runner=fake_claude())
    assert not failures and not list(worker_mod.requests_dir().glob("*.json"))
    assert not worker_mod.hold_file().exists()


@pytest.mark.parametrize("error,now,until", [
    ("You've hit your session limit · resets 11:30am (Asia/Seoul)", "2026-10-06T09:44", "2026-10-06T11:32"),
    ("usage limit · resets 1pm", "2026-10-06T22:10", "2026-10-07T13:02"),   # 이미 지났으면 다음 날
    ("Claude AI usage limit reached", "2026-10-06T09:44", "2026-10-06T10:16"),  # 시각을 못 읽으면 30분
])
def test_claude_hold_until_reset_time(worker_env, error, now, until):
    from datetime import datetime as dt
    assert worker_mod.set_hold(error, dt.fromisoformat(now)) == dt.fromisoformat(until)
    assert worker_mod.claude_hold(dt.fromisoformat(now)) == dt.fromisoformat(until)
    assert worker_mod.claude_hold(dt.fromisoformat(until)) is None


@pytest.mark.parametrize("stdout,code,expect", [
    (json.dumps({"is_error": False, "result": '{"ok": true}',  # 보조 작업용 Haiku 가 먼저 나와도 실제 작성 모델을 기록
                 "modelUsage": {"claude-haiku-4-5": {"outputTokens": 40}, "claude-sonnet-5": {"outputTokens": 5000}}}), 0, "ok"),
    (json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1759712400"}), 1, llm.RateLimited),
    (json.dumps({"is_error": True, "result": "Invalid model name"}), 1, llm.LLMError),
    ("not json", 1, llm.LLMError),
])
def test_claude_cli_provider(monkeypatch, stdout, code, expect):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    cfg = {"provider": "claude_cli", "tools": ["WebSearch", "WebFetch"]}
    if expect == "ok":
        r = llm._claude_cli("write", cfg, None, "PROMPT")
        assert r.text == '{"ok": true}' and r.model == "claude-sonnet-5"
        assert seen["input"] == "PROMPT" and "WebSearch,WebFetch" in seen["cmd"]
        assert "skin-claude-" in seen["cwd"]  # 저장소 밖에서 실행 (CLAUDE.md 를 읽지 않게)
    else:
        with pytest.raises(expect):
            llm._claude_cli("write", cfg, None, "PROMPT")


def test_worker_lock_is_exclusive(tmp_path):
    path = tmp_path / ".lock"
    with path.open("w") as first:
        assert worker_mod.try_lock(first)
        with path.open("w") as second:
            assert not worker_mod.try_lock(second)  # 이전 실행이 잡고 있으면 건너뜀
    with path.open("w") as again:
        assert worker_mod.try_lock(again)  # 닫으면 풀린다


def test_worker_revise_request_regenerates(worker_env):
    worker_mod.request_drafts("1")
    worker_mod.run(review_runner=fake_claude())
    human_mod.apply("1 수정: 톤 부드럽게")
    message, failures = worker_mod.run(review_runner=fake_claude())
    draft = common.load_draft(common.content_dir("drafts") / "2026-W40-01-what-is-rejuran")
    assert failures == [] and draft["revisions"][-1]["note"] == "톤 부드럽게" and "✅" in message


# ---- 스폰서 트랙 ----

sponsors_mod = importlib.import_module("pipeline.sponsors")
from datetime import date, timedelta  # noqa: E402

SPONSOR = {"id": "glow", "name_en": "Glow Skin Clinic", "name_ko": "글로우피부과의원",
           "official_url": "https://www.glow-clinic.example",
           "contract": {"type": "monthly", "start": "2026-01-01", "end": "2099-12-31"},
           "ad_review_required": False, "review_no": ""}


def write_sponsors(tmp_path, *items):
    import yaml
    (tmp_path / "sponsors.yaml").write_text(yaml.safe_dump({"sponsors": list(items)}, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def sponsored(env, tmp_path):
    write_sponsors(tmp_path, SPONSOR)
    env.responses["blog"] = blog(f"# Rejuran at Glow Skin Clinic\n\nGlow Skin Clinic offers Rejuran [F1]. "
                                 f"See https://www.glow-clinic.example/rejuran. Results vary; consult a doctor.")
    return env


def make_sponsored(env):
    return draft_mod.create_sponsored("glow", "Rejuran at Glow Skin Clinic", "what to expect")


@pytest.mark.parametrize("change,error", [
    ({"contract": {"type": "per_patient", "start": "2026-01-01", "end": "2026-12-31"}}, "monthly"),
    ({"official_url": "http://glow.example"}, "https"),
    ({"id": "Glow Clinic"}, "id"),
])
def test_sponsor_validation(change, error):
    with pytest.raises(sponsors_mod.SponsorError, match=error):
        sponsors_mod.validate({**SPONSOR, **change})


def test_sponsored_draft_has_disclosures_and_sponsor_rules(sponsored):
    draft_id = make_sponsored(sponsored)
    draft = common.load_draft(common.content_dir("drafts") / draft_id)
    assert draft_id.startswith("sp-glow-") and draft["content_type"] == "sponsored"
    assert draft["shortform"]["on_screen_disclosure"].startswith("Sponsored by Glow Skin Clinic")
    assert draft["blog"]["markdown"].startswith("> **Sponsored content — this is an advertisement by Glow Skin Clinic.**")
    research_prompt = sponsored.calls[0][1]
    assert "labeled advertisement" in research_prompt and "NON-NEGOTIABLE CONTENT RULES" not in research_prompt


def test_sponsored_rules_allow_own_clinic_only(sponsored):
    draft = common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored))
    findings, human = review_mod.check_rules(draft)
    assert [f.message for f in findings] == []
    assert any("병원 확인 필요" in h for h in human)
    draft["blog"]["markdown"] += " Unlike Shine Dermatology, call 02-123-4567."
    msgs = [f.message for f in review_mod.check_rules(draft)[0]]
    assert any("병원명" in m for m in msgs) and any("전화번호" in m for m in msgs)


def test_sponsored_missing_disclosure_or_expired_contract_blocks(sponsored, tmp_path):
    draft = common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored))
    draft["blog"]["markdown"] = draft["blog"]["markdown"].split("\n\n", 1)[1]
    draft["shortform"]["on_screen_disclosure"] = "AI-generated content"
    msgs = [f.message for f in review_mod.check_rules(draft)[0]]
    assert any("[blog] 스폰서 광고 표시 없음" in m for m in msgs) and any("[script] 영상 내 스폰서" in m for m in msgs)
    expired = {**SPONSOR, "contract": {"type": "monthly", "start": "2020-01-01", "end": "2020-12-31"},
               "ad_review_required": True}
    draft["sponsor"] = sponsors_mod.validate(expired)
    msgs = [f.message for f in review_mod.check_rules(draft)[0]]
    assert any("계약 기간 아님" in m for m in msgs) and any("심의번호 없음" in m for m in msgs)


def test_expired_sponsor_cannot_start_draft(env, tmp_path):
    write_sponsors(tmp_path, {**SPONSOR, "contract": {"type": "per_post", "start": "2020-01-01", "end": "2020-02-01"}})
    with pytest.raises(sponsors_mod.SponsorError):
        make_sponsored(env)
    with pytest.raises(ValueError):
        worker_mod.request_sponsored("glow", "t", "a")


def test_neutral_draft_mentioning_sponsor_is_blocked(env, tmp_path):
    write_sponsors(tmp_path, SPONSOR)
    msgs, _ = rules_for(script={"voice": "Many visitors go to glow-clinic.example for this."})
    assert any("중립 글에 스폰서 병원 노출" in m for m in msgs)


def test_sponsored_approval_requires_current_clinic_confirmation(sponsored):
    draft_id = make_sponsored(sponsored)
    path = common.content_dir("drafts") / draft_id
    review_mod.review(path, fetch=ok_fetch)
    msg = human_mod.list_message("drafts")
    assert "💼글로우피부과의원 광고 · 병원확인 대기" in msg
    out = human_mod.apply("1 승인", confirm=True)
    assert "병원 확인 전" in out[0] and path.exists()  # --confirm 으로도 우회 불가
    human_mod.apply("1 병원확인")
    draft_mod.revise(draft_id, "tone")  # 내용이 바뀌면 병원 확인 무효
    assert "병원 확인 전" in human_mod.apply("1 승인", confirm=True)[0]
    human_mod.apply("1 병원확인")
    human_mod.apply("1 승인", confirm=True)
    assert (common.content_dir("approved") / draft_id / "sponsor_approval.json").exists()


def test_sponsor_ok_on_neutral_draft_is_noop(env):
    make_draft(env)
    human_mod.list_message("drafts")
    assert "스폰서 글이 아닙니다" in human_mod.apply("1 병원확인")[0]


def test_worker_processes_sponsored_request(sponsored, monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "stage_config", _external_stage_config)
    monkeypatch.setattr(external_mod, "queue_dir", lambda kind: tmp_path / "review-queue" / kind)
    monkeypatch.setattr(review_mod.review_with_regeneration, "__defaults__", (ok_fetch,))
    assert "요청 접수" in worker_mod.request_sponsored("glow", "Rejuran at Glow Skin Clinic", "what to expect")
    captured = {}

    def runner(packet_path):
        captured["packet"] = common.load_json(packet_path)
        return fake_claude()(packet_path)

    message, failures = worker_mod.run(review_runner=runner)
    assert failures == [] and "💼글로우피부과의원" in message
    assert captured["packet"]["drafts"][0]["sponsor"]["name_en"] == "Glow Skin Clinic"
    assert "Sponsored drafts" in captured["packet"]["instructions"]


# ---- 링크 유입 추적 (tracker) ----

tracker_mod = importlib.import_module("pipeline.tracker")

STATS = {"from": "2026-10-01", "to": "2026-10-31", "sponsor": "glow",
         "totals": {"clicks": 40, "unique_daily": 31, "bots": 5},
         "by_source": [{"source": "tiktok", "clicks": 25}, {"source": "ai", "clicks": 10}, {"source": "direct", "clicks": 5}],
         "by_link": [{"code": "abc234", "label": "Rejuran guide", "sponsor_id": "glow", "clicks": 40}],
         "by_day": [{"day": "2026-10-02", "clicks": 40}], "by_country": [{"country": "US", "clicks": 30}, {"country": "TH", "clicks": 10}]}


def test_tracker_add_link_only_to_sponsor_site(env, tmp_path, monkeypatch):
    write_sponsors(tmp_path, SPONSOR)
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    sent = {}
    monkeypatch.setattr(tracker_mod, "_request", lambda m, p, body=None, query=None: sent.update(body=body) or
                        {"code": "abc234", "url": "https://go.example/abc234"})
    tracker_mod.add_link("glow", None, "Rejuran guide")
    assert sent["body"]["target_url"] == "https://www.glow-clinic.example"
    tracker_mod.add_link("glow", "https://book.glow-clinic.example/rejuran", "sub")  # 하위 도메인 허용
    with pytest.raises(tracker_mod.TrackerError):
        tracker_mod.add_link("glow", "https://glow-clinic.example.evil.com/", "spoof")


def test_tracker_weekly_message_and_silence():
    msg = tracker_mod.weekly_message(STATS, {"totals": {"clicks": 30}})
    assert "클릭 40" in msg and "지난주 대비 +10" in msg and "AI 검색 답변에서 넘어온 클릭 10건" in msg
    empty = {**STATS, "totals": {"clicks": 0, "unique_daily": 0, "bots": 0}, "by_source": [], "by_link": []}
    assert tracker_mod.weekly_message(empty, {"totals": {"clicks": 0}}) == ""


def test_tracker_sponsor_report_content():
    report = tracker_mod.sponsor_report(sponsors_mod.validate(SPONSOR), "2026-10", STATS)
    assert "글로우피부과의원" in report and "**40**" in report
    assert "| TikTok | 25 | 62% |" in report and "| AI 검색 답변 | 10 | 25% |" in report
    assert "utm_source" in report and "정액" in report


def test_tracker_report_silent_when_unconfigured(monkeypatch, capsys):
    monkeypatch.delenv("TRACKER_URL", raising=False)
    assert tracker_mod.main(["report"]) == 0
    assert capsys.readouterr().out == ""


def test_sponsored_draft_records_platform_policy(sponsored):
    draft_id = make_sponsored(sponsored)
    draft = common.load_draft(common.content_dir("drafts") / draft_id)
    assert "tiktok" in draft["platforms"]["blocked"] and "tiktok" not in draft["platforms"]["allowed"]
    assert "blog" in draft["platforms"]["allowed"]
    assert any("18" in r for r in draft["platforms"]["requires"]["instagram"])
    review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
    assert re.search(r"tiktok(, \w+)* 게시 불가", human_mod.list_message("drafts"))


def test_tracker_sponsor_link_is_blog_only(env, tmp_path, monkeypatch, capsys):
    write_sponsors(tmp_path, SPONSOR)
    monkeypatch.setattr(tracker_mod, "_request", lambda *a, **k: {"code": "abc234", "url": "https://go.example/abc234"})
    tracker_mod.main(["add", "--sponsor", "glow", "--label", "x"])
    out = capsys.readouterr().out
    assert "?s=blog" in out and "?s=tt" not in out and "?s=ig" not in out


# ---- 블로그 사이트 ----

site_mod = importlib.import_module("pipeline.site")
demand_mod = importlib.import_module("pipeline.demand")
FAQ_BLOG = (f"# Rejuran explained\n\nIt uses polynucleotides [F1]. Evil <script>alert(1)</script> and "
            f"[bad](javascript:alert(1)).\n\n## FAQ\n\n### Does it hurt\n\nMost people feel mild discomfort [F1].\n\n"
            f"### How long is downtime?\n\nUsually 1-3 days.\n\n## Next\n\nText.\n\n{DISCLOSURE}")


def _approve(env, draft_id):
    human_mod.list_message("drafts")
    ids = common.load_json(common.content_dir("drafts") / "_batch.json")["ids"]
    human_mod.apply(f"{ids.index(draft_id) + 1} 승인", confirm=True)


@pytest.fixture
def site_env(env, tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_SITE_DIR", str(tmp_path / "dist"))
    monkeypatch.delenv("TRACKER_URL", raising=False)
    monkeypatch.setattr(site_mod, "config", lambda: {**common.load_yaml("site.yaml"), "domain": "skinbound.example"})
    return env


def test_medical_facts_link_inline_travel_facts_use_footnotes():
    facts = [{"id": "F1", "url": "https://www.aad.org/a", "source_title": "AAD", "kind": "downtime"},
             {"id": "F2", "url": "https://english.visitkorea.or.kr/x", "source_title": "VisitKorea", "kind": "travel"},
             {"id": "F3", "url": "https://www.aad.org/a", "source_title": "AAD", "kind": "risk"}]
    html_out, sources = site_mod._numbered_sources("Redness lasts 1-3 days [F1]. The palace opens at 9 [F2]. "
                                                   "Bruising may occur [F3].", facts)
    assert html_out.count('href="https://www.aad.org/a"') == 2 and "[1 · aad.org]" in html_out  # 같은 출처는 같은 번호
    assert '<a href="#src-2">[2]</a>' in html_out  # 여행 정보는 글 아래 목록으로
    assert [s["url"] for s in sources] == ["https://www.aad.org/a", "https://english.visitkorea.or.kr/x"]


def test_site_publishes_only_approved_posts(site_env, tmp_path):
    site_env.responses["blog"] = blog(FAQ_BLOG)
    approved = make_draft(site_env)
    draft_mod.create("2026-W40", 2, TOPICS["topics"][1])  # 검수 대기 — 게시되면 안 됨
    review_mod.review(common.content_dir("drafts") / approved, fetch=ok_fetch)
    _approve(site_env, approved)
    result = site_mod.build()
    assert result["posts"] == 1
    dist = tmp_path / "dist"
    post = (dist / "rejuran" / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in post and "&lt;script&gt;" in post       # 원시 HTML 차단
    assert "javascript:" not in post
    # 의료 정보: 문장 옆 번호가 출처 페이지로 바로 연결 (도메인 표시) + 글 아래 출처 목록에도
    assert f'<a href="{URL}" rel="noopener" target="_blank" title="Source: PubMed">[1 · pubmed.ncbi.nlm.nih.gov]</a>' in post
    assert 'id="src-1"' in post
    assert post.count("<h1>") == 1
    ld = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', post)]
    assert ld[0]["citation"] == [URL] and "MedicalWebPage" in ld[0]["@type"]
    assert ld[1]["@type"] == "FAQPage" and ld[1]["mainEntity"][0]["name"] == "Does it hurt?"
    assert "https://skinbound.example/rejuran/" in (dist / "sitemap.xml").read_text(encoding="utf-8")
    assert "rejuran" in (dist / "llms.txt").read_text(encoding="utf-8")
    assert "Allow: /" in (dist / "robots.txt").read_text(encoding="utf-8")


def test_site_sponsored_post_labels_and_tracked_link(sponsored, tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_SITE_DIR", str(tmp_path / "dist"))
    monkeypatch.setattr(site_mod, "config", lambda: {**common.load_yaml("site.yaml"), "domain": ""})
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    monkeypatch.setattr(tracker_mod, "_request", lambda *a, **k: {"code": "abc234", "url": "https://go.example/abc234"})
    draft_id = make_sponsored(sponsored)
    review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
    human_mod.list_message("drafts")
    human_mod.apply("1 병원확인\n1 승인", confirm=True)
    site_mod.build()
    post = (tmp_path / "dist" / "rejuran" / "index.html").read_text(encoding="utf-8")  # blog.slug
    assert "Sponsored · Ad by Glow Skin Clinic" in post and "advertisement by Glow Skin Clinic" in post
    assert 'href="https://go.example/abc234?s=blog" rel="sponsored noopener"' in post
    assert '"sponsor": {"@type": "MedicalOrganization"' in post
    assert not (tmp_path / "dist" / "sitemap.xml").exists()                # 도메인 없으면 sitemap 생략
    assert "Sponsored" in (tmp_path / "dist" / "index.html").read_text(encoding="utf-8")


def test_site_deploy_only_when_changed(site_env, monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr(site_mod.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    approved = make_draft(site_env)
    review_mod.review(common.content_dir("drafts") / approved, fetch=ok_fetch)
    _approve(site_env, approved)
    msg = site_mod.deploy()
    assert "새 글: https://skinbound.example/rejuran/" in msg and len(calls) == 1
    assert "pages" in calls[0] and "deploy" in calls[0]
    assert site_mod.deploy() == "" and len(calls) == 1  # 바뀐 것 없음 → 배포·알림 없음


# ---- 초기 주제 목록 (seed) ----

def test_seed_topics_are_valid_and_compliant():
    seeds = common.load_yaml("seed_topics.yaml")["topics"]
    axes = common.load_yaml("channels.yaml")["content_axes"]
    banned = review_mod.load_banned()
    ids = [s["id"] for s in seeds]
    assert len(ids) == len(set(ids)) and len(seeds) >= 20
    for s in seeds:
        assert s["axis"] in axes and s["title"] and s["angle"] and s["hook"], s["id"]
        text = f"{s['title']}\n{s['hook']}\n{s['angle']}"
        hits = [term for term, pat in banned if pat.search(text)]
        assert not hits, (s["id"], hits)
        patterns = [label for kind, label, pat in review_mod.RULE_PATTERNS if pat.search(text)
                    and kind in ("clinic_name", "doctor_name", "phone", "booking", "credential", "testimonial")]
        assert not patterns, (s["id"], patterns)


def test_from_seed_order_and_reuse(env):
    week1 = topics_mod.from_seed("2026-W40", 3)
    ids = [t["seed_id"] for t in week1["topics"]]
    assert "rejuran-explained" in ids                                      # 관심도 5 + wave 1
    scores = [t["demand"]["score"] for t in week1["topics"]]
    assert scores == sorted(scores, reverse=True) and "관심도" in week1["topics"][0]["demand"]["reason"]
    assert "📈 수요" in topics_mod.telegram_message("2026-W40", week1["topics"])
    common.save_json(common.content_dir("topics") / "2026-W40.json", week1)
    rejuran = next(t for t in week1["topics"] if t["seed_id"] == "rejuran-explained")
    draft_mod.create("2026-W40", 1, rejuran)                                # rejuran 만 초안으로
    week2 = topics_mod.from_seed("2026-W41", 3)
    assert "rejuran-explained" not in [t["seed_id"] for t in week2["topics"]]
    common.save_json(common.content_dir("topics") / "2026-W41.json", week2)
    twice = set(ids[1:]) & {t["seed_id"] for t in week2["topics"]}
    week3 = topics_mod.from_seed("2026-W42", 5)                             # 두 번 제안하고 안 고른 주제는 제외
    assert twice and not twice & {t["seed_id"] for t in week3["topics"]}


def test_seed_week_limits_same_procedure():
    topics = [{"title": f"Rejuran {i}", "keywords": [], "axis": "procedure", "angle": "a"} for i in range(4)]
    topics.append({"title": "Botox basics", "keywords": [], "axis": "procedure", "angle": "a"})
    tables = demand_mod.all_scores()
    picked = topics_mod.pick(topics_mod.annotate(topics, tables), 3)
    assert sorted(t["title"] for t in picked) == ["Botox basics", "Rejuran 0", "Rejuran 1"]


def test_demand_blends_our_traffic(site_env, monkeypatch):
    approved = make_draft(site_env)                                        # rejuran 글 게시
    review_mod.review(common.content_dir("drafts") / approved, fetch=ok_fetch)
    _approve(site_env, approved)
    prior = demand_mod.scores()
    assert prior["rejuran"]["data_weight"] == 0 and prior["thermage_rf"]["score"] == 1.0
    assert prior["rejuran"]["score"] < 1.0                                 # 최근에 쓴 시술은 조금 낮춤
    monkeypatch.setattr(demand_mod, "page_views", lambda s, e: {"/rejuran/": 3000})
    data = demand_mod.scores()
    assert data["rejuran"]["views"] == 3000 and data["rejuran"]["data_weight"] == 0.7
    assert data["thermage_rf"]["data_weight"] == 0                         # 글 없는 시술은 사전값 유지
    assert "조회 3000" in demand_mod.reason(data["rejuran"])
    assert "Rejuran" in demand_mod.prompt_block({"procedures": data})


def test_topic_research_prompt_includes_demand(env):
    topics_mod.build("2026-W40", 3)
    assert "Audience demand by procedure" in env.calls[0][1] and "Thermage" in env.calls[0][1]


def test_sponsored_post_always_links_official_site(sponsored, tmp_path, monkeypatch):
    sponsored.responses["blog"] = blog(f"# Rejuran at Glow\n\nGlow Skin Clinic offers Rejuran [F1]. Results vary; consult a doctor.")
    draft_id = make_sponsored(sponsored)
    md = common.load_draft(common.content_dir("drafts") / draft_id)["blog"]["markdown"]
    assert md.rstrip().endswith("(https://www.glow-clinic.example)")      # 모델이 빠뜨려도 코드가 추가
    assert [f.message for f in review_mod.check_rules(common.load_draft(common.content_dir("drafts") / draft_id))[0]] == []
    monkeypatch.setenv("SKIN_SITE_DIR", str(tmp_path / "dist"))
    monkeypatch.setattr(site_mod, "config", lambda: {**common.load_yaml("site.yaml"), "domain": ""})
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    monkeypatch.setattr(tracker_mod, "_request", lambda *a, **k: {"code": "zz2345", "url": "https://go.example/zz2345"})
    review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
    human_mod.list_message("drafts")
    human_mod.apply("1 병원확인\n1 승인", confirm=True)
    site_mod.build()
    post = (tmp_path / "dist" / "rejuran" / "index.html").read_text(encoding="utf-8")
    assert 'href="https://go.example/zz2345?s=blog" rel="sponsored noopener"' in post


@pytest.mark.parametrize("sentence,blocked", [
    ("This cica cream heals acne scars.", True),
    ("It works like Botox in a jar.", True),
    ("A medical-grade serum for recovery.", True),
    ("Cosmetics can't legally claim to treat skin conditions.", False),
    ("Panthenol is described by makers as soothing and moisturizing.", False),
])
def test_cosmetic_claims_blocked_only_in_skincare(sentence, blocked):
    base = {"facts": [{"id": "F1", "text": "x", "url": URL}], "shortform": script(voice=sentence), "blog": blog()}
    msgs = [f.message for f in review_mod.check_rules({**base, "content_type": "skincare"})[0]]
    assert any("화장품법" in m for m in msgs) == blocked, msgs
    other = [f.message for f in review_mod.check_rules({**base, "content_type": "procedure"})[0]]
    assert not any("화장품법" in m for m in other)


def test_registry_box_in_neutral_posts_with_tracking(site_env, tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    made = []
    monkeypatch.setattr(tracker_mod, "_request",
                        lambda m, p, body=None, query=None: made.append(body) or {"code": "reg234", "url": "https://go.example/reg234"})
    approved = make_draft(site_env)
    review_mod.review(common.content_dir("drafts") / approved, fetch=ok_fetch)
    _approve(site_env, approved)
    site_mod.build()
    post = (tmp_path / "dist" / "rejuran" / "index.html").read_text(encoding="utf-8")
    assert "Looking for a clinic?" in post and 'href="https://go.example/reg234?s=blog"' in post
    assert made[0]["target_url"] == site_mod.REGISTRY_URL and made[0]["sponsor_id"] is None
    assert made[0]["label"].startswith("registry: ")                      # 글마다 따로 센다
    meta = site_mod.load_tracked_links()[site_mod.registry_key(approved)]
    assert meta["kind"] == "registry" and meta["code"] == "reg234" and meta["axis"] and meta["slug"] == "rejuran"
    site_mod.build()
    assert len(made) == 1  # 추적 링크는 한 번만 만든다


def test_tracked_links_reads_legacy_format(site_env):
    common.save_json(common.content_dir() / "site" / "tracked_links.json", {"_registry": "https://go.example/old?s=blog"})
    assert site_mod.load_tracked_links() == {"_registry": {"url": "https://go.example/old?s=blog"}}


def test_no_registry_box_in_sponsored_or_skincare(sponsored, tmp_path, monkeypatch):
    monkeypatch.setattr(site_mod, "config", lambda: {**common.load_yaml("site.yaml"), "domain": "", "analytics_token": "abc"})
    post = {"draft": {**common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored))}, "slug": "x", "date": "2026-10-01"}
    assert "Looking for a clinic?" not in site_mod.render_post(site_mod.config(), post)
    post["draft"] = {**post["draft"], "sponsor": None, "content_type": "skincare"}
    html_out = site_mod.render_post(site_mod.config(), post)
    assert "Looking for a clinic?" not in html_out
    assert "static.cloudflareinsights.com/beacon.min.js" in html_out and "&quot;token&quot;: &quot;abc&quot;" in html_out


# ---- 무상 파일럿 ----

PILOT = {**SPONSOR, "contract": {"type": "pilot", "start": "2026-01-01", "end": "2026-06-30"}}


def test_pilot_contract_is_short_and_labeled_as_ad(env, tmp_path):
    with pytest.raises(sponsors_mod.SponsorError, match="파일럿"):
        sponsors_mod.validate({**PILOT, "contract": {"type": "pilot", "start": "2026-01-01", "end": "2026-12-31"}})
    pilot = sponsors_mod.validate(PILOT)
    assert sponsors_mod.short_disclosure(pilot).startswith("Partner content with Glow Skin Clinic · Advertisement")
    assert "advertisement by Glow Skin Clinic (unpaid pilot" in sponsors_mod.blog_disclosure(pilot)
    assert "free of charge" in sponsors_mod.rules_text(pilot) and "flat fee" not in sponsors_mod.rules_text(pilot)


def test_pilot_draft_passes_sponsor_checks_and_report_marks_pilot(sponsored, tmp_path, monkeypatch):
    write_sponsors(tmp_path, {**PILOT, "contract": {"type": "pilot", "start": str(date.today()),
                                                     "end": str(date.today() + timedelta(days=90))}})
    draft = common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored))
    assert draft["blog"]["markdown"].startswith("> **Partner content — this is an advertisement by Glow Skin Clinic")
    assert [f.message for f in review_mod.check_rules(draft)[0]] == []
    draft["blog"]["markdown"] = draft["blog"]["markdown"].split("\n\n", 1)[1]
    assert any("광고 표시 없음" in f.message for f in review_mod.check_rules(draft)[0])
    post = site_mod.render_post({**common.load_yaml("site.yaml"), "domain": ""}, {"draft": draft, "slug": "x", "date": "2026-10-01"})
    assert "Partner · Ad by Glow Skin Clinic" in post
    data = {"from": "2026-10-01", "to": "2026-10-31", "totals": {"clicks": 0, "unique_daily": 0, "bots": 0},
            "by_source": [], "by_link": [], "by_country": [], "by_day": []}
    report = tracker_mod.sponsor_report(draft["sponsor"], "2026-10", data)
    assert "무상 파일럿" in report and "병원 사이트 도착" in report


# ---- 유입 분석 ----

analytics_mod = importlib.import_module("pipeline.analytics")


def test_procedure_tagging():
    procs = analytics_mod.load_procedures()
    assert analytics_mod.tag({"title": "What is Rejuran? PN skin boosters explained"}, procs)[:1] == ["rejuran"]
    assert "acne_scar" in analytics_mod.tag({"title": "Acne scars in Korea", "slug": "acne-scars"}, procs)
    assert analytics_mod.tag({"title": "Planning a Seoul trip", "keywords": ["k-eta"]}, procs) == ["travel"]
    assert analytics_mod.tag({"title": "Opinion"}, procs) == ["other"]
    assert "rejuran" not in analytics_mod.tag({"title": "Open hours"}, procs)  # pn 은 단어 단위


def test_analytics_report_joins_links_and_procedures(env, tmp_path, monkeypatch):
    links = {
        "_registry:2026-W40-01-rejuran": {"url": "u", "code": "reg111", "kind": "registry", "draft_id": "2026-W40-01-rejuran",
                                          "slug": "rejuran", "date": "2026-10-01", "title": "What is Rejuran?", "axis": "procedure",
                                          "keywords": ["rejuran"], "sponsor_id": None, "contract": None},
        "_registry:2026-W40-02-ulthera": {"url": "u", "code": "reg222", "kind": "registry", "slug": "ulthera", "date": "2026-10-01",
                                          "title": "Ultherapy basics", "axis": "procedure", "keywords": []},
        "sp-glow-1": {"url": "u", "code": "sp1111", "kind": "sponsor", "draft_id": "sp-glow-1", "slug": "glow-rejuran",
                      "date": "2026-10-05", "title": "Rejuran at Glow", "axis": "sponsored", "keywords": [],
                      "sponsor_id": "glow", "contract": "pilot"},
        "_registry:later": {"url": "u", "code": "reg999", "kind": "registry", "date": "2026-12-01", "title": "Later"},
    }
    common.save_json(common.content_dir() / "site" / "tracked_links.json", links)
    rows = [
        {"day": "2026-10-02", "code": "reg111", "sponsor_id": None, "label": "", "target_url": site_mod.REGISTRY_URL,
         "source": "ai", "country": "US", "is_bot": 0, "clicks": 4, "unique_visitors": 3},
        {"day": "2026-10-09", "code": "sp1111", "sponsor_id": "glow", "label": "", "target_url": "https://glow",
         "source": "blog", "country": "SG", "is_bot": 0, "clicks": 6, "unique_visitors": 5},
        {"day": "2026-10-09", "code": "sp1111", "sponsor_id": "glow", "label": "", "target_url": "https://glow",
         "source": "other", "country": "US", "is_bot": 1, "clicks": 50, "unique_visitors": 1},
        {"day": "2026-10-10", "code": "manual", "sponsor_id": None, "label": "tiktok bio", "target_url": "https://skinbound.example",
         "source": "tiktok", "country": "TH", "is_bot": 0, "clicks": 2, "unique_visitors": 2},
    ]
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    calls = []
    monkeypatch.setattr(tracker_mod, "_request", lambda m, p, body=None, query=None: calls.append((p, query)) or {"rows": rows})
    monkeypatch.setattr(analytics_mod, "reports_dir", lambda: tmp_path / "reports")
    assert analytics_mod.main(["report", "--month", "2026-10"]) == 0
    assert calls == [("/api/export", {"from": "2026-10-01", "to": "2026-10-31"})]
    md = (tmp_path / "reports" / "2026-10.md").read_text(encoding="utf-8")
    assert "| 의도: 등록기관 목록 클릭 | 4 |" in md and "| 도착: 파일럿 병원 사이트 | 6 |" in md
    assert "| 전체 클릭 (사람) | 12 |" in md                                  # 봇 50 제외
    assert "| Rejuran (PN skin booster) | 4 | 0 | 6 | 0 | 10 |" in md          # 중립 글 의도 + 파일럿 도착
    assert "| 의도(등록기관 목록) | 2 | 4 | 2.0 |" in md                          # 12월 글은 분모에서 제외, 클릭 0 인 글은 포함
    import csv
    data = list(csv.DictReader((tmp_path / "reports" / "2026-10.csv").open(encoding="utf-8")))
    assert len(data) == 3 and {r["kind"] for r in data} == {"registry", "pilot", "other"} and "attractions" in data[0]
    assert "## 관광지·지역별" in md
    assert "visitor" not in data[0] and data[0]["week"] == "2026-W40"


def test_analytics_silent_without_tracker(monkeypatch, capsys):
    monkeypatch.delenv("TRACKER_URL", raising=False)
    assert analytics_mod.main(["report"]) == 0 and capsys.readouterr().out == ""


# ---- 관광지 (소개·코스) ----

attractions_mod = importlib.import_module("pipeline.attractions")


def test_observation_day_places():
    catalog = attractions_mod.load()
    assert catalog["coex"]["observation_ok"] and catalog["gwangjang_market"]["observation_ok"]
    for aid in ("jjimjilbang", "gyeongbokgung", "k_hiking", "nami_day_trip", "jeju"):
        assert not catalog[aid]["observation_ok"], aid
    axes = common.load_yaml("channels.yaml")["content_axes"]
    assert "travel_guide" not in axes and "procedure_travel" in axes  # 관광지만 다루는 글은 하지 않는다 (2026-10-06)
    assert "travel_guide" in site_mod.REGISTRY_AXES  # 이미 게시된 글의 등록기관 안내는 유지


def test_travel_context_only_for_travel_axes(env):
    topic = {"title": "Seongsu-dong and Seoul Forest", "axis": "travel_guide", "keywords": ["seongsu"]}
    block = attractions_mod.context(topic)
    assert [ln for ln in block.split("\n") if ln.startswith("- ")][0].startswith("- Seongsu-dong & Seoul Forest")  # 주제에 걸린 곳이 맨 앞
    assert "OK for the observation day" in block and "visitkorea" in block and "half-day course" in block
    assert attractions_mod.context({"title": "Rejuran", "axis": "procedure"}) == ""
    draft_mod.create("2026-W40", 1, {**TOPICS["topics"][0], **topic})
    prompts = {stage: p for stage, p in env.calls}
    for stage in ("sources", "write_search"):
        assert "TRAVEL PLANNING DATA" in prompts[stage], stage
    assert "Place guides and routes" in prompts["write_search"]


def test_procedure_travel_itinerary_needs_observation_day():
    base = {"facts": [{"id": "F1", "text": "x", "url": URL}], "shortform": script(), "content_type": "procedure_travel"}
    plan = "# Trip\n\nDay 1: treatment [F1]. Day 2: Gyeongbokgung. Results vary; ask a licensed doctor.\n\n" + DISCLOSURE
    msgs = [f.message for f in review_mod.check_rules({**base, "blog": blog(plan)})[0]]
    assert any("관찰일" in m for m in msgs)
    ok = plan.replace("Day 2: Gyeongbokgung.", "Day 2: an observation day at COEX.")
    assert not any("관찰일" in f.message for f in review_mod.check_rules({**base, "blog": blog(ok)})[0])


def test_travel_topics_compete_on_attraction_demand(env):
    tables = demand_mod.all_scores()
    score, focus, why = demand_mod.topic_score({"title": "Seongsu-dong and Seoul Forest: a half-day guide"}, tables)
    assert focus == "attractions:seongsu" and score >= 1.0 and "Seongsu" in why
    assert demand_mod.topic_score({"title": "Your first consultation"}, tables)[1] == "procedures:other"
    assert "Places & areas:" in demand_mod.prompt_block(tables)
    week = topics_mod.from_seed("2026-W40", 6, tables)
    assert not any(t["axis"] == "travel_guide" for t in week["topics"])  # 관광 전용 주제는 후보에서 빠진다
    assert any(t["axis"] == "procedure_travel" for t in week["topics"])  # 관광지 수요는 시술 × 여행으로


def test_weekly_topics_follow_axis_quota(env):
    week = topics_mod.from_seed("2026-W40", 6)
    axes = Counter(t["axis"] for t in week["topics"])
    assert len(week["topics"]) == 6 and axes["procedure_travel"] == 2 and sum(axes.values()) - axes["procedure_travel"] == 4
    scores = [t["demand"]["score"] for t in week["topics"]]
    assert scores == sorted(scores, reverse=True)  # 메시지는 수요 점수 순


def test_quota_fills_from_other_groups_when_short():
    topics = [{"axis": "procedure", "title": f"p{i}", "demand": {"score": 1 - i / 10, "focus": f"procedures:p{i}"}}
              for i in range(6)] + [{"axis": "procedure_travel", "title": "t", "demand": {"score": 0.1, "focus": "x:t"}}]
    picked = topics_mod.pick(topics, 6)
    assert [t["title"] for t in picked] == ["p0", "p1", "p2", "p3", "p4", "t"]  # 여행 1개뿐 → 시술로 채움
    assert topics_mod.quota(12) == [("info", topics_mod.quota(6)[0][1], 8), ("procedure_travel", ["procedure_travel"], 4)]


# ---- 관광지 트렌드 ----

trends_mod = importlib.import_module("pipeline.trends")
TREND_SCAN = {
    "attractions": [
        {"id": "ddp", "trend": 90, "why": "Viral light show", "sources": ["https://english.visitseoul.net/x"]},
        {"id": "hongdae", "trend": 80, "why": "no source", "sources": []},
        {"id": "unknown-place", "trend": 99, "why": "x", "sources": ["https://a.example"]},
    ],
    "emerging": [
        {"name": "Seoul Autumn Pop-up", "area": "Seongsu", "trend": 85, "why": "Pop-up trending on TikTok",
         "sources": ["https://news.example/popup"], "until": "2099-12-31",
         "topic": {"title": "The autumn pop-up everyone visits in Seongsu", "angle": "What it is and how to visit",
                   "keywords": ["seongsu pop-up"], "hook": "Seen this pop-up?"}},
        {"name": "Ended Fest", "area": "Seoul", "trend": 95, "why": "over", "sources": ["https://news.example/f"],
         "until": "2020-01-01", "topic": {"title": "t", "angle": "a"}},
    ],
}


def test_trend_scan_keeps_only_sourced_current_items(env):
    env.responses["trends"] = TREND_SCAN
    data = trends_mod.scan("2026-W40")
    assert set(data["attractions"]) == {"ddp"}                              # 출처 없음·모르는 id 제외
    assert [e["name"] for e in data["emerging"]] == ["Seoul Autumn Pop-up"]  # 끝난 행사 제외
    assert env.calls[0][0] == "trends" and "Known places" in env.calls[0][1] and "- ddp:" in env.calls[0][1]
    assert trends_mod.latest()["week"] == "2026-W40"
    assert trends_mod.ensure_fresh()["week"] == "2026-W40" and len(env.calls) == 1   # 최근 스캔 있으면 다시 안 함


def test_trend_and_season_raise_attraction_scores(env):
    today = date(2026, 9, 28)
    before = demand_mod.scores(today, "attractions")
    assert before["ddp"]["trend"] == 0
    assert before["gyeongbokgung"]["season"] and not before["jjimjilbang"]["season"]   # 9~10월 궁 시즌, 찜질방 겨울
    assert before["jjimjilbang"]["score"] < before["gyeongbokgung"]["score"]
    winter = demand_mod.scores(date(2026, 12, 10), "attractions")
    assert winter["jjimjilbang"]["season"] and winter["jjimjilbang"]["score"] > before["jjimjilbang"]["score"]
    env.responses["trends"] = TREND_SCAN
    trends_mod.scan("2026-W40")
    after = demand_mod.scores(date.today(), "attractions")
    assert after["ddp"]["trend"] == 90 and after["ddp"]["score"] > before["ddp"]["score"]
    assert "🔥 트렌드 90" in demand_mod.reason(after["ddp"])
    block = demand_mod.prompt_block(demand_mod.all_scores())
    assert "Trending right now" in block and "Seoul Autumn Pop-up" in block


def test_trending_places_are_not_topic_candidates(env):
    """관광지만 다루는 글은 하지 않으므로 트렌드 장소·맛집은 주제 후보가 아니라 수요 정보로만 쓴다."""
    env.responses["trends"] = FOOD_SCAN
    trends_mod.scan("2026-W40")
    week = topics_mod.from_seed("2026-W40", 40)
    assert not any(t["seed_id"].startswith("trend-") for t in week["topics"])
    assert "Seoul Autumn Pop-up" in demand_mod.prompt_block(demand_mod.all_scores())  # 주제 조사 프롬프트에는 들어감


def test_seed_wave_bonus_follows_the_topic_after_filtering(env):
    week = topics_mod.from_seed("2026-W40", 40)
    seeds = {s["id"]: s for s in common.load_yaml("seed_topics.yaml")["topics"]}
    tables = demand_mod.all_scores()
    for t in week["topics"]:
        base = demand_mod.topic_score(t, tables)[0]
        expected = base + topics_mod.WAVE_BONUS.get(seeds[t["seed_id"]].get("wave", 9), 0)
        assert t["demand"]["score"] == round(expected, 3), t["seed_id"]


def test_old_trend_scan_is_ignored(env):
    env.responses["trends"] = TREND_SCAN
    trends_mod.scan("2026-W40")
    assert trends_mod.latest(date.today() + timedelta(days=30)) is None


# ---- 맛집·카페 트렌드 / 병원 중심 코스 (스폰서) ----

FOOD_SCAN = {**TREND_SCAN, "food": [
    {"name": "Seongsu salt-bread cafes", "kind": "cafe_street", "area": "Seongsu", "attraction": "seongsu", "trend": 80,
     "why": "Viral on Instagram", "sources": ["https://news.example/saltbread"],
     "topic": {"title": "Seongsu's salt-bread cafe trend", "angle": "What it is and where the cafe streets are",
               "keywords": ["seongsu cafe"], "hook": "Why the lines in Seongsu?"}},
    {"name": "Dubai chewy cookie", "kind": "dessert", "area": "Seoul", "attraction": "", "trend": 70,
     "why": "Dessert trend", "sources": ["https://news.example/cookie"], "topic": None},
    {"name": "No source bistro", "kind": "restaurant", "area": "Hongdae", "attraction": "hongdae", "trend": 99, "sources": []},
]}


def test_food_trends_feed_places_prompts_and_topics(env):
    env.responses["trends"] = FOOD_SCAN
    data = trends_mod.scan("2026-W40")
    assert [f["name"] for f in data["food"]] == ["Seongsu salt-bread cafes", "Dubai chewy cookie"]   # 출처 없음 제외
    places = trends_mod.place_trends(data)
    assert places["seongsu"]["trend"] == 60 and "salt-bread" in places["seongsu"]["why"]            # 80 × 0.75
    assert demand_mod.scores(catalog_name="attractions")["seongsu"]["trend"] == 60
    block = attractions_mod.context({"title": "Seongsu-dong guide", "axis": "travel_guide", "keywords": ["seongsu"]})
    assert "TRENDING FOOD & CAFES" in block and "Seongsu salt-bread cafes" in block and "Dubai chewy cookie" in block
    assert "https://news.example/saltbread" in block
    assert "🍜 Seongsu salt-bread cafes" in trends_mod.summary(data)
    assert "unpaid editorial example" in common.read_prompt("_rules")


def test_sponsored_course_post_uses_clinic_zone(sponsored, tmp_path):
    write_sponsors(tmp_path, {**SPONSOR, "zone": "gangnam", "area": "Sinsa, Gangnam-gu"})
    draft_id = draft_mod.create_sponsored("glow", "3 days in Seoul around Glow Skin Clinic", "a route", course=True)
    blog_prompt = [p for stage, p in sponsored.calls if stage.startswith("write")][-1]
    assert "TRAVEL PLANNING DATA" in blog_prompt and "visitors treated at Glow Skin Clinic in Sinsa, Gangnam-gu" in blog_prompt
    assert "no perks, pickups, discounts" in blog_prompt
    first = [ln for ln in blog_prompt.split("\n") if ln.startswith("- ")]
    assert "COEX" in "".join(first[:4])                                     # 같은 권역(강남) 관광지 먼저
    assert common.load_draft(common.content_dir("drafts") / draft_id)["topic"]["course"] is True
    sponsored.calls.clear()
    draft_mod.create_sponsored("glow", "Rejuran at Glow Skin Clinic", "what to expect")
    assert all("TRAVEL PLANNING DATA" not in p for _, p in sponsored.calls)   # 일반 스폰서 글은 관광 데이터 없음


def test_sponsored_course_requires_zone(sponsored):
    with pytest.raises(sponsors_mod.SponsorError, match="zone"):
        draft_mod.create_sponsored("glow", "t", "a", course=True)
    with pytest.raises(ValueError, match="zone"):
        worker_mod.request_sponsored("glow", "t", "a", course=True)
    with pytest.raises(sponsors_mod.SponsorError, match="zone"):
        sponsors_mod.validate({**SPONSOR, "zone": "mars"})


# ---- 코스 주변 피부과 목록 (심평원 공공데이터) ----

clinics_mod = importlib.import_module("pipeline.clinics")


def _hira_item(name, lat, lng, url=""):
    return {"yadmNm": name, "clCdNm": "의원", "addr": f"서울 강남구 {name}", "sgguCdNm": "강남구", "emdongNm": "삼성동",
            "XPos": str(lng), "YPos": str(lat), "hospUrl": url}


def test_hira_fetch_reads_all_pages_sorted_by_distance(monkeypatch):
    pages = {1: {"totalCount": 600, "items": {"item": [_hira_item("먼피부과의원", 37.5150, 127.0595),
                                                        _hira_item("가까운피부과의원", 37.5117, 127.0596)]}},
             2: {"totalCount": 600, "items": {"item": _hira_item("중간피부과의원", 37.5130, 127.0595)}}}  # 1건이면 dict
    calls = []
    monkeypatch.setattr(clinics_mod, "_get", lambda params: calls.append(params) or pages[params["pageNo"]])
    rows = clinics_mod.fetch_near(37.5116, 127.0595, 700)
    assert [c["name"] for c in rows] == ["가까운피부과의원", "중간피부과의원", "먼피부과의원"]
    assert len(calls) == 2 and calls[0]["dgsbjtCd"] == "14" and calls[0]["radius"] == 700
    assert calls[0]["xPos"].startswith("127.0595") and calls[0]["yPos"].startswith("37.5116")
    assert rows[0]["district"] == "강남구 삼성동" and rows[0]["distance_m"] < rows[1]["distance_m"]


def _clinic_cache(env_tmp, clinics_list):
    common.save_json(common.content_dir() / "clinics" / "coex.json",
                     {"attraction": "coex", "fetched_at": "2026-09-28T00:00:00+00:00", "radius_m": 700, "source": "HIRA",
                      "clinics": clinics_list})


def _travel_post(env):
    env.responses["blog"] = blog("# Indoor Gangnam\n\nCOEX and Starfield Library are indoors [F1]. Results vary; ask a licensed doctor.\n\n"
                                 + DISCLOSURE)
    topic = {**TOPICS["topics"][0], "title": "Indoor Gangnam: COEX", "axis": "travel_guide", "keywords": ["coex"]}
    draft_id = draft_mod.create("2026-W40", 1, topic)
    return {"draft": common.load_draft(common.content_dir("drafts") / draft_id), "slug": "x", "date": "2026-10-01"}


def test_route_post_lists_every_nearby_clinic_with_advertiser_label(sponsored, tmp_path):
    write_sponsors(tmp_path, SPONSOR)
    near = [{"name": "가까운피부과의원", "type": "의원", "addr": "서울 강남구 A", "district": "강남구 삼성동", "url": "",
             "lat": 0, "lng": 0, "distance_m": 80},
            {"name": "글로우피부과의원", "type": "의원", "addr": "서울 강남구 B", "district": "강남구 삼성동",
             "url": "http://www.glow-clinic.example", "lat": 0, "lng": 0, "distance_m": 150},
            {"name": "먼피부과의원", "type": "병원", "addr": "서울 강남구 C", "district": "강남구 대치동",
             "url": "https://far.example", "lat": 0, "lng": 0, "distance_m": 690}]
    _clinic_cache(tmp_path, near)
    post = _travel_post(sponsored)
    cfg = {**common.load_yaml("site.yaml"), "domain": ""}
    out = site_mod.render_post(cfg, post)
    assert "Dermatology clinics near this route" in out and "all 3 clinics" in out
    assert out.index("가까운피부과의원") < out.index("글로우피부과의원") < out.index("먼피부과의원")   # 거리순 그대로
    assert out.count("Advertiser</span>") == 1 and "글로우피부과의원 <span class=\"badge\">Advertiser" in out
    # 사이트 주소가 있는 병원은 전부 똑같이 링크 (광고주도 같은 모양) — 고르지 않는다
    assert out.count(">website</a>") == 2 and 'href="https://far.example"' in out and 'href="http://www.glow-clinic.example"' in out
    assert "google.com/maps/search" in out and "Website links are the addresses listed in the same public data" in out
    orig = clinics_mod.settings
    clinics_mod.settings = lambda: {**orig(), "max_per_stop": 2}  # 표시 수를 줄여도 가까운 순으로 자르고 밝힌다
    try:
        assert "the 2 closest of 3" in site_mod.render_post(cfg, post)
    finally:
        clinics_mod.settings = orig


@pytest.mark.parametrize("raw,expected", [
    ("www.a-clinic.co.kr", "http://www.a-clinic.co.kr"), ("https://b.example/", "https://b.example/"),
    ("", ""), ("javascript:alert(1)", ""), ("not a url", ""),
])
def test_clinic_website_normalized(raw, expected):
    assert clinics_mod.website({"url": raw}) == expected


def test_clinic_list_links_tracked_and_reported_only_in_aggregate(env, tmp_path, monkeypatch):
    _clinic_cache(tmp_path, [
        {"name": "가까운피부과의원", "type": "의원", "addr": "A", "district": "강남구 삼성동", "url": "https://near.example",
         "lat": 0, "lng": 0, "distance_m": 80},
        {"name": "먼피부과의원", "type": "의원", "addr": "B", "district": "강남구 대치동", "url": "www.far.example",
         "lat": 0, "lng": 0, "distance_m": 600}])
    post = _travel_post(env)
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    made = []

    def fake_request(method, path, body=None, query=None):
        made.append(body)
        return {"code": f"c{len(made)}", "url": f"https://go.example/c{len(made)}"}

    monkeypatch.setattr(tracker_mod, "_request", fake_request)
    links = site_mod.tracked_links([post])
    clinic = {k: v for k, v in links.items() if k.startswith("_clinic:")}
    assert [v["clinic"] for v in clinic.values()] == ["가까운피부과의원"]  # https 사이트만 추적 (http 는 바로 연결)
    assert all(v["kind"] == "clinic" and v["attraction"] == "coex" for v in clinic.values())
    html_out = site_mod.render_post({**common.load_yaml("site.yaml"), "domain": ""}, post,
                                    clinic_urls={k.rsplit(":", 1)[1]: v["url"] for k, v in clinic.items()})
    assert 'href="https://go.example/c' in html_out and 'href="http://www.far.example"' in html_out
    code = next(iter(clinic.values()))["code"]
    rows = analytics_mod.enrich([{"day": "2026-10-05", "code": code, "source": "blog", "country": "US", "is_bot": 0,
                                  "clicks": 7, "unique_visitors": 5}], links, analytics_mod.load_catalog("procedures"))
    assert rows[0]["kind"] == "clinic" and rows[0]["attractions"] == "coex"
    assert "가까운피부과의원" not in json.dumps(rows, ensure_ascii=False)  # 리포트·CSV 행에 병원명 없음 (합산만)
    result = analytics_mod.analyze(rows, links, analytics_mod.load_catalog("procedures"), date(2026, 10, 31))
    assert result["kinds"]["clinic"] == 7 and result["posts"]["clinic"] == 1  # 병원 수가 아니라 글 수로 센다


def test_no_clinic_list_in_sponsored_or_non_travel_posts(sponsored, tmp_path):
    _clinic_cache(tmp_path, [{"name": "가까운피부과의원", "type": "의원", "addr": "a", "district": "d", "url": "",
                              "lat": 0, "lng": 0, "distance_m": 80}])
    cfg = {**common.load_yaml("site.yaml"), "domain": ""}
    sp = {"draft": common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored)), "slug": "s", "date": "2026-10-01"}
    sp["draft"]["blog"]["markdown"] += " Near COEX."
    assert "near this route" not in site_mod.render_post(cfg, sp)
    neutral = {"draft": {**sp["draft"], "sponsor": None, "content_type": "procedure"}, "slug": "n", "date": "2026-10-01"}
    assert "near this route" not in site_mod.render_post(cfg, neutral)
    assert "Do not name or recommend any clinic" in attractions_mod.context({"title": "COEX", "axis": "travel_guide"})


def test_clinic_refresh_needs_key_and_skips_fresh_cache(env, monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    assert clinics_mod.main(["refresh"]) == 0                        # 키 없으면 조용히
    monkeypatch.setenv("DATA_GO_KR_KEY", "k")
    calls = []
    monkeypatch.setattr(clinics_mod, "fetch_near", lambda lat, lng, r: calls.append((lat, lng)) or [])
    done = clinics_mod.refresh()
    assert "coex" in done and "jeju" not in done and len(calls) == len(done)
    assert clinics_mod.refresh() == {}                                # 30일 안이면 다시 안 부름


# ---- 코스 광고 카드 (중립 여행 글, 조용한 광고) ----

ROUTE_AD = {**SPONSOR, "zone": "gangnam", "area": "Sinsa, Gangnam-gu",
            "route_ad": {"tagline": "English-speaking dermatology clinic in Sinsa", "confirmed": "2026-09-01"}}


def test_route_ad_card_is_small_labeled_and_geo_hidden(sponsored, tmp_path, monkeypatch):
    write_sponsors(tmp_path, ROUTE_AD)
    post = _travel_post(sponsored)                                             # COEX = gangnam
    cfg = {**common.load_yaml("site.yaml"), "domain": ""}
    out = site_mod.render_post(cfg, post, ad_url="https://go.example/ad1?s=blog")
    card = out[out.index('<aside class="adcard"'):out.index("</aside>", out.index('<aside class="adcard"'))]
    assert "data-geo-ad hidden" in card and "Ad · Sponsored" in card and "Glow Skin Clinic" in card
    assert 'href="https://go.example/ad1?s=blog" rel="sponsored noopener"' in card and "Not a recommendation" in card
    assert "/cdn-cgi/trace" in out and '["KR"]' in out
    assert out.index('<aside class="adcard"') > out.index("Starfield Library are indoors")   # 본문 뒤
    other_zone = {**post, "draft": {**post["draft"], "blog": {**post["draft"]["blog"], "title": "Hongdae",
                                                                 "markdown": "Hongdae walk [F1]."}}}
    assert '<aside class="adcard"' not in site_mod.render_post(cfg, other_zone)       # 권역이 다르면 없음
    sp = {"draft": common.load_draft(common.content_dir("drafts") / make_sponsored(sponsored)), "slug": "s", "date": "2026-10-01"}
    assert site_mod.route_ad_sponsor(sp) is None                                       # 스폰서 글에는 없음


def test_route_ad_needs_confirmation_and_clean_tagline():
    with pytest.raises(sponsors_mod.SponsorError, match="confirmed"):
        sponsors_mod.validate({**ROUTE_AD, "route_ad": {"tagline": "x"}})
    with pytest.raises(sponsors_mod.SponsorError, match="금지 표현"):
        sponsors_mod.validate({**ROUTE_AD, "route_ad": {"tagline": "The best clinic near COEX", "confirmed": "2026-09-01"}})
    with pytest.raises(sponsors_mod.SponsorError, match="zone"):
        sponsors_mod.validate({k: v for k, v in ROUTE_AD.items() if k != "zone"})
    v = sponsors_mod.validate({**ROUTE_AD, "route_ad": {"tagline": "ok", "confirmed": date(2026, 9, 1)}})
    assert v["route_ad"]["confirmed"] == "2026-09-01"                                  # JSON 저장 가능


def test_route_ad_tracked_link_and_expired_contract(site_env, tmp_path, monkeypatch):
    write_sponsors(tmp_path, ROUTE_AD)
    monkeypatch.setenv("SKIN_SPONSORS_FILE", str(tmp_path / "sponsors.yaml"))
    monkeypatch.setenv("TRACKER_URL", "https://go.example")
    monkeypatch.setenv("TRACKER_TOKEN", "x")
    made = []

    def fake(m, p, body=None, query=None):
        if p == "/api/export":
            return {"rows": []}
        made.append(body)
        return {"code": f"c{len(made)}", "url": f"https://go.example/c{len(made)}"}
    monkeypatch.setattr(tracker_mod, "_request", fake)
    post = _travel_post(site_env)
    links = site_mod.tracked_links([post])
    key = site_mod.route_ad_key(post["draft"]["id"], "glow")
    assert links[key]["placement"] == "route_ad" and links[key]["sponsor_id"] == "glow"
    assert any(b["sponsor_id"] == "glow" and b["label"].startswith("route ad:") for b in made)
    assert analytics_mod.published_counts(links, date(2099, 1, 1))["sponsor"] == 0    # 광고 자리는 글 수에 안 셈
    write_sponsors(tmp_path, {**ROUTE_AD, "contract": {"type": "monthly", "start": "2020-01-01", "end": "2020-12-31"}})
    assert site_mod.route_ad_sponsor(post) is None                                     # 계약 끝나면 자동으로 빠짐


def test_route_ad_one_advertiser_per_post_and_assignment_is_stable(sponsored, tmp_path):
    write_sponsors(tmp_path, ROUTE_AD)
    post = _travel_post(sponsored)
    assert site_mod.route_ad_sponsor(post)["id"] == "glow"
    shine = {**ROUTE_AD, "id": "shine", "name_en": "Shine Skin Clinic", "name_ko": "샤인피부과의원",
             "official_url": "https://www.shine.example"}
    write_sponsors(tmp_path, ROUTE_AD, shine)
    assert site_mod.route_ad_sponsor(post)["id"] == "glow"                  # 새 광고주가 와도 기존 글은 그대로
    other = {**post, "draft": {**post["draft"], "id": "2026-W40-09-coex-2"}}
    assert site_mod.route_ad_sponsor(other)["id"] == "shine"                # 새 글은 배정 적은 광고주에게
    html_out = site_mod.render_post({**common.load_yaml("site.yaml"), "domain": ""}, post)
    assert html_out.count('<aside class="adcard"') == 1                     # 글 하나에 광고 카드 하나
    write_sponsors(tmp_path, shine)                                         # glow 계약 종료 → 그 글은 다음 광고주로
    assert site_mod.route_ad_sponsor(post)["id"] == "shine"


def test_route_ad_zone_exclusive(sponsored, tmp_path):
    exclusive = {**ROUTE_AD, "route_ad": {**ROUTE_AD["route_ad"], "exclusive": True}}
    shine = {**ROUTE_AD, "id": "shine", "name_en": "Shine Skin Clinic", "name_ko": "샤인피부과의원",
             "official_url": "https://www.shine.example"}
    write_sponsors(tmp_path, exclusive, shine)
    with pytest.raises(sponsors_mod.SponsorError, match="독점"):
        sponsors_mod.load()
    later = {**shine, "contract": {"type": "monthly", "start": "2100-01-01", "end": "2100-12-31"}}
    write_sponsors(tmp_path, exclusive, later)                              # 기간이 안 겹치면 가능
    assert set(sponsors_mod.load()) == {"glow", "shine"}
    post = _travel_post(sponsored)
    assert site_mod.route_ad_sponsor(post)["id"] == "glow"


def test_trend_scan_survives_malformed_model_output(env):
    env.responses["trends"] = {
        "attractions": ["ddp", {"id": ["x"], "trend": 50, "sources": "https://a.example"},
                       {"id": "ddp", "trend": "90", "why": "ok", "sources": "https://s.example"}],
        "emerging": [{"name": "Pop-up", "sources": ["https://p.example"], "topic": "just a string"},
                     {"name": "Fest", "trend": 70, "sources": ["https://f.example"], "until": "not-a-date",
                      "topic": {"title": "Fest guide", "angle": "a", "keywords": "x"}}],
        "food": [{"name": "Cookie", "kind": ["dessert"], "attraction": ["seongsu"], "trend": 60, "sources": ["https://c.example"]}],
    }
    data = trends_mod.scan("2026-W40")
    assert data["attractions"]["ddp"]["trend"] == 90 and data["attractions"]["ddp"]["sources"] == ["https://s.example"]
    assert [e["name"] for e in data["emerging"]] == ["Fest"] and data["emerging"][0]["topic"]["keywords"] == []
    assert data["food"][0]["kind"] == "dish" and data["food"][0]["attraction"] == ""


# ---- 04 영상 렌더링 ----

render_mod = importlib.import_module("pipeline.04_render_video")


def test_ass_and_srt_timings():
    timeline = [{"start": 0, "end": 2.5, "voice": "Hello there.", "caption": "Hello {bold}"},
                {"start": 2.75, "end": 61.2, "voice": "Second.", "caption": "Second"}]
    ass = render_mod.build_ass(timeline, 61.45, "Sponsored by Glow Skin Clinic · Advertisement · AI-generated content", "skinboundkorea.com")
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Caption,,0,0,0,,Hello (bold)" in ass            # ASS 태그 무력화
    assert "0:01:01.20" in ass and ",Disclosure,,0,0,0,,Sponsored by Glow Skin Clinic" in ass
    srt = render_mod.build_srt(timeline)
    assert "00:00:02,750 --> 00:01:01,200\nSecond." in srt


def test_render_moves_to_rendered_with_outputs(env, monkeypatch):
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(render_mod, "duration", lambda path: 2.0)
    cmds = []
    monkeypatch.setattr(render_mod, "_run", lambda cmd: cmds.append(cmd) or "")

    def fake_run_in(cwd, cmd):
        cmds.append(cmd)
        (cwd / "video.mp4").write_bytes(b"mp4")
        return ""
    monkeypatch.setattr(render_mod, "_run_in", fake_run_in)
    render_mod.set_tts(lambda text, voice: b"mp3")
    try:
        draft_id = make_draft(env)
        review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
        _approve(env, draft_id)
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert render_mod.main([]) == 0
    finally:
        render_mod.set_tts(None)
    out = common.content_dir("rendered") / draft_id
    assert (out / "video.mp4").exists() and (out / "captions.srt").exists() and not (out / "render").exists()
    meta = common.load_json(out / "render.json")
    assert meta["timeline"][0]["start"] == 0 and meta["voice"] == common.load_yaml("voice.yaml")["voice"]
    assert "AI-generated content" in (out / "captions.ass").read_text(encoding="utf-8")
    assert any("ass=captions.ass" in c for c in cmds[-1]) and "1080x1920" in " ".join(cmds[-1])
    assert common.load_json(out / "history.json")[-1] == {**common.load_json(out / "history.json")[-1], "from": "approved", "to": "rendered"}
    assert not list(common.draft_dirs("approved"))


@pytest.mark.parametrize("audio,mime,rate,fmt,tempo", [
    (b"RIFF....WAVEfmt ", "audio/wav", 1.0, False, False),                 # WAV 는 헤더대로
    (b"\x00\x01" * 10, "audio/l16; rate=24000; channels=1", 1.0, True, False),  # 원시 PCM 은 형식을 알려 준다
    (b"\x00\x01" * 10, "audio/l16; rate=24000", 1.1, True, True),          # 속도 조절은 atempo
])
def test_gemini_tts_converts_to_mp3(monkeypatch, audio, mime, rate, fmt, tempo):
    seen = {}
    monkeypatch.setattr(llm, "speech", lambda stage, text, voice: seen.update(stage=stage, voice=voice) or (audio, mime))

    def fake_run(cmd):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"ID3mp3")
        return ""

    monkeypatch.setattr(render_mod, "_run", fake_run)
    assert render_mod._gemini_tts("Hello.", {"provider": "gemini", "voice": "Kore", "speaking_rate": rate}) == b"ID3mp3"
    assert seen["stage"] == "tts" and seen["voice"] == "Kore"
    assert ("s16le" in seen["cmd"]) == fmt and ("24000" in seen["cmd"]) == fmt
    assert any(c.startswith("atempo=") for c in seen["cmd"]) == tempo


def test_tts_uses_gemini_key_and_kore_voice(monkeypatch):
    voice = common.load_yaml("voice.yaml")
    assert voice["provider"] == "gemini" and voice["voice"] == "Kore"
    assert llm.stage_config("tts")["provider"] == "gemini" and llm.stage_config("tts")["audio"]
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GOOGLE_TTS_API_KEY", raising=False)
    assert render_mod.tts_configured()  # 별도 TTS 키 없이 Gemini 키로


def test_render_is_silent_without_tts_key(env, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert render_mod.main([]) == 0 and capsys.readouterr().out == ""


# ---- 06 게시 키트 ----

publish_mod = importlib.import_module("pipeline.06_publish")


def _ready(draft_id, sponsor=None):
    draft = {"id": draft_id, "content_type": "sponsored" if sponsor else "procedure",
             "blog": {"title": "What is Rejuran?", "slug": "what-is-rejuran", "markdown": "x"},
             "shortform": {"title": "What is Rejuran? 45s", "hook": "Salmon DNA on your face?", "hashtags": ["#rejuran", "kbeauty"],
                           "lines": [], "on_screen_disclosure": "AI-generated content"}}
    if sponsor:
        draft["sponsor"] = sponsor
    path = common.content_dir("ready_to_publish") / draft_id
    common.save_json(path / "draft.json", draft)
    return path


def test_publish_kits_and_done_flow(env):
    _ready("2026-W40-01-what-is-rejuran")
    msg = publish_mod.main(["kits"])
    kit = common.load_json(common.content_dir("ready_to_publish") / "2026-W40-01-what-is-rejuran" / "kit.json")
    assert set(kit["channels"]) == {"tiktok", "instagram", "youtube"} and kit["skipped"] == {}
    assert "link in bio" in kit["channels"]["tiktok"]["caption"] and "#kbeauty" in kit["channels"]["tiktok"]["caption"]
    assert "https://skinboundkorea.com/what-is-rejuran/" in kit["channels"]["youtube"]["caption"]
    assert "#ad" not in kit["channels"]["tiktok"]["caption"] and len(kit["channels"]["youtube"]["title"]) <= 100
    assert any("AI" in c for c in kit["channels"]["tiktok"]["checklist"])
    with pytest.raises(ValueError, match="게시물 주소"):
        publish_mod.done("2026-W40-01-what-is-rejuran", "tiktok", "https://evil.example/tiktok.com")
    assert "남은 채널" in publish_mod.done("2026-W40-01-what-is-rejuran", "tiktok", "https://www.tiktok.com/@skinboundkorea/video/1")
    publish_mod.done("2026-W40-01-what-is-rejuran", "instagram", "https://www.instagram.com/reel/abc/")
    assert "published" in publish_mod.done("2026-W40-01-what-is-rejuran", "youtube", "https://youtube.com/shorts/xyz")
    assert (common.content_dir("published") / "2026-W40-01-what-is-rejuran" / "publish_log.json").exists()


def test_sponsored_kit_skips_tiktok_and_lists_platform_settings(env, tmp_path):
    _ready("sp-glow-1", sponsors_mod.validate(SPONSOR))
    kit = publish_mod.kit(common.content_dir("ready_to_publish") / "sp-glow-1")
    assert "tiktok" in kit["skipped"] and set(kit["channels"]) == {"instagram", "youtube"}
    assert kit["channels"]["instagram"]["caption"].startswith("#ad Sponsored by Glow Skin Clinic")
    assert any("브랜디드" in c for c in kit["channels"]["instagram"]["checklist"])
    with pytest.raises(ValueError, match="게시 채널이 아닙니다"):
        publish_mod.done("sp-glow-1", "tiktok", "https://www.tiktok.com/@x/video/1")


def test_publish_only_touches_ready_to_publish(env):
    path = common.content_dir("approved") / "x"
    common.save_json(path / "draft.json", {"id": "x"})
    with pytest.raises(ValueError, match="ready_to_publish"):
        publish_mod.kit(path)
    assert publish_mod.kits_message() == "" and publish_mod.status_message() == "게시 대기 없음"


# ---- 07 주간 리포트 ----

report_mod = importlib.import_module("pipeline.07_report")


def test_weekly_report_counts_pipeline_activity(env, monkeypatch, tmp_path):
    approved = make_draft(env)
    review_mod.review(common.content_dir("drafts") / approved, fetch=ok_fetch)
    _approve(env, approved)
    _ready("2026-W40-09-posted")
    publish_mod.kit(common.content_dir("ready_to_publish") / "2026-W40-09-posted")
    publish_mod.done("2026-W40-09-posted", "tiktok", "https://www.tiktok.com/@s/video/1")
    data = report_mod.collect(7)
    assert data["created"] >= 1 and data["moves"]["approved"] == 1 and data["posts"]["tiktok"] == 1
    assert data["waiting"]["ready_to_publish"] == 1
    monkeypatch.setattr(report_mod, "ROOT", tmp_path)
    monkeypatch.setattr(report_mod, "extras", lambda: ["🌐 블로그 글 1개 (스폰서 0)"])
    assert report_mod.main([]) == 0
    text = next((tmp_path / "reports" / "weekly").glob("*.md")).read_text(encoding="utf-8")
    assert "[주간 리포트" in text and "승인 1" in text and "tiktok 1" in text and "게시 키트 1" in text


def test_site_share_images_and_icons(sponsored, tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.setenv("SKIN_SITE_DIR", str(tmp_path / "dist"))
    monkeypatch.setattr(site_mod, "config", lambda: {**common.load_yaml("site.yaml"), "domain": "skinbound.example"})
    draft_id = make_sponsored(sponsored)
    review_mod.review(common.content_dir("drafts") / draft_id, fetch=ok_fetch)
    human_mod.list_message("drafts")
    human_mod.apply("1 병원확인\n1 승인", confirm=True)
    site_mod.build()
    dist = tmp_path / "dist"
    assert (dist / "og" / "rejuran.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and (dist / "og" / "default.png").exists()
    assert (dist / "favicon.svg").exists() and (dist / "apple-touch-icon.png").exists()
    post = (dist / "rejuran" / "index.html").read_text(encoding="utf-8")
    assert '<meta property="og:image" content="https://skinbound.example/og/rejuran.png">' in post
    assert 'twitter:card" content="summary_large_image"' in post and 'rel="icon" href="/favicon.svg"' in post
    first = (dist / "og" / "rejuran.png").read_bytes()
    site_mod.build()
    assert (dist / "og" / "rejuran.png").read_bytes() == first          # 같은 입력 → 같은 이미지 (변경 없으면 배포 안 함)


def test_pdf_text_extraction_handles_broken_files():
    assert web.pdf_to_text(b"%PDF-1.4 not really a pdf") == ""  # 손상·스캔 PDF → 빈 본문 (사람 확인)


# ---- X·Threads 소개 글 ----

social_mod = importlib.import_module("pipeline.social")
SOCIAL = {"x": "Salmon DNA on your face? Rejuran is a polynucleotide skin booster. Results vary; ask a licensed doctor.",
          "threads": "Rejuran is a polynucleotide skin booster popular in Korea. Downtime and results vary from person "
                     "to person, so ask a licensed doctor. What would you want to know before trying it?",
          "fact_ids": ["F1"]}


def _published(draft_id="2026-W40-01-what-is-rejuran", sponsor=None, state="approved"):
    draft = {"id": draft_id, "content_type": "procedure", "facts": [{"id": "F1", "text": "Downtime is 1-3 days.", "url": URL}],
             "blog": {"title": "What is Rejuran?", "slug": "what-is-rejuran", "markdown": "x"},
             "shortform": {"hook": "Salmon DNA on your face?", "lines": []}}
    if sponsor:
        draft["sponsor"] = sponsor
    path = common.content_dir(state) / draft_id
    common.save_json(path / "draft.json", draft)
    return path


def test_social_compose_list_and_manual_record(env):
    env.responses["social"] = SOCIAL
    _published()
    assert social_mod.main(["compose"]) == 0
    data = common.load_json(common.content_dir("approved") / "2026-W40-01-what-is-rejuran" / "social.json")
    assert data["status"] == "ready" and data["problems"] == []
    assert data["posts"]["x"]["text"].endswith("AI-assisted guide with sources: https://skinboundkorea.com/what-is-rejuran/")
    assert social_mod.x_length(data["posts"]["x"]["text"]) <= 280
    msg = social_mod.list_message()
    assert "https://x.com/intent/post?text=Salmon%20DNA" in msg and "threads.net/intent/post?text=" in msg  # 키 없으면 작성 링크
    with pytest.raises(social_mod.SocialError, match="게시물 주소"):
        social_mod.record(social_mod.resolve("1"), "x", "https://evil.example/x.com")
    assert social_mod.main(["done", "1", "x", "https://x.com/skinboundkorea/status/123"]) == 0
    assert "x ✅ https://x.com/skinboundkorea/status/123" in social_mod.list_message()
    assert social_mod.pending_paths() == []                                     # 한 번만 만든다


@pytest.mark.parametrize("text,problem", [
    ("Visit Glow Skin Clinic for Rejuran.", "병원명"),
    ("Rejuran costs 300 dollars.", "사실 목록에 없는 수치"),
    ("Downtime is 1-3 days. See https://example.com", "링크"),
    ("The best skin booster in Korea.", "금지 표현"),
    ("Rejuran explained. " * 30, "너무 김"),
])
def test_social_check_blocks_rule_violations(text, problem):
    draft = {"id": "d", "facts": [{"id": "F1", "text": "Downtime is 1-3 days."}], "blog": {"title": "t", "slug": "t"}}
    problems = social_mod.check(draft, {"x": text, "threads": "Downtime is 1-3 days. Ask a licensed doctor."})
    assert any(problem in p for p in problems), problems
    assert social_mod.check(draft, {"x": "Downtime is 1-3 days; ask a licensed doctor.", "threads": "ok"}) == []


def test_social_blocked_after_retry_and_sponsored_skipped(env, tmp_path):
    env.responses["social"] = {**SOCIAL, "x": "The best clinic trick."}
    path = _published()
    data = social_mod.compose(path)
    assert data["status"] == "blocked" and len([c for c in env.calls if c[0] == "social"]) == 2   # 1번 다시 생성
    assert "⛔" in social_mod.list_message()
    with pytest.raises(social_mod.SocialError, match="검사"):
        social_mod.post(path, "threads")
    _published("sp-glow-1", sponsor=sponsors_mod.validate(SPONSOR))
    assert [p.name for p in social_mod.pending_paths()] == []                    # 스폰서 글: 정책 확인 전 제외


def test_threads_api_post_and_token_refresh(env, tmp_path, monkeypatch):
    env.responses["social"] = SOCIAL
    path = _published()
    social_mod.compose(path)
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "old")
    monkeypatch.setenv("THREADS_TOKEN_FILE", str(tmp_path / "tok" / "threads.json"))
    calls = []

    def fake_http(method, url, params=None, headers=None, body=None):
        calls.append((method, url, dict(params or {})))
        if "refresh_access_token" in url:
            return {"access_token": "new", "expires_in": 5184000}
        if url.endswith("/me"):
            return {"id": "42", "username": "skinboundkorea"}
        if url.endswith("/42/threads"):
            return {"id": "c1"}
        if url.endswith("/threads_publish"):
            return {"id": "p1"}
        return {"permalink": "https://www.threads.net/@skinboundkorea/post/abc"}
    monkeypatch.setattr(social_mod, "_http", fake_http)
    out = social_mod.post(path, "threads")
    assert "threads 게시 기록" in out
    create = next(c for c in calls if c[1].endswith("/42/threads"))
    assert create[2]["media_type"] == "TEXT" and create[2]["access_token"] == "new" and "AI-assisted" in create[2]["text"]
    saved = tmp_path / "tok" / "threads.json"
    assert json.loads(saved.read_text())["access_token"] == "new" and oct(saved.stat().st_mode)[-3:] == "600"
    calls.clear()
    social_mod.threads_token()
    assert not any("refresh" in c[1] for c in calls)                          # 50일 안이면 다시 갱신하지 않음
    assert "이미 게시됨" in social_mod.post(path, "threads")


def test_x_api_is_optional_and_signed(env, monkeypatch):
    with pytest.raises(social_mod.SocialError, match="유료"):
        social_mod.x_post("hi")
    for k in social_mod.X_KEYS:
        monkeypatch.setenv(k, "k-" + k.lower())
    header = social_mod._oauth1_header("POST", social_mod.X_API)
    assert header.startswith("OAuth ") and 'oauth_signature_method="HMAC-SHA1"' in header and "oauth_signature=" in header
    sent = {}
    monkeypatch.setattr(social_mod, "_http", lambda m, u, params=None, headers=None, body=None:
                        sent.update(body=json.loads(body), auth=headers["authorization"]) or {"data": {"id": "99"}})
    assert social_mod.x_post("hello") == "https://x.com/i/web/status/99" and sent["body"] == {"text": "hello"}
