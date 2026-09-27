"""01 → 02 → 02b → 03 흐름을 가짜 LLM·가짜 웹 응답으로 검증한다 (네트워크·API 키 불필요)."""

import importlib
import json

import pytest

from pipeline import common, llm

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
FACTS = {"facts": [
    {"id": "F1", "text": f"Polynucleotide injections fact {i}", "url": URL, "source_title": "PubMed", "kind": "mechanism"}
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
    def __init__(self):
        self.responses = {
            "topics": TOPICS, "research": FACTS, "shortform": script(), "blog": blog(),
            "compliance": {"issues": [], "summary": "ok"},
            "source_check": {"results": [{"id": "F1", "verdict": "supported", "evidence": "..."}]},
            "cross_review": {"findings": [], "overall": "fine"},
        }
        self.calls = []

    def __call__(self, stage, cfg, system, prompt):
        self.calls.append((stage, prompt))
        return llm.LLMResult(text=json.dumps(self.responses[stage]), model=f"fake-{cfg['model']}", stage=stage)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("SKIN_LOG_DIR", str(tmp_path / "logs"))
    fake = FakeLLM()
    llm.set_backend(fake)
    yield fake
    llm.set_backend(None)


def ok_fetch(url):
    return 200, "Polynucleotide injections fact " * 20


def make_draft(env, **overrides):
    common.save_json(common.content_dir("topics") / "2026-W40.json", TOPICS)
    env.responses.update(overrides)
    return draft_mod.create("2026-W40", 1, TOPICS["topics"][0])


# ---- 프롬프트 ----

@pytest.mark.parametrize("name", ["topic_research", "fact_research", "shortform_script", "blog_post",
                                  "compliance_check", "cross_review"])
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
    cfg = llm.stage_config("cross_review")
    assert cfg["provider"] == "anthropic"
    assert llm.stage_config("research")["provider"] == "gemini"


def test_generation_and_review_use_different_vendors():
    assert llm.stage_config("cross_review")["provider"] != llm.stage_config("blog")["provider"]
    assert llm.stage_config("source_check")["provider"] != llm.stage_config("research")["provider"]


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
    assert all(f["url"].startswith("http") for f in draft["facts"])  # 출처 없는 사실 제외
    assert (path / "script.md").exists() and (path / "blog.md").exists()
    assert [c[0] for c in env.calls] == ["research", "shortform", "blog", "compliance"]
    # 대본·블로그 프롬프트에는 사실 목록과 규칙이 들어간다
    shortform_prompt = env.calls[1][1]
    assert URL in shortform_prompt and "NON-NEGOTIABLE CONTENT RULES" in shortform_prompt


def test_revise_keeps_identity_and_records_note(env):
    draft_id = make_draft(env)
    draft_mod.revise(draft_id, "가격 출처 다시")
    draft = common.load_draft(common.content_dir("drafts") / draft_id)
    assert draft["id"] == draft_id and draft["revisions"][0]["note"] == "가격 출처 다시"
    assert "가격 출처 다시" in env.calls[-4][1]  # research 프롬프트에 수정 요청 반영


def test_parse_pick():
    assert draft_mod.parse_pick("1, 3", 5) == [1, 3]
    with pytest.raises(ValueError):
        draft_mod.parse_pick("7", 5)


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
    ("Written by a board-certified dermatologist.", "자격"),
    ("Message us at pf.kakao.com/_abc for a quote.", "예약"),
    ("See before and after photos.", "금지 표현"),
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
    rows = (tmp_path / "logs" / "review_agreement.csv").read_text().splitlines()
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


def test_invalid_reply_moves_nothing(env):
    first, _ = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    with pytest.raises(human_mod.ReplyError):
        human_mod.apply("1 승인\n9 폐기")  # 9번 없음 → 1번도 처리하지 않는다
    assert (common.content_dir("drafts") / first).exists()


def test_revise_regenerates_and_rereviews(env):
    first, _ = _two_reviewed_drafts(env)
    human_mod.list_message("drafts")
    calls = []
    out = human_mod.apply("1 수정: 톤 부드럽게",
                          revise_fn=lambda d, n: calls.append((d, n)),
                          review_fn=lambda p: {"grade": "pass"})
    assert calls == [(first, "톤 부드럽게")] and "재생성" in out[0]


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
