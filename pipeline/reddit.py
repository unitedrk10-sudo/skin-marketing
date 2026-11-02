"""Reddit 답변 준비 — 후보 찾기 → 커뮤니티 단계 판단 → 초안·사실 정리 → 검수 → 붙여넣을 글 → 기록 (운영 기준: docs/reddit-ops.md).

게시는 사람이 직접 한다. Reddit API 는 사전 승인제이고 봇·AI 에이전트 게시도 그 대상이라 자동 게시·웹 자동 조작은 만들지 않는다.
댓글에는 X 같은 작성 링크도 없어서, 글 주소와 붙여넣을 본문을 메시지 두 개로 나눠 보낸다.

  - 커뮤니티 단계 (2026-11-02, `standing`): 제외·정지·미확인·문의 필요·직접 작성·활동. 설정(config/reddit.yaml communities, 조사 결과)과
    운영자 기록(원문 확인·운영진 답변·삭제·경고, content/reddit/state.json)을 합쳐 코드가 정한다. 사람의 "원문 확인"은 운영자 참여 금지를 풀지 못하고
    (운영진 허락이 있어야), AI 글 금지 커뮤니티에서는 AI 가 쓴 글을 만들지 않는다 — 사실 정리와 운영자가 쓴 글 검사만
  - 후보: Gemini 검색(reddit_find). 병원 추천을 묻는 글·제외·정지 커뮤니티·오래된 글은 코드가 뺀다. 글 나이는 AI 가 말한 날짜가
    아니라 글 번호로 어림한다. 내용은 AI 요약이라 사람이 열어서 확인한다
  - 초안: 활동 단계만 게시용. 미확인·문의 필요 단계는 "게시용 아님" 연습 초안(메시지 한 개). Claude(reddit_answer)가 게시된 중립 글의 사실 목록으로만 쓴다
  - 검수: 02b 금지 규칙(코드) → Gemini(reddit_review). 통과한 글만 붙여넣을 본문을 보낸다
  - 운영자 공개 문구는 코드가 항상 붙이고("AI-assisted" 는 AI 가 쓴 글에만, 설정 disclosure_ai_label), 블로그 링크는 원문 확인으로 허용된 곳에서만
  - 기록: 게시·반응·삭제·경고. 삭제 → 그 커뮤니티 정지, 경고 → 전체 정지, 같은 커뮤니티 연속 2개 0점 이하 → 그 커뮤니티 정지

    python -m pipeline.reddit find | list | communities
    python -m pipeline.reddit add <주소> "질문 내용"
    python -m pipeline.reddit draft <N> | direct <N> "운영자 답변 방향" | facts <N> | check <N> "운영자가 쓴 영문 본문"
    python -m pipeline.reddit show <N> | skip <N> | done <N> <댓글 주소> | react <N> <점수> [--replies 2] | removed <N> [--note …]
    python -m pipeline.reddit allow <서브레딧> [--links] [--no-ai]
    python -m pipeline.reddit asked <서브레딧> | mods <서브레딧> allowed|refused [--links] [--no-ai] [--note …] | reset <서브레딧>
    python -m pipeline.reddit warn <서브레딧|all> [--note …] | resume <서브레딧|all>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
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
# 운영자 공개 문구 — 코드가 항상 붙이고 설정으로 끌 수 없다. "AI-assisted" 줄만 설정(disclosure_ai_label)을 따른다 (2026-11-02)
OPERATOR_LINE = "Disclosure: I run Skinbound Korea, an information site about skin treatments in Korea."
AI_LINE = "AI-assisted and human-checked."
END_LINE = "Not medical advice."
DISCLOSURE = f"{OPERATOR_LINE} {AI_LINE} {END_LINE}"           # AI 가 쓴 글
DISCLOSURE_OPERATOR = f"{OPERATOR_LINE} {END_LINE}"           # 운영자가 직접 쓴 글

WRITE_TRIES = 2
RULE_TRIES = 2
STALE_DAYS = 90         # 원문 확인 기록이 이보다 오래되면 다시 확인 대상으로 표시
REACT_AFTER_DAYS = 3    # 게시 후 이만큼 지나도 반응 기록이 없으면 리포트에 표시
# 운영자 방향·운영자가 쓴 글에서도 시술·주사제 추천은 코드가 막는다. 의사가 아닌 운영자의 추천은 §56 광고 시비, 리투오 등은 광고 제한 쟁점 (2026-11-02)
TREATMENT = r"(?:botox|botulinum|fillers?|skin\s*boosters?|rejuran|juvelook|re2o|ecm|pdrn|exosomes?|thermage|ulthera|laser|injectables?|리투오|리쥬란|필러|보톡스)"
PROMOTES_TREATMENT = re.compile(rf"\b(?:recommend|suggest|go for|get|try|choose|pick|book|worth)\b[^.!?\n]{{0,50}}\b{TREATMENT}\b"
                                rf"|\b{TREATMENT}\b[^.!?\n]{{0,40}}\b(?:is|are)\s+(?:worth|great|amazing|the best)\b|리투오|Re2O", re.I)

MODE_LABEL = {"paused": "전체 정지", "stopped": "정지", "excluded": "제외", "unverified": "미확인", "ask_mods": "문의 필요",
              "manual": "직접 작성", "active": "활동"}
BLOCKED_MODES = ("paused", "stopped", "excluded")


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


def _name(sub: str) -> str:
    return sub.removeprefix("r/").strip()


# ── 커뮤니티 단계 ─────────────────────────────────────────────

def _community(sub: str) -> dict:
    """설정의 기준 (개발 세션에서 정한 단계·조사 결과). 대소문자 무시."""
    for name, c in (settings().get("communities") or {}).items():
        if name.lower() == sub.lower():
            return {"name": name, **(c or {})}
    return {}


def _record(state: dict, sub: str) -> dict:
    """운영자 기록. 2026-11-02 이전 형식({checked, links, source})은 읽을 때 바꾼다 — 예전 "직접 확인"은 원문 확인으로만 친다."""
    rec = state["subs"].get(sub.lower())
    if rec is None:
        return {}
    if "source" in rec:
        old = dict(rec)
        rec.clear()
        if old["source"] == "ai":
            rec["research"] = {k: old[k] for k in ("checked", "rules_ko", "url", "self_promotion", "operators_banned", "ai_text") if k in old}
            rec["research"]["found"] = True
        else:
            rec.update(read=old.get("checked", ""), links=bool(old.get("links")))
    return rec


def standing(state: dict, sub: str) -> dict:
    """{mode, label, why, links, writer}. writer: post = 게시용 초안, practice = 연습 초안, facts = 사실 정리만, None = 아무것도 안 함."""
    cfg, c, rec = settings(), _community(sub), _record(state, sub)
    research = rec.get("research") or {}
    why: list[str] = []
    if c.get("note"):
        why.append(c["note"])
    paused = state.get("paused") or ({"note": "설정 paused"} if cfg.get("paused") else None)
    if paused:
        return _st("paused", [f"Reddit 전체 정지 ({paused.get('at', '')[:10]} {paused.get('note', '')})".strip()])
    if rec.get("stopped"):
        return _st("stopped", [f"{rec['stopped']} {rec.get('stop_reason', '')}".strip()])
    if c.get("status") == "excluded":
        return _st("excluded", why)
    if rec.get("mods") == "refused":
        return _st("excluded", [f"운영진 거절 {rec.get('mods_at', '')} {rec.get('mods_note', '')}".strip()])
    ai_banned = c.get("ai_text") == "banned" or research.get("ai_text") == "banned" or bool(rec.get("ai_banned"))
    if ai_banned and "AI" not in c.get("note", ""):
        why.append("AI 글 금지")
    operators = c.get("status") == "ask_mods" or research.get("operators_banned") or research.get("self_promotion") == "banned"
    allowed = rec.get("mods") == "allowed"
    if operators and not allowed:
        mode = "ask_mods"
        why.append(f"운영진 문의 {rec['asked']} 보냄, 답 기다리는 중" if rec.get("asked") else "운영진에게 참여 가능 여부를 물어야 함")
    elif not rec.get("read") and not allowed:
        mode = "unverified"
        why.append("규칙 원문 확인 전")
    else:
        mode = "manual" if ai_banned else "active"
        if rec.get("read") and rec["read"] < (date.today() - timedelta(days=STALE_DAYS)).isoformat():
            why.append(f"원문 확인 {STALE_DAYS}일 지남, 다시 확인")
    known = _researched(c) or bool(research.get("found"))
    writer = {"active": "post", "manual": "facts"}.get(mode) or ("practice" if known and not ai_banned else "facts")
    return {**_st(mode, why), "links": mode in ("active", "manual") and bool(rec.get("links")), "writer": writer}


def _st(mode: str, why: list[str]) -> dict:
    return {"mode": mode, "label": MODE_LABEL[mode], "why": [w for w in why if w], "links": False, "writer": None}


def _researched(c: dict) -> bool:
    """설정에 조사 결과(note·ai_text)가 있는가. 단계만 적힌 곳은 규칙을 모르는 곳으로 본다."""
    return bool(c.get("note") or c.get("ai_text"))


def _blocked_sub(state: dict, sub: str) -> bool:
    return standing(state, sub)["mode"] in ("stopped", "excluded")


def can_post(st: dict, written_by: str) -> bool:
    """지금 기준으로 이 글을 게시해도 되는가. 직접 작성 단계에서는 운영자가 쓴 글만."""
    return st["mode"] == "active" or (st["mode"] == "manual" and written_by == "operator")


def disclosure(written_by: str) -> str:
    label = settings().get("disclosure_ai_label", "ai_only")
    ai = label == "always" or (label == "ai_only" and written_by != "operator")
    return DISCLOSURE if ai else DISCLOSURE_OPERATOR


# ── 후보 ─────────────────────────────────────────────────────

def _add(state: dict, url: str, title: str, question: str, posted: str = "") -> dict | None:
    m = THREAD.match(url or "")
    if not m or any(i["thread"] == m.group(2).lower() for i in state["items"]):
        return None
    sub = m.group(1)
    if _blocked_sub(state, sub) or CLINIC_ASK.search(f"{title}\n{question}"):
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


def _skip_subs(state: dict) -> list[str]:
    names = [n for n, c in (settings().get("communities") or {}).items() if (c or {}).get("status") == "excluded"]
    return names + [s for s, r in state["subs"].items() if r.get("stopped") or r.get("mods") == "refused"]


def find() -> str:
    cfg = settings()
    state = load()
    if standing(state, "_")["mode"] == "paused":
        return "Reddit 전체 정지 중이라 후보를 찾지 않았습니다 (`레딧 기준`)"
    posts = _live_posts()
    data, _ = llm.generate_json("reddit_find", prompt("reddit_find", today=date.today().isoformat(), year=str(date.today().year), count=str(cfg.get("find_count", 8)),
                                                      topics="\n".join(f"- {p['title']}" for p in posts) or "(none)",
                                                      excluded=", ".join(_skip_subs(state)) or "(none)"))
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
    st = standing(state, sub)
    tool = {"post": "초안 가능", "practice": "연습 초안만", "facts": "사실 정리·직접 작성 검사"}.get(st["writer"] or "", "만들지 않음")
    return f"r/{sub}: {st['label']} — {tool}" + (f" ({'; '.join(st['why'])})" if st["why"] else "") + (" · 링크 허용" if st["links"] else "")


def list_message(items: list[dict] | None = None, head: str = "[Reddit 후보]") -> str:
    state = load()
    items = [i for i in state["items"] if i["status"] in ("candidate", "ready", "blocked", "manual")] if items is None else items
    if not items:
        return "대기 중인 Reddit 후보가 없습니다"
    label = {"candidate": "초안 전", "ready": "붙여넣기 준비됨", "blocked": "검수 걸림", "manual": "직접 작성 대기"}
    lines = [head + " 글을 열어 날짜와 내용을 확인해 주세요 (AI 검색 결과라 틀릴 수 있음)"]
    for i in items:
        practice = " · 연습" if i.get("practice") and i["status"] == "ready" else ""
        lines += ["", f"{i['n']}. {i['title']} ({label.get(i['status'], i['status'])}{practice}{', ' + i['posted'] if i.get('posted') else ''})",
                  f"   {i['question'][:200]}", f"   {_sub_line(state, i['sub'])}", f"   {i['url']}"]
    lines += ["", "초안: `레딧 1 초안` · 직접 쓰기: `레딧 1 사실` → `레딧 1 검사: 본문` · 제외: `레딧 1 빼기` · 기준: `레딧 기준`"]
    return "\n".join(lines)


# ── 규칙 조사 ────────────────────────────────────────────────

def look_up_rules(sub: str) -> dict:
    """규칙 기록이 없는 커뮤니티: Gemini 검색으로 찾아 본다. 검색 근거가 있을 때만 믿는다 (없으면 AI 기억일 수 있다). PC 에서 Reddit 은 403.
    못 찾으면 {"found": False} — 다시 찾으려면 `레딧 규칙초기화`."""
    for _ in range(RULE_TRIES):
        res = llm.generate("reddit_rules", prompt("reddit_rules", subreddit=sub))  # JSON 을 바로 달라고 하면 검색을 안 한다 (2026-11-02 시험)
        if not res.grounding_urls:
            continue
        data, _r = llm.generate_json("reddit_rules_extract", prompt("reddit_rules_extract", subreddit=sub, report=res.text))
        if isinstance(data, dict) and data.get("found"):
            return {"found": True, "checked": date.today().isoformat(), "rules_ko": data.get("rules_ko", [])[:12],
                    "url": data.get("source_url", ""), "self_promotion": data.get("self_promotion", "unknown"),
                    "operators_banned": bool(data.get("operators_banned")), "ai_text": data.get("ai_text", "unknown")}
        break
    return {"found": False, "checked": date.today().isoformat()}


def _ensure_research(state: dict, sub: str) -> None:
    rec = _record(state, sub)
    if _researched(_community(sub)) or rec.get("research") or rec.get("read") or rec.get("mods"):
        return
    state["subs"].setdefault(sub.lower(), {})["research"] = look_up_rules(sub)
    save(state)


def _rule_lines(rule: dict) -> str:
    return "\n".join([f"- {r}" for r in rule.get("rules_ko", [])] + ([rule["url"]] if rule.get("url") else []))


# ── 사실·초안·검사 ───────────────────────────────────────────

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


def posted_recently(state: dict, sub: str = "") -> int:
    since = (datetime.fromisoformat(now_iso()) - timedelta(days=7)).isoformat()
    return sum(1 for i in state["items"] if i["status"] in ("posted", "removed") and i.get("done_at", "") >= since
               and (not sub or i["sub"].lower() == sub.lower()))


def _check_limits(state: dict, sub: str) -> None:
    cfg = settings()
    limit, per_sub = int(cfg.get("max_per_week", 2)), int(cfg.get("max_per_community_week", 1))
    if posted_recently(state) >= limit:
        raise RedditError(f"최근 7일 게시가 {limit}개라 이번 주는 더 만들지 않습니다")
    if posted_recently(state, sub) >= per_sub:
        raise RedditError(f"r/{sub} 에 최근 7일 게시가 {per_sub}개라 이번 주는 이 커뮤니티에 더 만들지 않습니다")


def _prepare(n: int) -> tuple[dict, dict, dict]:
    """(state, item, standing). 제외·정지 커뮤니티, 게시가 끝난 글은 여기서 멈춘다."""
    state = load()
    item = _item(state, n)
    if item["status"] in ("posted", "removed"):
        raise RedditError(f"{n}번은 이미 게시 기록이 있습니다")
    _ensure_research(state, item["sub"])
    st = standing(state, item["sub"])
    if st["mode"] in BLOCKED_MODES:
        raise RedditError(f"r/{item['sub']} 은 {st['label']} 단계라 만들지 않습니다" + (f": {'; '.join(st['why'])}" if st["why"] else ""))
    return state, item, st


def _numbers(by_id: dict, question: str) -> set[str]:
    social = importlib.import_module("pipeline.social")
    return set(social.NUMBER.findall(" ".join(f.get("text", "") for f in by_id.values()))) | set(social.NUMBER.findall(question))


def draft(n: int, direction: str = "") -> dict:
    """direction: 운영자가 준 답변 방향. 있으면 그 입장을 커뮤니티 말투로 옮기고, 없으면 게시된 글의 사실로 답한다.
    AI 글을 못 쓰는 곳(직접 작성 단계, AI 글 금지, 규칙을 모르는 곳)은 사실 정리로 바꾼다."""
    cfg = settings()
    state, item, st = _prepare(n)
    if st["writer"] == "facts":
        return notes(n)
    if st["writer"] == "post":
        _check_limits(state, item["sub"])
    facts, by_id = _facts_block(_live_posts())
    social = importlib.import_module("pipeline.social")
    numbers = _numbers(by_id, item["question"])
    problems: list[str] = []
    stage = "reddit_direct" if direction else "reddit_answer"
    item.update(direction=direction, written_by="ai", practice=st["writer"] == "practice")
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
                                                                   direction=direction or "(none)", author="Another AI wrote this reply"))
            ko = review.get("ko", "")
            if not review.get("ok"):
                problems = [i.get("problem", "") if isinstance(i, dict) else str(i) for i in review.get("issues", [])] or ["검수 통과 못 함"]
        if not problems:
            break
    item.update(fact_ids=[k for k in data.get("fact_ids", []) if k in by_id], problems=problems, ko=ko, body=body,
                sources=list(dict.fromkeys(f.get("url", "") for f in used if f.get("url"))))
    _finish(item, problems, body, used[0]["post"]["url"] if used and st["links"] and not item["practice"] else "")
    save(state)
    return item


def _finish(item: dict, problems: list[str], body: str, link: str) -> None:
    if problems:
        item.update(status="blocked", paste="")
        return
    parts = [body] + ([f"More with sources: {link}"] if link else []) + [disclosure(item.get("written_by", "ai"))]
    item.update(status="ready", paste="\n\n".join(parts))
    item["seal"] = seal_of(item)


def notes(n: int) -> dict:
    """사실 정리: 직접 쓸 운영자에게 질문에 맞는 사실을 원문 그대로 준다. Claude(reddit_facts)는 고르기만 하고 사실 문장은 코드가 목록에서 꺼낸다."""
    state, item, st = _prepare(n)
    facts, by_id = _facts_block(_live_posts())
    data, _r = llm.generate_json("reddit_facts", prompt("reddit_facts", subreddit=item["sub"], title=item["title"], question=item["question"], facts=facts))
    ids = [k for k in (data.get("fact_ids") or []) if k in by_id][:8] if isinstance(data, dict) else []
    if not ids or not data.get("answerable", True):
        item.update(status="blocked", problems=[f"우리 글로 답할 수 없음: {(data or {}).get('reason', '맞는 사실 없음')}"], paste="", note_ids=[])
    else:
        item.update(status="manual", note_ids=ids, gaps=data.get("gaps_ko", ""), problems=[], paste="",
                    practice=not can_post(st, "operator"))
    save(state)
    return item


def check(n: int, text: str) -> dict:
    """운영자가 직접 쓴 영문 본문 검사 → 통과하면 공개 문구를 붙인 붙여넣을 글. 같은 코드 규칙과 Gemini 검수를 거친다."""
    cfg = settings()
    state, item, st = _prepare(n)
    body = text.strip()
    if not body:
        raise RedditError("검사할 본문이 없습니다: `레딧 N 검사: 영어 본문`")
    practice = not can_post(st, "operator")
    if not practice:
        _check_limits(state, item["sub"])
    _facts, by_id = _facts_block(_live_posts())
    social = importlib.import_module("pipeline.social")
    cosmetic = importlib.import_module("pipeline.02b_auto_review").COSMETIC_PATTERNS
    problems = social.text_problems(body, _numbers(by_id, item["question"]))
    problems += [f"시술·주사제 추천으로 보임: {m.group(0)}" for m in PROMOTES_TREATMENT.finditer(body)]
    problems += [f"화장품을 의약품처럼 표현: {m.group(0)}" for m in cosmetic.finditer(body)]
    if len(body.split()) > int(cfg.get("max_words", 180)):
        problems.append("너무 김")
    noted = [by_id[k] for k in item.get("note_ids", []) if k in by_id]
    ko = ""
    if not problems:
        review, _r = llm.generate_json("reddit_review", prompt("reddit_review", question=item["question"], answer=body,
                                                               facts=_review_facts(noted, by_id) if noted else "\n".join(f"- {f.get('text', '')}" for f in by_id.values()),
                                                               direction="(none)", author="The site operator wrote this reply personally"))
        ko = review.get("ko", "")
        if not review.get("ok"):
            problems = [i.get("problem", "") if isinstance(i, dict) else str(i) for i in review.get("issues", [])] or ["검수 통과 못 함"]
    item.update(written_by="operator", direction="", body=body, ko=ko, problems=problems, practice=practice,
                sources=list(dict.fromkeys(f.get("url", "") for f in noted if f.get("url"))))
    _finish(item, problems, body, noted[0]["post"]["url"] if noted and st["links"] and not practice else "")
    save(state)
    return item


# ── 메시지 ───────────────────────────────────────────────────

def paste_messages(item: dict) -> list[str]:
    """[안내, 붙여넣을 본문]. 본문 메시지에는 다른 글자를 넣지 않는다. 지금 기준으로 게시할 수 없는 글은 메시지 한 개."""
    head = f"[Reddit {item['n']}번] r/{item['sub']}: {item['title']}"
    if item["status"] == "blocked":
        return [f"{head}\n⛔ 붙여넣을 글을 만들지 않았습니다\n" + "\n".join(f"- {p}" for p in item.get("problems", []))
                + (f"\n고쳐서 다시: `레딧 {item['n']} 검사: 본문`" if item.get("written_by") == "operator" else "")]
    state = load()
    st = standing(state, item["sub"])
    if item["status"] == "manual":
        return [notes_message(item, st)]
    if item["status"] != "ready" or item.get("seal") != seal_of(item):
        raise RedditError(f"{item['n']}번은 붙여넣을 수 있는 상태가 아닙니다. `레딧 {item['n']} 초안` 으로 다시 만들어 주세요")
    if item.get("practice") or not can_post(st, item.get("written_by", "ai")):
        why = "; ".join(st["why"]) or st["label"]
        return [f"{head}\n🧪 연습 초안 — 게시용 아님 (r/{item['sub']}: {st['label']}, {why})\n"
                "기준(docs/reddit-ops.md)상 아직 이 커뮤니티에 올리지 않습니다. 단계가 바뀌면 다시 만들어 주세요.\n\n"
                f"{item['paste']}\n\n🇰🇷 {item.get('ko', '')}"]
    meta = [head, item["url"], "",
            "1. 글을 열어 날짜·질문·현재 규칙을 확인", "2. 다음 메시지를 복사해 말투를 다듬고 직접 게시 (공개 문구는 지우지 않기)",
            f"3. `레딧 {item['n']} 완료 <댓글 주소>`", "", f"🇰🇷 {item.get('ko', '')}"]
    research = _record(state, item["sub"]).get("research") or {}
    if research.get("found"):
        meta += ["", f"📜 r/{item['sub']} 규칙 (AI 검색 요약)", _rule_lines(research)]
    if item.get("sources"):
        meta += ["", "출처:", *item["sources"]]
    return ["\n".join(meta), item["paste"]]


def notes_message(item: dict, st: dict) -> str:
    _facts, by_id = _facts_block(_live_posts())
    lines = [f"[Reddit {item['n']}번] r/{item['sub']}: {item['title']}", item["url"], "",
             f"✍️ 직접 작성 — r/{item['sub']}: {st['label']}" + (f" ({'; '.join(st['why'])})" if st["why"] else "")
             + ("\n🧪 지금은 게시용 아님 (연습)" if item.get("practice") else ""),
             "AI 가 쓴 글을 올리지 않는 곳입니다. 아래 사실만 근거로 영어로 직접 써서 보내 주세요: "
             f"`레딧 {item['n']} 검사: 본문` (공개 문구는 코드가 붙입니다)", "", "근거 사실 (원문 그대로):"]
    for k in item.get("note_ids", []):
        if k in by_id:
            f = by_id[k]
            lines += [f"- {f.get('text', '')}", f"  {f.get('url', '')}"]
    if item.get("gaps"):
        lines += ["", f"사실 목록에 없는 부분: {item['gaps']}"]
    return "\n".join(lines)


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
        return "⚠️ 답변을 만들지 않았습니다: 병원 추천을 묻는 글이거나 제외·정지 커뮤니티입니다"
    try:
        return send(draft(item["n"]))
    except RedditError as e:
        state = load()
        _item(state, item["n"])["wanted"] = True
        save(state)
        return f"⚠️ {e}"


# ── 운영자 기록 ──────────────────────────────────────────────

def _editable(state: dict, sub: str) -> dict:
    name = _name(sub)
    if _community(name).get("status") == "excluded":
        raise RedditError(f"r/{name} 은 기준상 제외 커뮤니티입니다 (config/reddit.yaml, 바꾸려면 개발 세션)")
    rec = _record(state, name)
    if rec.get("stopped"):
        raise RedditError(f"r/{name} 은 정지 중입니다 ({rec['stopped']} {rec.get('stop_reason', '')}). 원인을 확인한 뒤 `레딧 재개 {name}`")
    return state["subs"].setdefault(name.lower(), rec)


def _after(state: dict, sub: str, head: str) -> str:
    st = standing(state, _name(sub))
    return f"{head}\n→ 현재 단계: {_sub_line(state, _name(sub))}" + ("\n운영진 허락이 있어야 게시할 수 있습니다: `레딧 운영진답변 "
                                                                f"{_name(sub)} 허용`" if st["mode"] == "ask_mods" else "")


def allow(sub: str, links: bool, no_ai: bool = False) -> str:
    """원문 확인 기록. 운영자 참여 금지로 나온 곳은 이것만으로 풀리지 않는다 (운영진 허락 필요)."""
    state = load()
    rec = _editable(state, sub)
    rec.update(read=date.today().isoformat(), links=links, ai_banned=no_ai)
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 규칙 원문 확인 기록 ({'링크 허용' if links else '링크 없이'}{', AI 글 금지' if no_ai else ''})")


def asked(sub: str) -> str:
    state = load()
    _editable(state, sub)["asked"] = date.today().isoformat()
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 운영진 문의 보냄 기록")


def mods(sub: str, decision: str, links: bool = False, no_ai: bool = False, note: str = "") -> str:
    if decision not in ("allowed", "refused"):
        raise RedditError("운영진 답변은 허용 또는 거절입니다")
    state = load()
    rec = _editable(state, sub)
    rec.update(mods=decision, mods_at=date.today().isoformat(), mods_note=note)
    if decision == "allowed":
        rec.update(links=links or bool(rec.get("links")), ai_banned=no_ai or bool(rec.get("ai_banned")))
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 운영진 답변: {'허용' if decision == 'allowed' else '거절'}" + (f" ({note})" if note else ""))


def reset(sub: str) -> str:
    """확인·조사 기록을 지운다. 정지는 이걸로 풀리지 않는다 (재개 먼저)."""
    state = load()
    _editable(state, sub)
    state["subs"].pop(_name(sub).lower(), None)
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 기록을 지웠습니다 (다음 초안 때 규칙을 다시 조사)")


def done(n: int, url: str) -> str:
    host = re.match(r"https://([^/]+)/", url or "")
    if not host or host.group(1).lower() not in COMMENT_HOSTS:
        raise RedditError("Reddit 댓글 주소를 넣어 주세요")
    state = load()
    item = _item(state, n)
    st = standing(state, item["sub"])
    outside = item.get("status") != "ready" or bool(item.get("practice")) or not can_post(st, item.get("written_by", "ai"))
    item.update(status="posted", comment_url=url, done_at=now_iso(), outside_standard=outside)
    save(state)
    warn = (f"\n⚠️ 기준 밖 게시로 기록했습니다 (r/{item['sub']}: {st['label']}). 삭제·경고가 오면 `레딧 {n} 삭제됨` / `레딧 경고 {item['sub']}`"
            if outside else "")
    return f"{n}번 게시 기록 (최근 7일 {posted_recently(state)}개)" + warn


def _stop(state: dict, sub: str, reason: str) -> None:
    rec = state["subs"].setdefault(sub.lower(), _record(state, sub))
    rec.update(stopped=date.today().isoformat(), stop_reason=reason)


def removed(n: int, note: str = "") -> str:
    """우리 댓글이 삭제됨 → 그 커뮤니티 정지."""
    state = load()
    item = _item(state, n)
    if item["status"] != "posted":
        raise RedditError(f"{n}번은 게시 기록이 없습니다")
    item.update(status="removed", removed_at=now_iso(), removed_note=note)
    _stop(state, item["sub"], f"{n}번 댓글 삭제 {note}".strip())
    save(state)
    return f"⛔ {n}번 삭제 기록 — r/{item['sub']} 활동을 멈췄습니다. 원인을 확인하고 원문부터 다시 읽은 뒤에만 `레딧 재개 {item['sub']}`"


def react(n: int, score: int, replies: int | None = None) -> str:
    """반응 기록. 같은 커뮤니티의 최근 게시 2개가 모두 0점 이하면 그 커뮤니티 정지."""
    state = load()
    item = _item(state, n)
    if item["status"] != "posted":
        raise RedditError(f"{n}번은 게시 기록이 없습니다")
    item.update(score=score, **({"replies": replies} if replies is not None else {}), reacted_at=now_iso())
    out = f"{n}번 반응 기록: {score}점" + (f", 답글 {replies}" if replies is not None else "")
    recent = sorted((i for i in state["items"] if i["sub"].lower() == item["sub"].lower() and i["status"] in ("posted", "removed")),
                    key=lambda i: i.get("done_at", ""))[-2:]
    if len(recent) == 2 and all(i.get("score") is not None and i["score"] <= 0 for i in recent):
        _stop(state, item["sub"], "연속 2개 0점 이하")
        out += f"\n⛔ r/{item['sub']} 에서 연속 2개가 0점 이하라 활동을 멈췄습니다. 재개: `레딧 재개 {item['sub']}`"
    save(state)
    return out


def warn(target: str, note: str = "") -> str:
    """운영진·관리자 경고, 계정 제한, 섀도밴 의심 → Reddit 전체 정지 (그 커뮤니티도 정지)."""
    state = load()
    sub = "" if target.lower() in ("all", "전체") else _name(target)
    state["paused"] = {"at": now_iso(), "sub": sub, "note": note or "경고"}
    if sub:
        _stop(state, sub, f"경고 {note}".strip())
    save(state)
    return "⛔ Reddit 전체 정지 — 초안·후보 찾기를 멈췄습니다. 방식 재검토 뒤 `레딧 재개 전체`"


def resume(target: str) -> str:
    state = load()
    if target.lower() in ("all", "전체"):
        state.pop("paused", None)
        save(state)
        return "Reddit 전체 정지 해제"
    name = _name(target)
    rec = _record(state, name)
    if not rec.get("stopped"):
        return f"r/{name} 은 정지 상태가 아닙니다"
    for key in ("stopped", "stop_reason", "read", "links", "ai_banned"):
        rec.pop(key, None)
    if rec.get("mods") == "allowed":
        rec.pop("mods", None)
    save(state)
    return _after(state, name, f"r/{name} 정지 해제 — 원문 확인부터 다시 (`레딧 규칙확인 {name} …`)")


def communities_message() -> str:
    state = load()
    names = {n.lower(): n for n in (settings().get("communities") or {})}
    for key in state["subs"]:
        names.setdefault(key, next((i["sub"] for i in state["items"] if i["sub"].lower() == key), key))
    paused = state.get("paused")
    lines = ["[Reddit 커뮤니티 기준] docs/reddit-ops.md"] + ([f"⛔ 전체 정지 중: {paused.get('at', '')[:10]} {paused.get('note', '')}"] if paused else [])
    for key in sorted(names):
        rec = _record(state, names[key])
        extra = [f"원문 확인 {rec['read']}" if rec.get("read") else "",
                 f"운영진 {'허용' if rec.get('mods') == 'allowed' else '거절' if rec.get('mods') == 'refused' else ''} {rec.get('mods_at', '')}".strip() if rec.get("mods") else "",
                 f"게시 {sum(1 for i in state['items'] if i['sub'].lower() == key and i['status'] in ('posted', 'removed'))}"]
        lines += ["", f"• {_sub_line(state, names[key])}"] + ([f"  {' · '.join(x for x in extra if x)}"] if any(extra[:2]) else [])
    lines += ["", "기록: `레딧 규칙확인 <이름> [링크허용] [AI금지]` · `레딧 문의 <이름>` · `레딧 운영진답변 <이름> 허용|거절` · `레딧 규칙초기화 <이름>`"]
    return "\n".join(lines)


def report_lines(days: int = 7) -> list[str]:
    """주간 리포트용 (07_report). 기록이 하나도 없으면 빈 목록."""
    state = load()
    if not state["items"] and not state["subs"]:
        return []
    now = datetime.fromisoformat(now_iso())
    since = (now - timedelta(days=days)).isoformat()
    posted = [i for i in state["items"] if i["status"] in ("posted", "removed")]
    recent = [i for i in posted if i.get("done_at", "") >= since]
    no_react = [str(i["n"]) for i in posted if i["status"] == "posted" and i.get("score") is None
                and i.get("done_at", "") <= (now - timedelta(days=REACT_AFTER_DAYS)).isoformat()]
    scores = [i["score"] for i in recent if i.get("score") is not None]
    line = f"👽 Reddit: 최근 {days}일 게시 {len(recent)} (누적 {len(posted)})"
    if scores:
        line += f", 점수 합 {sum(scores)}, 답글 {sum(i.get('replies') or 0 for i in recent)}"
    removed_n = sum(1 for i in posted if i["status"] == "removed")
    if removed_n:
        line += f", 삭제 누적 {removed_n}"
    lines = [line]
    if state.get("paused"):
        lines.append(f"   ⛔ 전체 정지 중 ({state['paused'].get('note', '')})")
    stopped = [f"r/{k}" for k, r in state["subs"].items() if r.get("stopped")]
    if stopped:
        lines.append(f"   ⛔ 정지 커뮤니티: {', '.join(stopped)}")
    if no_react:
        lines.append(f"   반응 기록 필요: {', '.join(no_react)}번 (`레딧 N 반응 <점수> [답글 <수>]`)")
    outside = [str(i["n"]) for i in recent if i.get("outside_standard")]
    if outside:
        lines.append(f"   ⚠️ 기준 밖 게시: {', '.join(outside)}번")
    open_subs = [k for k in {i["sub"] for i in state["items"]} if standing(state, k)["mode"] in ("active", "manual")]
    if not open_subs and not state.get("paused"):
        lines.append("   게시할 수 있는 커뮤니티 없음 (`레딧 기준`)")
    return lines


def weekly_message(now: datetime | None = None) -> str:
    """정해진 요일에 한 번 후보 찾기 (2026-11-02 사람 요청: 규칙 확인과 상관없이 매주). 전체 정지 중에는 안 찾는다."""
    social = importlib.import_module("pipeline.social")
    cfg, now = settings(), now or social.now_kst()
    state = load()
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if now.weekday() != int(cfg.get("find_weekday", 1)) or now.hour < int(cfg.get("find_hour", 10)) or state.get("found_week") == week:
        return ""
    if standing(state, "_")["mode"] == "paused":
        return ""
    state["found_week"] = week
    save(state)
    return find()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reddit 답변 준비")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("find", "list", "communities"):
        sub.add_parser(name)
    p = sub.add_parser("add")
    p.add_argument("url")
    p.add_argument("question")
    p = sub.add_parser("want")
    p.add_argument("url")
    p.add_argument("question", nargs="?", default="")
    p = sub.add_parser("allow")
    p.add_argument("sub")
    p.add_argument("--links", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    for name in ("asked", "reset", "resume"):
        sub.add_parser(name).add_argument("sub")
    p = sub.add_parser("mods")
    p.add_argument("sub")
    p.add_argument("decision", choices=["allowed", "refused"])
    p.add_argument("--links", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--note", default="")
    p = sub.add_parser("warn")
    p.add_argument("sub")
    p.add_argument("--note", default="")
    for name in ("draft", "show", "skip", "facts"):
        sub.add_parser(name).add_argument("n", type=int)
    for name in ("direct", "check"):
        p = sub.add_parser(name)
        p.add_argument("n", type=int)
        p.add_argument("text")
    p = sub.add_parser("done")
    p.add_argument("n", type=int)
    p.add_argument("url")
    p = sub.add_parser("removed")
    p.add_argument("n", type=int)
    p.add_argument("--note", default="")
    p = sub.add_parser("react")
    p.add_argument("n", type=int)
    p.add_argument("score", type=int)
    p.add_argument("--replies", type=int)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "find":
            print(find())
        elif args.cmd == "list":
            print(list_message())
        elif args.cmd == "communities":
            print(communities_message())
        elif args.cmd == "add":
            state = load()
            item = _add(state, args.url, args.question[:100], args.question)
            if not item:
                raise RedditError("추가하지 못했습니다: 주소가 틀렸거나, 이미 있거나, 제외·정지 커뮤니티이거나, 병원 추천을 묻는 글입니다")
            save(state)
            print(list_message([item], f"[Reddit {item['n']}번 추가]"))
        elif args.cmd in ("allow", "mods"):
            print(allow(args.sub, args.links, args.no_ai) if args.cmd == "allow" else mods(args.sub, args.decision, args.links, args.no_ai, args.note))
            for item in [i for i in load()["items"] if i.get("wanted") and i["sub"].lower() == _name(args.sub).lower()]:
                state = load()
                _item(state, item["n"])["wanted"] = False
                save(state)
                try:
                    print(send(draft(item["n"])))
                except RedditError as e:
                    print(f"⚠️ {item['n']}번: {e}")
        elif args.cmd == "asked":
            print(asked(args.sub))
        elif args.cmd == "reset":
            print(reset(args.sub))
        elif args.cmd == "resume":
            print(resume(args.sub))
        elif args.cmd == "warn":
            print(warn(args.sub, args.note))
        elif args.cmd == "want":
            print(want(args.url, args.question))
        elif args.cmd == "draft":
            print(send(draft(args.n)))
        elif args.cmd == "direct":
            print(send(draft(args.n, args.text)))
        elif args.cmd == "facts":
            print(send(notes(args.n)))
        elif args.cmd == "check":
            print(send(check(args.n, args.text)))
        elif args.cmd == "show":
            print(send(_item(load(), args.n)))
        elif args.cmd == "skip":
            state = load()
            _item(state, args.n)["status"] = "skipped"
            save(state)
            print(f"{args.n}번 제외")
        elif args.cmd == "done":
            print(done(args.n, args.url))
        elif args.cmd == "removed":
            print(removed(args.n, args.note))
        elif args.cmd == "react":
            print(react(args.n, args.score, args.replies))
    except RedditError as e:
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("reddit", main)
