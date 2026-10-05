"""2. 초안 생성 — Hermes 워커 (주제 선택 직후).

흐름 (모델 배치는 config/models.yaml):
  1. 출처 찾기 (stage sources, Gemini + Google 검색): 읽을 페이지 후보만 고른다 — 사실은 쓰지 않는다.
  2. 출처 확보 (코드): 실제 주소로 바꾸고 페이지를 받아 404·빈 페이지·병원 사이트를 뺀다.
  3. 작성 (stage write, Claude Code CLI): 받아 둔 페이지 본문만 보고 사실 목록 → 숏폼 대본 → 블로그.
     쓸 만한 페이지가 MIN_SOURCES 개보다 적으면 stage write_search (Claude 가 직접 추가 검색).
  4. 사실 확인 (코드): 사실마다 붙인 원문 인용이 그 페이지에 실제로 있는지 대조, 없으면 버린다.
대본·블로그는 사실 id 로 근거를 단다 (02b 출처 검증의 기준). 교차 검수는 작성과 다른 회사 모델(Gemini)이 한다.

    python -m pipeline.02_draft --week 2026-W40 --pick 1,3,4
    python -m pipeline.02_draft --revise 2026-W40-01-what-is-rejuran --note "가격 출처 다시"
    python -m pipeline.02_draft --sponsor example-clinic --title "Rejuran at ..." --angle "..." [--keywords a,b]

스폰서 글(기획서 12-1-1): 광고주 병원 규칙(_sponsored_rules.md)으로 생성하고 광고 표시를 자동으로 넣는다.

결과: content/drafts/<draft_id>/{draft.json, script.md, blog.md}. 생성된 draft_id 를 stdout 에 한 줄씩 출력.
"""

from __future__ import annotations

import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from pipeline import attractions, llm, sponsors, web
from pipeline.common import (
    blog_text,
    content_dir,
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
FACT_ID = re.compile(r"\bF\d+\b")

MAX_SOURCES = 12      # 작성 모델에 넘기는 페이지 수
MIN_SOURCES = 5       # 이보다 적으면 작성 모델이 직접 추가 검색 (stage write_search)
SOURCE_CHARS = 12000  # 페이지당 넘기는 본문 길이
MIN_PAGE_CHARS = 500  # 이보다 짧은 본문은 스크립트 렌더링·차단 페이지로 보고 뺀다
MIN_FACTS = 5


def _revision(note: str | None) -> str:
    return f"\nREVISION REQUEST from the human reviewer (must be addressed): {note}\n" if note else ""


# ---------------- 1·2. 출처 ----------------

def search_sources(topic: dict, note: str | None, rules: str | None = None, travel: str = "") -> list[dict]:
    """Gemini 검색으로 읽을 페이지 후보 [{url, title}] — 모델이 고른 목록 + 검색 연동이 실제로 연 페이지."""
    text = prompt(
        "source_search",
        rules=rules,
        today=date.today().isoformat(),
        title=topic["title"],
        axis=topic["axis"],
        angle=topic["angle"],
        keywords=", ".join(topic.get("keywords", [])),
        revision_note=_revision(note),
        travel_context=travel,
    )
    data, result = llm.generate_json("sources", text)
    picked = data.get("sources", []) if isinstance(data, dict) else []
    candidates = [{"url": str(s.get("url", "")).strip(), "title": s.get("title", "")} for s in picked if isinstance(s, dict)]
    return candidates + [{"url": g["url"], "title": g.get("title", "")} for g in result.grounding_urls]


def fetch_sources(candidates: list[dict], sponsor: dict | None = None) -> list[dict]:
    """후보를 실제 주소로 바꾸고 받아 와서 쓸 수 있는 페이지만 [{id, url, title, text}] 로 돌려준다."""
    sponsor_host = sponsors.official_host(sponsor) if sponsor else None
    if sponsor:  # 병원에 관한 사실(제공 시술·언어 등)은 광고주 공식 사이트만 출처로 쓸 수 있다
        candidates = [{"url": sponsor["official_url"], "title": sponsor["name_en"]}] + candidates
    with ThreadPoolExecutor(max_workers=8) as pool:
        urls = list(pool.map(lambda c: web.source_url(c["url"]) if c["url"].startswith("http") else None, candidates))
    seen, todo = set(), []
    for cand, url in zip(candidates, urls):
        if not url or url in seen:
            continue
        seen.add(url)
        if web.is_clinic_host(url) and web.host(url) != sponsor_host:
            log.info("병원 사이트로 보여 출처에서 제외: %s", url)
            continue
        todo.append({"url": url, "title": cand["title"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda s: web.get(s["url"]), todo))
    sources = []
    for s, (status, text) in zip(todo, pages):
        if status != 200 or len(text) < MIN_PAGE_CHARS:
            log.info("출처 제외 (%s, 본문 %d자): %s", status or "연결 오류", len(text), s["url"])
            continue
        sources.append({"id": f"S{len(sources) + 1}", **s, "text": text})
        if len(sources) >= MAX_SOURCES:
            break
    return sources


def sources_text(sources: list[dict]) -> str:
    return "\n\n".join(f"[{s['id']}] {s['title'] or s['url']}\nURL: {s['url']}\n<page>\n{s['text'][:SOURCE_CHARS]}\n</page>"
                       for s in sources)


# ---------------- 3. 작성 ----------------

def write_draft(topic: dict, sources: list[dict], note: str | None, rules: str | None = None,
                travel: str = "") -> tuple[dict, str]:
    channels = load_yaml("channels.yaml")
    lo, hi = channels["shortform"]["duration_sec"]
    words_lo, words_hi = channels["blog"]["words"]
    search = len(sources) < MIN_SOURCES
    text = prompt(
        "write_draft",
        rules=rules,
        title=topic["title"],
        angle=topic["angle"],
        hook=topic.get("hook", ""),
        keywords=", ".join(topic.get("keywords", [])),
        revision_note=_revision(note),
        travel_context=travel,
        duration=f"{lo}-{hi}",
        min_words=str(words_lo),
        max_words=str(words_hi),
        sources=sources_text(sources) or "(no usable pages were found)",
        search_note=(". There are few usable pages, so you may also use web search to find more pages from the "
                     "preferred source types in the rules (never clinic websites). For a fact from a page you found "
                     "yourself, give \"url\" (the exact page URL) instead of \"source\", and copy the quote from that page."
                     if search else "."),
    )
    data, result = llm.generate_json("write_search" if search else "write", text)
    if not isinstance(data, dict) or not (data.get("shortform") or {}).get("lines"):
        raise llm.LLMError("작성 결과에 대본(shortform.lines)이 없습니다")
    if not (data.get("blog") or {}).get("markdown"):
        raise llm.LLMError("작성 결과에 블로그 markdown 이 없습니다")
    for line in data["shortform"]["lines"]:  # 모델이 [["F1","F2"]] 나 "F1, F2" 로 줄 때가 있다
        line["fact_ids"] = _fact_ids(line.get("fact_ids"))
    # 제목은 title 로 따로 붙으므로 본문 맨 앞의 H1 은 뺀다 (제목 중복 방지)
    data["blog"]["markdown"] = re.sub(r"\A\s*#\s[^\n]*\n+", "", data["blog"]["markdown"])
    return data, result.model


def _fact_ids(value) -> list[str]:
    if isinstance(value, str):
        return FACT_ID.findall(value)
    if isinstance(value, (list, tuple)):
        return [fid for item in value for fid in _fact_ids(item)]
    return []


# ---------------- 4. 사실 확인 ----------------

def verify_facts(raw: list, sources: list[dict]) -> list[dict]:
    """원문 인용이 해당 페이지에 실제로 있는 사실만 남긴다. 작성 모델이 직접 찾은 페이지(url)는 받아 와서 확인한다.
    (버린 사실을 대본·블로그가 참조하면 02b 가 '존재하지 않는 사실 참조'로 차단한다)"""
    by_id = {s["id"]: s for s in sources}
    by_url = {s["url"]: s for s in sources}
    facts, seen = [], set()
    for f in raw if isinstance(raw, list) else []:
        fid = str(f.get("id", "")) if isinstance(f, dict) else ""
        if not re.fullmatch(r"F\d+", fid) or fid in seen or not f.get("text"):
            continue
        src = by_id.get(str(f.get("source", ""))) or by_url.get(str(f.get("url", "")))
        url = str(f.get("url", "")).strip()
        if src is None and url.startswith("http") and not web.is_clinic_host(url):
            status, page = web.get(url)
            if status == 200 and len(page) >= MIN_PAGE_CHARS:
                src = {"id": f"S{len(sources) + 1}", "url": url, "title": f.get("source_title", ""), "text": page}
                sources.append(src)
                by_url[url] = src
        if src is None:
            log.warning("출처 없는 사실 제외 [%s]: %s", fid, f["text"][:80])
            continue
        if not web.quote_in_page(str(f.get("quote", "")), src["text"]):
            log.warning("인용문이 페이지에 없어 제외 [%s] %s: %s", fid, src["url"], str(f.get("quote", ""))[:80])
            continue
        seen.add(fid)
        facts.append({"id": fid, "text": f["text"].strip(), "url": src["url"], "source_title": src["title"],
                      "quote": f["quote"].strip(), "kind": f.get("kind", "other")})
    return facts


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
    sources = fetch_sources(search_sources(topic, note, rules, travel), sponsor)
    log.info("출처 %d곳 확보: %s", len(sources), topic["title"])
    written, writer = write_draft(topic, sources, note, rules, travel)
    facts = verify_facts(written.get("facts"), sources)
    if len(facts) < MIN_FACTS:
        raise llm.LLMError(f"페이지로 확인된 사실이 부족합니다 ({len(facts)}건, 출처 {len(sources)}곳)")
    draft = {"topic": topic, "facts": facts, "shortform": written["shortform"], "blog": written["blog"],
             "writer": writer, "sources": [{k: s[k] for k in ("id", "url", "title")} for s in sources]}
    if sponsor:
        draft["sponsor"] = sponsor
        add_disclosures(draft, sponsor)
    unknown = used_fact_ids(draft) - {f["id"] for f in facts}
    if unknown:
        log.warning("확인되지 않은 사실 참조: %s (02b 에서 차단됨)", sorted(unknown))
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
    log.info("초안 생성: %s (사실 %d건, 출처 %d곳, %s)", draft_id, len(draft["facts"]), len(draft["sources"]), draft["writer"])
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
