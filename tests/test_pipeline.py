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


_real_stage_config = llm.stage_config


def _api_mode_stage_config(stage):
    """검수 단계를 Anthropic API 모드로 시험 (설정 기본값은 claude_code 외부 검수)."""
    cfg = _real_stage_config(stage)
    if cfg["provider"] == llm.EXTERNAL:
        cfg = {"provider": "anthropic", "model": "claude-test"}
    return cfg


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("SKIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(llm, "stage_config", _api_mode_stage_config)
    monkeypatch.setenv("SKIN_SPONSORS_FILE", str(tmp_path / "sponsors.yaml"))  # 기본: 스폰서 없음
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
    assert llm.stage_config("research")["provider"] == "gemini"
    assert llm.is_external("cross_review") and llm.is_external("source_check")


def test_generation_and_review_use_different_vendors():
    assert llm.stage_config("cross_review")["provider"] != llm.stage_config("blog")["provider"]
    assert llm.stage_config("source_check")["provider"] != llm.stage_config("research")["provider"]


def test_external_stage_is_never_called_as_api():
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
    monkeypatch.setattr(llm, "stage_config", _real_stage_config)  # 검수 단계 = claude_code
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
    monkeypatch.setattr(llm, "stage_config", _real_stage_config)
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
