"""2. 초안 생성 — Hermes (주제 선택 직후, 에이전트 모드).

흐름: 사실 수집(검색 연동, 사실마다 출처 URL) → 숏폼 대본 → 블로그 글 → 법규 1차 점검.
대본·블로그는 수집된 사실 목록만 사용하고 각 문장에 사실 id 를 단다 (02b 출처 검증의 기준).

    python -m pipeline.02_draft --week 2026-W40 --pick 1,3,4
    python -m pipeline.02_draft --revise 2026-W40-01-what-is-rejuran --note "가격 출처 다시"
    python -m pipeline.02_draft --sponsor example-clinic --title "Rejuran at ..." --angle "..." [--keywords a,b]

스폰서 글(기획서 12-1-1): 광고주 병원 규칙(_sponsored_rules.md)으로 생성하고 광고 표시를 자동으로 넣는다.

결과: content/drafts/<draft_id>/{draft.json, script.md, blog.md}. 생성된 draft_id 를 stdout 에 한 줄씩 출력.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

from pipeline import attractions, llm, sponsors
from pipeline.common import (
    blog_text,
    content_dir,
    facts_text,
    find_draft,
    get_logger,
    iso_week,
    load_draft,
    load_json,
    load_yaml,
    now_iso,
    prompt,
    run_cli,
    save_json,
    script_text,
    slugify,
)

log = get_logger("02_draft")
FACT_REF = re.compile(r"\[(F\d+)\]")


def _revision(note: str | None) -> str:
    return f"\nREVISION REQUEST from the human reviewer (must be addressed): {note}\n" if note else ""


def research(topic: dict, note: str | None, rules: str | None = None, travel: str = "") -> list[dict]:
    text = prompt(
        "fact_research",
        rules=rules,
        today=date.today().isoformat(),
        title=topic["title"],
        axis=topic["axis"],
        angle=topic["angle"],
        keywords=", ".join(topic.get("keywords", [])),
        revision_note=_revision(note),
        travel_context=travel,
    )
    data, _ = llm.generate_json("research", text)
    facts = []
    for i, f in enumerate(data.get("facts", []) if isinstance(data, dict) else [], 1):
        url = str(f.get("url", "")).strip()
        if not f.get("text") or not url.startswith("http"):
            log.warning("출처 없는 사실 제외: %s", f.get("text", "")[:80])
            continue
        facts.append({"id": f"F{i}", "text": f["text"].strip(), "url": url,
                      "source_title": f.get("source_title", ""), "kind": f.get("kind", "other")})
    if len(facts) < 5:
        raise llm.LLMError(f"출처 있는 사실이 부족합니다 ({len(facts)}건)")
    # 모델이 준 id 대신 순번을 쓰므로 이후 단계는 이 목록 기준
    return facts


def write_script(topic: dict, facts: list[dict], note: str | None, rules: str | None = None, travel: str = "") -> dict:
    lo, hi = load_yaml("channels.yaml")["shortform"]["duration_sec"]
    text = prompt(
        "shortform_script",
        rules=rules,
        duration=f"{lo}-{hi}",
        title=topic["title"],
        angle=topic["angle"],
        hook=topic.get("hook", ""),
        facts=facts_text(facts),
        revision_note=_revision(note),
        travel_context=travel,
    )
    data, _ = llm.generate_json("shortform", text)
    if not data.get("lines"):
        raise llm.LLMError("대본에 lines 가 없습니다")
    return data


def write_blog(topic: dict, facts: list[dict], note: str | None, rules: str | None = None, travel: str = "") -> dict:
    lo, hi = load_yaml("channels.yaml")["blog"]["words"]
    text = prompt(
        "blog_post",
        rules=rules,
        min_words=str(lo),
        max_words=str(hi),
        title=topic["title"],
        angle=topic["angle"],
        keywords=", ".join(topic.get("keywords", [])),
        facts=facts_text(facts),
        revision_note=_revision(note),
        travel_context=travel,
    )
    data, _ = llm.generate_json("blog", text)
    if not data.get("markdown"):
        raise llm.LLMError("블로그 markdown 이 없습니다")
    return data


def precheck(draft: dict, rules: str | None = None) -> dict:
    text = prompt("compliance_check", rules=rules, script=script_text(draft), blog=blog_text(draft))
    data, result = llm.generate_json("compliance", text)
    return {"model": result.model, "issues": data.get("issues", []), "summary": data.get("summary", "")}


def used_fact_ids(draft: dict) -> set[str]:
    ids = {fid for line in draft["shortform"].get("lines", []) for fid in line.get("fact_ids", [])}
    return ids | set(FACT_REF.findall(draft["blog"].get("markdown", "")))


def add_disclosures(draft: dict, sponsor: dict) -> None:
    """광고 표시는 모델에 맡기지 않고 코드로 넣는다 (02b 가 다시 확인)."""
    draft["shortform"]["on_screen_disclosure"] = sponsors.short_disclosure(sponsor)
    notice = sponsors.blog_disclosure(sponsor)
    markdown = draft["blog"]["markdown"]
    if notice not in markdown:
        draft["blog"]["markdown"] = f"{notice}\n\n{markdown}"
    link = sponsors.official_link_line(sponsor)
    if link not in draft["blog"]["markdown"]:  # 모델이 링크를 빠뜨려도 병원 링크(→ 유입 추적)가 항상 있게
        draft["blog"]["markdown"] = draft["blog"]["markdown"].rstrip() + f"\n\n{link}\n"


def compose(topic: dict, note: str | None = None, sponsor: dict | None = None) -> dict:
    rules = sponsors.rules_text(sponsor) if sponsor else None
    if sponsor:  # 스폰서 "병원 중심 코스" 글만 관광 데이터를 받는다 (병원 권역 기준)
        travel = attractions.context(topic, home=sponsor) if topic.get("course") else ""
    else:  # 여행 축: 관광지 속성·코스 규칙·맛집 트렌드
        travel = attractions.context(topic)
    facts = research(topic, note, rules, travel)
    draft = {"topic": topic, "facts": facts}
    draft["shortform"] = write_script(topic, facts, note, rules, travel)
    draft["blog"] = write_blog(topic, facts, note, rules, travel)
    if sponsor:
        draft["sponsor"] = sponsor
        add_disclosures(draft, sponsor)
    known = {f["id"] for f in facts}
    unknown = used_fact_ids(draft) - known
    if unknown:
        log.warning("존재하지 않는 사실 id 참조: %s (02b 에서 차단됨)", sorted(unknown))
    draft["precheck"] = precheck(draft, rules)
    return draft


def write_files(path: Path, draft: dict) -> None:
    save_json(path / "draft.json", draft)
    (path / "script.md").write_text(script_text(draft) + "\n", encoding="utf-8")
    (path / "blog.md").write_text(blog_text(draft) + "\n\n## Sources\n" + "\n".join(
        f"- [{f['id']}] {f['source_title'] or f['url']}: {f['url']}" for f in draft["facts"]) + "\n", encoding="utf-8")
    (path / "review.json").unlink(missing_ok=True)  # 내용이 바뀌면 이전 검수 결과는 무효


def create(week: str, index: int, topic: dict) -> str:
    draft_id = f"{week}-{index:02d}-{slugify(topic['title'])}"
    path = content_dir("drafts") / draft_id
    if (path / "draft.json").exists():
        log.info("이미 있음, 건너뜀: %s", draft_id)
        return draft_id
    draft = compose(topic)
    draft.update({"id": draft_id, "week": week, "content_type": topic["axis"], "created_at": now_iso(),
                  "revisions": [], "regenerated": 0})
    write_files(path, draft)
    log.info("초안 생성: %s (사실 %d건)", draft_id, len(draft["facts"]))
    return draft_id


def create_sponsored(sponsor_id: str, title: str, angle: str, keywords: list[str] | None = None, course: bool = False) -> str:
    """스폰서 글 초안. 계약 기간이 아니면 만들지 않는다.
    course=True: 병원 권역 중심 여행 코스 글 (sponsors.yaml 의 zone·area 필요, 광고 표시는 동일)."""
    sponsor = sponsors.get(sponsor_id)
    if not sponsors.contract_active(sponsor):
        raise sponsors.SponsorError(f"{sponsor_id}: 계약 기간이 아닙니다 ({sponsor['contract']['start']} ~ {sponsor['contract']['end']})")
    if course and not sponsor.get("zone"):
        raise sponsors.SponsorError(f"{sponsor_id}: 코스 글에는 sponsors.yaml 의 zone(병원 권역)이 필요합니다")
    topic = {"title": title, "axis": "sponsored", "angle": angle, "keywords": keywords or [], "hook": "", "has_price": False,
             **({"course": True} if course else {})}
    draft_id = f"sp-{sponsor_id}-{date.today().strftime('%Y%m%d')}-{slugify(title, 30)}"
    path = content_dir("drafts") / draft_id
    if (path / "draft.json").exists():
        log.info("이미 있음, 건너뜀: %s", draft_id)
        return draft_id
    draft = compose(topic, sponsor=sponsor)
    draft.update({"id": draft_id, "week": iso_week(), "content_type": "sponsored", "created_at": now_iso(),
                  "revisions": [], "regenerated": 0})
    draft["platforms"] = sponsors.platform_policy()  # 06_publish 가 따를 채널 제한 (틱톡 불가 등)
    write_files(path, draft)
    log.info("스폰서 초안 생성: %s (%s)", draft_id, sponsor["name_en"])
    return draft_id


def revise(draft_id: str, note: str, auto: bool = False) -> str:
    """수정 요청 반영 재생성. drafts/ 에 있는 초안만 대상."""
    state, path = find_draft(draft_id)
    if state != "drafts":
        raise ValueError(f"{draft_id} 는 {state}/ 에 있어 수정할 수 없습니다 (drafts/ 만 가능)")
    old = load_draft(path)
    sponsor = sponsors.get(old["sponsor"]["id"]) if old.get("sponsor") else None  # 최신 계약·심의번호 반영
    draft = compose(old["topic"], note, sponsor)
    for key in ("id", "week", "content_type", "created_at"):
        draft[key] = old[key]
    if sponsor:
        draft["platforms"] = sponsors.platform_policy()
    draft["revisions"] = old.get("revisions", []) + [{"at": now_iso(), "note": note, "auto": auto}]
    draft["regenerated"] = old.get("regenerated", 0) + (1 if auto else 0)
    write_files(path, draft)
    log.info("재생성: %s (%s)", draft_id, "자동" if auto else "수정 요청")
    return draft_id


def parse_pick(pick: str, total: int) -> list[int]:
    nums = sorted({int(n) for n in re.findall(r"\d+", pick)})
    bad = [n for n in nums if not 1 <= n <= total]
    if not nums or bad:
        raise ValueError(f"잘못된 번호: {pick!r} (1~{total})")
    return nums


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="초안 생성")
    parser.add_argument("--week")
    parser.add_argument("--pick", help="주제 번호, 예: 1,3,4")
    parser.add_argument("--revise", metavar="DRAFT_ID")
    parser.add_argument("--note", help="수정 요청 내용 (--revise 와 함께)")
    parser.add_argument("--sponsor", metavar="SPONSOR_ID", help="스폰서 글 (config/sponsors.yaml 의 id)")
    parser.add_argument("--title", help="스폰서 글 제목 (--sponsor 와 함께)")
    parser.add_argument("--angle", help="스폰서 글에서 답할 질문 (--sponsor 와 함께)")
    parser.add_argument("--keywords", default="", help="쉼표로 구분")
    parser.add_argument("--course", action="store_true", help="스폰서 병원 권역 중심 여행 코스 글 (--sponsor 와 함께)")
    args = parser.parse_args(argv)

    if args.revise:
        if not args.note:
            parser.error("--revise 에는 --note 가 필요합니다")
        print(revise(args.revise, args.note))
        return 0
    if args.sponsor:
        if not (args.title and args.angle):
            parser.error("--sponsor 에는 --title 과 --angle 이 필요합니다")
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        print(create_sponsored(args.sponsor, args.title, args.angle, keywords, args.course))
        return 0
    if not (args.week and args.pick):
        parser.error("--week 와 --pick 이 필요합니다")

    topics = load_json(content_dir("topics") / f"{args.week}.json")["topics"]
    failed = 0
    for n in parse_pick(args.pick, len(topics)):
        try:
            print(create(args.week, n, topics[n - 1]))
        except llm.LLMError as e:  # 한 건 실패가 나머지를 막지 않게
            failed += 1
            log.error("주제 %d 실패: %s", n, e)
    return 1 if failed else 0


if __name__ == "__main__":
    run_cli("02_draft", main)
