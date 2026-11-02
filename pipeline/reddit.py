"""Reddit 답변 준비 — 후보 찾기 → 초안 → 검수 → 붙여넣을 글 (2026-11-02, docs/ai-citation-plan.md 3단계).

게시는 사람이 직접 한다. Reddit API 는 사전 승인제이고 봇·AI 에이전트 게시도 그 대상이라 자동 게시·웹 자동 조작은 만들지 않는다.
댓글에는 X 같은 작성 링크도 없어서, 글 주소와 붙여넣을 본문을 메시지 두 개로 나눠 보낸다.

  - 후보: Gemini 검색(reddit_find). 병원 추천을 묻는 글·제외 서브레딧·오래된 글은 코드가 뺀다. 글 나이는 AI 가 말한 날짜(연도가 틀렸다)가
    아니라 글 번호로 어림한다. 내용은 AI 요약이라 사람이 열어서 확인한다
  - 초안: 규칙 기록이 있는 커뮤니티만. 없으면 Gemini 검색으로 찾아 기록(링크 없이), AI 글 금지로 나오면 사람이 확인하기 전까지 안 만든다. Claude(reddit_answer)가 게시된 중립 글의 사실 목록으로만 쓴다
  - 검수: 02b 금지 규칙(코드) → Gemini(reddit_review). 통과한 글만 붙여넣을 본문을 보낸다
  - 운영자 공개 문구는 코드가 항상 붙이고, 블로그 링크는 그 서브레딧에서 허용했을 때만 붙인다

    python -m pipeline.reddit find
    python -m pipeline.reddit add <주소> "질문 내용"
    python -m pipeline.reddit allow <서브레딧> [--links]
    python -m pipeline.reddit list
    python -m pipeline.reddit draft <N>
    python -m pipeline.reddit direct <N> "운영자 답변 방향"
    python -m pipeline.reddit show <N>
    python -m pipeline.reddit skip <N>
    python -m pipeline.reddit done <N> <댓글 주소>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
from datetime import date, datetime, timedelta

from pipeline import llm
from pipeline.common import content_dir, get_logger, load_draft, load_json, load_yaml, now_iso, prompt, run_cli, save_json

log = get_logger("reddit")

THREAD = re.compile(r"https://(?:www\.|old\.|new\.)?reddit\.com/(?:r|u|user)/([A-Za-z0-9_-]+)/(?:comments|s)/([a-z0-9]+)(?:/[^\s?#)\]]*)?", re.I)  # /s/ = 앱의 공유 링크
COMMENT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com")
CLINIC_ASK = re.compile(r"(which|what|best|good|recommend\w*|suggest\w*|any)\b[^.?!\n]{0,40}\b(clinics?|dermatologists?|doctors?|derms?|hospitals?)\b"
                        r"|\b(clinics?|dermatologists?|derms?)\b[^.?!\n]{0,20}\b(recommendations?|recs?|suggestions?)\b", re.I)
ID_ANCHOR, ID_ANCHOR_DATE = "1nm0000", date(2025, 9, 20)
ID_PER_DAY = (int("1nm0000", 36) - int("1cx0000", 36)) / 487  # 487일 = 2024-05-21 ~ 2025-09-20
DISCLOSURE = "Disclosure: I run Skinbound Korea, an information site about skin treatments in Korea. AI-assisted and human-checked. Not medical advice."


WRITE_TRIES = 2
RULE_TRIES = 2
# 운영자 방향으로 쓴 글에서도 시술·주사제 추천은 코드가 막는다. 의사가 아닌 운영자의 추천은 §56 광고 시비, 리투오 등은 광고 제한 쟁점 (2026-11-02)
TREATMENT = r"(?:botox|botulinum|fillers?|skin\s*boosters?|rejuran|juvelook|re2o|ecm|pdrn|exosomes?|thermage|ulthera|laser|injectables?|리투오|리쥬란|필러|보톡스)"
PROMOTES_TREATMENT = re.compile(rf"\b(?:recommend|suggest|go for|get|try|choose|pick|book|worth)\b[^.!?\n]{{0,50}}\b{TREATMENT}\b"
                                rf"|\b{TREATMENT}\b[^.!?\n]{{0,40}}\b(?:is|are)\s+(?:worth|great|amazing|the best)\b|리투오|Re2O", re.I)


class RedditError(RuntimeError):
    pass


def settings() -> dict:
    return load_yaml("reddit.yaml")


def _file():
    return content_dir() / "reddit" / "state.json"


def load() -> dict:
    return load_json(_file()) if _file().exists() else {"subs": {}, "items": []}


def save(state: dict) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    save_json(_file(), state)


def _item(state: dict, n: int) -> dict:
    for item in state["items"]:
        if item["n"] == n:
            return item
    raise RedditError(f"{n}번 후보가 없습니다")


def _add(state: dict, url: str, title: str, question: str, posted: str = "") -> dict | None:
    m = THREAD.match(url or "")
    if not m or any(i["thread"] == m.group(2).lower() for i in state["items"]):
        return None
    sub = m.group(1)
    if sub.lower() in {s.lower() for s in settings().get("excluded", [])} or CLINIC_ASK.search(f"{title}\n{question}"):
        return None
    item = {"n": max((i["n"] for i in state["items"]), default=0) + 1, "thread": m.group(2).lower(), "url": m.group(0), "sub": sub,
            "title": title, "question": question, "posted": posted, "status": "candidate", "at": now_iso()}
    state["items"].append(item)
    return item


def estimated_date(thread: str) -> date:
    """Reddit 글 번호(36진수)는 시간 순서로 커진다 — 어림값. 기준점·속도는 2024-05(1cx…)~2025-09(1nm…) 글에서 잡았다 (2026-11-02)."""
    return ID_ANCHOR_DATE + timedelta(days=(int(thread, 36) - int(ID_ANCHOR, 36)) / ID_PER_DAY)


def too_old(thread: str) -> bool:
    return estimated_date(thread) < date.today() - timedelta(days=int(settings().get("max_age_days", 180)))


def _live_posts() -> list[dict]:
    """답변 근거: 블로그에 실제로 게시된 중립 글 (스폰서 제외, 병원 공통 정보 제외)."""
    social = importlib.import_module("pipeline.social")
    site = importlib.import_module("pipeline.site")
    live = social.live_slugs()
    out = []
    for post in site.collect_posts():
        draft = post["draft"]
        if draft["id"] in live and not draft.get("sponsor"):
            out.append({"key": post["slug"], "title": draft["blog"].get("title", ""), "url": social.blog_url(draft, live),
                        "facts": social.social_facts(load_draft(post["path"]))})
    return out


def find() -> str:
    cfg = settings()
    state = load()
    posts = _live_posts()
    data, _ = llm.generate_json("reddit_find", prompt("reddit_find", today=date.today().isoformat(), year=str(date.today().year), count=str(cfg.get("find_count", 8)),
                                                      topics="\n".join(f"- {p['title']}" for p in posts) or "(none)",
                                                      excluded=", ".join(cfg.get("excluded", []))))
    added = []
    for c in data.get("threads", []) if isinstance(data, dict) else []:
        m = THREAD.match(c.get("url", ""))
        if m and not too_old(m.group(2)):
            item = _add(state, c["url"], c.get("title", ""), c.get("question", ""), f"{estimated_date(m.group(2)):%Y-%m} 무렵")
            if item:
                added.append(item)
    save(state)
    return list_message(added, f"[Reddit 후보 {len(added)}건]") if added else "새 Reddit 후보가 없습니다"


def _sub_line(state: dict, sub: str) -> str:
    rule = state["subs"].get(sub.lower())
    if not rule:
        return f"r/{sub} 규칙 아직 안 읽음 (초안 때 자동으로 찾음)"
    who = "AI 검색" if rule.get("source") == "ai" else "직접 확인"
    return f"✔ r/{sub} 규칙 {who} {rule['checked']}" + (" · 링크 허용" if rule["links"] else " · 링크 없음")


def list_message(items: list[dict] | None = None, head: str = "[Reddit 후보]") -> str:
    state = load()
    items = [i for i in state["items"] if i["status"] in ("candidate", "ready", "blocked")] if items is None else items
    if not items:
        return "대기 중인 Reddit 후보가 없습니다"
    label = {"candidate": "초안 전", "ready": "붙여넣기 준비됨", "blocked": "검수 걸림"}
    lines = [head + " 글을 열어 날짜와 내용을 확인해 주세요 (AI 검색 결과라 틀릴 수 있음)"]
    for i in items:
        lines += ["", f"{i['n']}. {i['title']} ({label[i['status']]}{', ' + i['posted'] if i.get('posted') else ''})",
                  f"   {i['question'][:200]}", f"   {_sub_line(state, i['sub'])}", f"   {i['url']}"]
    lines += ["", "초안: `레딧 1 초안` · 제외: `레딧 1 빼기`"]
    return "\n".join(lines)


def _facts_block(posts: list[dict]) -> tuple[str, dict[str, dict]]:
    by_id, out = {}, []
    for p_no, post in enumerate(posts, 1):
        out.append(f"\nPOST P{p_no}: {post['title']}")
        for f in post["facts"]:
            key = f"P{p_no}:{f['id']}"
            by_id[key] = {**f, "post": post}
            out.append(f"[{key}] {f.get('text', '')}")
    return "\n".join(out), by_id


def _review_facts(used: list[dict], by_id: dict[str, dict]) -> str:
    """검수 근거: 인용한 사실이 있는 글의 사실 전부 (쓴 쪽이 인용 번호를 빠뜨린 문장도 대조되게)."""
    titles = {f["post"]["title"] for f in used}
    return "\n".join(f"- {f.get('text', '')}" for f in by_id.values() if f["post"]["title"] in titles)


def seal_of(item: dict) -> str:
    return hashlib.sha256(item.get("paste", "").encode("utf-8")).hexdigest()[:16]


def posted_recently(state: dict) -> int:
    since = (datetime.fromisoformat(now_iso()) - timedelta(days=7)).isoformat()
    return sum(1 for i in state["items"] if i["status"] == "posted" and i.get("done_at", "") >= since)


def look_up_rules(sub: str) -> dict | None:
    """규칙을 안 읽은 커뮤니티: Gemini 검색으로 찾아 본다. 검색 근거가 있을 때만 믿는다 (없으면 AI 기억일 수 있다). PC 에서 Reddit 은 403."""
    for _ in range(RULE_TRIES):
        res = llm.generate("reddit_rules", prompt("reddit_rules", subreddit=sub))  # JSON 을 바로 달라고 하면 검색을 안 한다 (2026-11-02 시험)
        if not res.grounding_urls:
            continue
        data, _r = llm.generate_json("reddit_rules_extract", prompt("reddit_rules_extract", subreddit=sub, report=res.text))
        if isinstance(data, dict) and data.get("found"):
            return {"checked": date.today().isoformat(), "links": False, "source": "ai", "rules_ko": data.get("rules_ko", [])[:12],
                    "url": data.get("source_url", ""), "self_promotion": data.get("self_promotion", "unknown"),
                    "operators_banned": bool(data.get("operators_banned")), "ai_text": data.get("ai_text", "unknown")}
        return None
    return None


def _rule_for(state: dict, sub: str) -> dict:
    """그 커뮤니티 기록. 없으면 찾아서 저장한다. AI 글 금지로 나온 곳은 사람이 확인하기 전에는 초안을 만들지 않는다."""
    rule = state["subs"].get(sub.lower())
    if not rule:
        rule = look_up_rules(sub)
        if not rule:
            raise RedditError(f"r/{sub} 규칙을 찾지 못했습니다. 직접 읽어 본 뒤 보내 주세요: `레딧 규칙확인 {sub}`")
        state["subs"][sub.lower()] = rule
        save(state)
    if rule.get("source") == "ai" and (rule.get("ai_text") == "banned" or rule.get("operators_banned")):
        why = "AI 글을 금지" if rule.get("ai_text") == "banned" else "사이트 운영자·홍보 담당자의 글을 금지"
        raise RedditError(f"r/{sub} 은 {why}한다고 나옵니다. 초안을 만들지 않았습니다.\n" + _rule_lines(rule)
                          + f"\n직접 읽어 보니 괜찮으면: `레딧 규칙확인 {sub}`")
    return rule


def _rule_lines(rule: dict) -> str:
    return "\n".join([f"- {r}" for r in rule.get("rules_ko", [])] + ([rule["url"]] if rule.get("url") else []))


def draft(n: int, direction: str = "") -> dict:
    """direction: 운영자가 준 답변 방향. 있으면 그 입장을 커뮤니티 말투로 옮기고, 없으면 게시된 글의 사실로 답한다."""
    cfg, state = settings(), load()
    item = _item(state, n)
    if item["status"] == "posted":
        raise RedditError(f"{n}번은 이미 게시 기록이 있습니다")
    limit = int(cfg.get("max_per_week", 2))
    if posted_recently(state) >= limit:
        raise RedditError(f"최근 7일 게시가 {limit}개라 이번 주는 더 만들지 않습니다")
    rule = _rule_for(state, item["sub"])
    facts, by_id = _facts_block(_live_posts())
    social = importlib.import_module("pipeline.social")
    numbers = set(social.NUMBER.findall(" ".join(f.get("text", "") for f in by_id.values()))) | set(social.NUMBER.findall(item["question"]))
    problems: list[str] = []
    stage = "reddit_direct" if direction else "reddit_answer"
    item["direction"] = direction
    cosmetic = importlib.import_module("pipeline.02b_auto_review").COSMETIC_PATTERNS
    for _ in range(WRITE_TRIES):  # 검사·검수에 걸리면 지적을 주고 한 번 더 쓴다
        note = f"\n\nA reviewer rejected your previous reply. Fix these problems:\n" + "\n".join(f"- {p}" for p in problems) if problems else ""
        data, _r = llm.generate_json(stage, prompt(stage, subreddit=item["sub"], title=item["title"], question=item["question"],
                                                   facts=facts, direction=direction) + note)
        if not data.get("answerable"):
            item.update(status="blocked", problems=[data.get("reason", "") if direction else f"우리 글로 답할 수 없음: {data.get('reason', '')}"], paste="")
            save(state)
            return item
        body = (data.get("body") or "").strip()
        used = [by_id[k] for k in data.get("fact_ids", []) if k in by_id]
        problems = social.text_problems(body, numbers)
        if not used and not direction:
            problems.append("근거 사실 없음")
        if direction:
            problems += [f"시술·주사제 추천으로 보임: {m.group(0)}" for m in PROMOTES_TREATMENT.finditer(body)]
            problems += [f"화장품을 의약품처럼 표현: {m.group(0)}" for m in cosmetic.finditer(body)]
        if len(body.split()) > int(cfg.get("max_words", 180)):
            problems.append("너무 김")
        ko = ""
        if not problems:
            review, _r = llm.generate_json("reddit_review", prompt("reddit_review", question=item["question"], answer=body,
                                                                   facts=_review_facts(used, by_id) or "(none cited)",
                                                                   direction=direction or "(none)"))
            ko = review.get("ko", "")
            if not review.get("ok"):
                problems = [i.get("problem", "") if isinstance(i, dict) else str(i) for i in review.get("issues", [])] or ["검수 통과 못 함"]
        if not problems:
            break
    item.update(fact_ids=[k for k in data.get("fact_ids", []) if k in by_id], problems=problems, ko=ko, body=body,
                sources=list(dict.fromkeys(f.get("url", "") for f in used if f.get("url"))))
    if problems:
        item.update(status="blocked", paste="")
    else:
        parts = [body]
        if rule["links"] and used:
            parts.append(f"More with sources: {used[0]['post']['url']}")
        parts.append(DISCLOSURE)
        item.update(status="ready", paste="\n\n".join(parts))
        item["seal"] = seal_of(item)
    save(state)
    return item


def paste_messages(item: dict) -> list[str]:
    """[안내, 붙여넣을 본문]. 본문 메시지에는 다른 글자를 넣지 않는다."""
    if item["status"] == "blocked":
        return [f"[Reddit {item['n']}번] ⛔ 초안을 보내지 않았습니다\n" + "\n".join(f"- {p}" for p in item.get("problems", []))]
    if item["status"] != "ready" or item.get("seal") != seal_of(item):
        raise RedditError(f"{item['n']}번은 붙여넣을 수 있는 상태가 아닙니다. `레딧 {item['n']} 초안` 으로 다시 만들어 주세요")
    meta = [f"[Reddit {item['n']}번] r/{item['sub']}: {item['title']}", item["url"], "",
            "1. 글을 열어 날짜·질문·현재 규칙을 확인 (아래 규칙은 AI 검색 요약일 수 있음)", "2. 다음 메시지를 복사해 말투를 다듬고 직접 게시",
            f"3. `레딧 {item['n']} 완료 <댓글 주소>`", "", f"🇰🇷 {item.get('ko', '')}"]
    rule = load()["subs"].get(item["sub"].lower()) or {}
    if rule.get("source") == "ai":
        warn = "⚠️ 홍보 금지로 나옵니다. 공개 문구 때문에 삭제될 수 있습니다\n" if rule.get("self_promotion") == "banned" else ""
        meta += ["", f"📜 r/{item['sub']} 규칙 (AI 검색 요약, 링크 없이 작성)", warn + _rule_lines(rule)]
    if item.get("sources"):
        meta += ["", "출처:", *item["sources"]]
    return ["\n".join(meta), item["paste"]]


def send(item: dict) -> str:
    """메시지를 나눠 보낸다. 못 보내면 출력으로 돌려준다 (텔레그램 답장으로 나간다)."""
    messages = paste_messages(item)
    hermes_send = importlib.import_module("pipeline.worker").hermes_send
    if all(hermes_send(m) for m in messages):
        return "✅ 보냈습니다"
    return "\n\n".join(messages)


WAIT_MINUTES = 30


def waiting_url() -> str:
    """주소만 받고 글 내용을 기다리는 중이면 그 주소."""
    wait = load().get("waiting") or {}
    if wait and datetime.fromisoformat(wait["at"]) >= datetime.fromisoformat(now_iso()) - timedelta(minutes=WAIT_MINUTES):
        return wait["url"]
    return ""


def want(url: str, question: str = "") -> str:
    """사람이 "이 글에 답하자" 고 보낸 글. PC 에서 Reddit 을 열 수 없어(403) 글 내용은 사람이 붙여 넣는다."""
    m = THREAD.match(url)
    if not m:
        return "⚠️ Reddit 글 주소가 아닙니다"
    state = load()
    if not question.strip():
        state["waiting"] = {"url": m.group(0), "at": now_iso()}
        save(state)
        return "글 내용을 이어서 붙여 넣어 주세요 (Reddit 글은 제가 직접 열 수 없습니다). 받으면 바로 답변을 만듭니다."
    state.pop("waiting", None)
    item = next((i for i in state["items"] if i["thread"] == m.group(2).lower()), None)
    if item:
        item.update(question=question, title=question[:100])
    else:
        item = _add(state, m.group(0), question[:100], question)
    save(state)
    if not item:
        return "⚠️ 답변을 만들지 않았습니다: 병원 추천을 묻는 글이거나 제외 커뮤니티입니다"
    try:
        return send(draft(item["n"]))
    except RedditError as e:
        state = load()
        _item(state, item["n"])["wanted"] = True
        save(state)
        return f"⚠️ {e}"


def allow(sub: str, links: bool) -> str:
    state = load()
    name = sub.removeprefix("r/").strip()
    if name.lower() in {s.lower() for s in settings().get("excluded", [])}:
        raise RedditError(f"r/{name} 은 제외 목록에 있습니다")
    state["subs"][name.lower()] = {"checked": date.today().isoformat(), "links": links, "source": "human"}
    save(state)
    return f"r/{name} 규칙 확인 기록 ({'링크 허용' if links else '링크 없이 답변'})"


def done(n: int, url: str) -> str:
    host = re.match(r"https://([^/]+)/", url or "")
    if not host or host.group(1).lower() not in COMMENT_HOSTS:
        raise RedditError("Reddit 댓글 주소를 넣어 주세요")
    state = load()
    item = _item(state, n)
    item.update(status="posted", comment_url=url, done_at=now_iso())
    save(state)
    return f"{n}번 게시 기록 (최근 7일 {posted_recently(state)}개)"


def weekly_message(now: datetime | None = None) -> str:
    """정해진 요일에 한 번 후보 찾기 (2026-11-02 사람 요청: 규칙 확인과 상관없이 매주)."""
    social = importlib.import_module("pipeline.social")
    cfg, now = settings(), now or social.now_kst()
    state = load()
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if now.weekday() != int(cfg.get("find_weekday", 1)) or now.hour < int(cfg.get("find_hour", 10)) or state.get("found_week") == week:
        return ""
    state["found_week"] = week
    save(state)
    return find()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reddit 답변 준비")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find")
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("url")
    p.add_argument("question")
    p = sub.add_parser("want")
    p.add_argument("url")
    p.add_argument("question", nargs="?", default="")
    p = sub.add_parser("allow")
    p.add_argument("sub")
    p.add_argument("--links", action="store_true")
    for name in ("draft", "show", "skip"):
        sub.add_parser(name).add_argument("n", type=int)
    p = sub.add_parser("direct")
    p.add_argument("n", type=int)
    p.add_argument("direction")
    p = sub.add_parser("done")
    p.add_argument("n", type=int)
    p.add_argument("url")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "find":
            print(find())
        elif args.cmd == "list":
            print(list_message())
        elif args.cmd == "add":
            state = load()
            item = _add(state, args.url, args.question[:100], args.question)
            if not item:
                raise RedditError("추가하지 못했습니다: 주소가 틀렸거나, 이미 있거나, 제외 서브레딧이거나, 병원 추천을 묻는 글입니다")
            save(state)
            print(list_message([item], f"[Reddit {item['n']}번 추가]"))
        elif args.cmd == "allow":
            print(allow(args.sub, args.links))
            for item in [i for i in load()["items"] if i.get("wanted") and i["sub"].lower() == args.sub.removeprefix("r/").lower()]:
                state = load()
                _item(state, item["n"])["wanted"] = False
                save(state)
                print(send(draft(item["n"])))
        elif args.cmd == "want":
            print(want(args.url, args.question))
        elif args.cmd == "draft":
            print(send(draft(args.n)))
        elif args.cmd == "direct":
            print(send(draft(args.n, args.direction)))
        elif args.cmd == "show":
            print(send(_item(load(), args.n)))
        elif args.cmd == "skip":
            state = load()
            _item(state, args.n)["status"] = "skipped"
            save(state)
            print(f"{args.n}번 제외")
        elif args.cmd == "done":
            print(done(args.n, args.url))
    except RedditError as e:
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("reddit", main)
