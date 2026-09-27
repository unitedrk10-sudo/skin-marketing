"""2. 초안 생성 — Hermes (주제 선택 직후, 에이전트 모드).

흐름: 사실 수집(검색 연동, 사실마다 출처 URL) → 숏폼 대본 → 블로그 글 → 법규 1차 점검.
대본·블로그는 수집된 사실 목록만 사용하고 각 문장에 사실 id 를 단다 (02b 출처 검증의 기준).

    python -m pipeline.02_draft --week 2026-W40 --pick 1,3,4
    python -m pipeline.02_draft --revise 2026-W40-01-what-is-rejuran --note "가격 출처 다시"

결과: content/drafts/<draft_id>/{draft.json, script.md, blog.md}. 생성된 draft_id 를 stdout 에 한 줄씩 출력.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

from pipeline import llm
from pipeline.common import (
    blog_text,
    content_dir,
    facts_text,
    find_draft,
    get_logger,
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


def research(topic: dict, note: str | None) -> list[dict]:
    text = prompt(
        "fact_research",
        today=date.today().isoformat(),
        title=topic["title"],
        axis=topic["axis"],
        angle=topic["angle"],
        keywords=", ".join(topic.get("keywords", [])),
        revision_note=_revision(note),
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


def write_script(topic: dict, facts: list[dict], note: str | None) -> dict:
    lo, hi = load_yaml("channels.yaml")["shortform"]["duration_sec"]
    text = prompt(
        "shortform_script",
        duration=f"{lo}-{hi}",
        title=topic["title"],
        angle=topic["angle"],
        hook=topic.get("hook", ""),
        facts=facts_text(facts),
        revision_note=_revision(note),
    )
    data, _ = llm.generate_json("shortform", text)
    if not data.get("lines"):
        raise llm.LLMError("대본에 lines 가 없습니다")
    return data


def write_blog(topic: dict, facts: list[dict], note: str | None) -> dict:
    lo, hi = load_yaml("channels.yaml")["blog"]["words"]
    text = prompt(
        "blog_post",
        min_words=str(lo),
        max_words=str(hi),
        title=topic["title"],
        angle=topic["angle"],
        keywords=", ".join(topic.get("keywords", [])),
        facts=facts_text(facts),
        revision_note=_revision(note),
    )
    data, _ = llm.generate_json("blog", text)
    if not data.get("markdown"):
        raise llm.LLMError("블로그 markdown 이 없습니다")
    return data


def precheck(draft: dict) -> dict:
    text = prompt("compliance_check", script=script_text(draft), blog=blog_text(draft))
    data, result = llm.generate_json("compliance", text)
    return {"model": result.model, "issues": data.get("issues", []), "summary": data.get("summary", "")}


def used_fact_ids(draft: dict) -> set[str]:
    ids = {fid for line in draft["shortform"].get("lines", []) for fid in line.get("fact_ids", [])}
    return ids | set(FACT_REF.findall(draft["blog"].get("markdown", "")))


def compose(topic: dict, note: str | None = None) -> dict:
    facts = research(topic, note)
    draft = {"topic": topic, "facts": facts}
    draft["shortform"] = write_script(topic, facts, note)
    draft["blog"] = write_blog(topic, facts, note)
    known = {f["id"] for f in facts}
    unknown = used_fact_ids(draft) - known
    if unknown:
        log.warning("존재하지 않는 사실 id 참조: %s (02b 에서 차단됨)", sorted(unknown))
    draft["precheck"] = precheck(draft)
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


def revise(draft_id: str, note: str, auto: bool = False) -> str:
    """수정 요청 반영 재생성. drafts/ 에 있는 초안만 대상."""
    state, path = find_draft(draft_id)
    if state != "drafts":
        raise ValueError(f"{draft_id} 는 {state}/ 에 있어 수정할 수 없습니다 (drafts/ 만 가능)")
    old = load_draft(path)
    draft = compose(old["topic"], note)
    for key in ("id", "week", "content_type", "created_at"):
        draft[key] = old[key]
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
    args = parser.parse_args(argv)

    if args.revise:
        if not args.note:
            parser.error("--revise 에는 --note 가 필요합니다")
        print(revise(args.revise, args.note))
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
