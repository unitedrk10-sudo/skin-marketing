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

    python -m pipeline.reddit find | list | communities | rules [--force]
    python -m pipeline.reddit add <주소> "질문 내용"
    python -m pipeline.reddit brief <N> | draft <N> | direct <N> "운영자 답변 방향" | facts <N> | check <N> "운영자가 쓴 본문 (한국어면 번역)"
    python -m pipeline.reddit show <N> | skip <N> | done <N> <댓글 주소> | react <N> <점수> [--replies 2] | removed <N> [--note …]
    python -m pipeline.reddit allow <서브레딧> [--links] [--no-ai] [--mt]
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
from pipeline import machine_translate as mt
from pipeline import common
from pipeline.common import content_dir, get_logger, load_draft, load_env_file, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("reddit")

THREAD = re.compile(r"https://(?:www\.|old\.|new\.)?reddit\.com/(?:r|u|user)/([A-Za-z0-9_-]+)/(?:comments|s)/([a-z0-9]+)(?:/[^\s?#)\]]*)?", re.I)  # /s/ = 앱의 공유 링크
COMMENT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com")
# 병원 질문 (이름·추천·"어떻게 고르나요?"). 거르지 않고 이름 없이 고르는 기준으로 답한다 (2026-11-05 운영자 결정)
CLINIC_ASK = re.compile(r"\b(which|best|good|great|reputable|trusted|trustworthy|recommend\w*|suggest\w*)\b[^.?!\n]{0,30}\b(clinics?|dermatologists?|derms?|doctors?|hospitals?|surgeons?)\b"
                        r"|\b(clinics?|dermatologists?|derms?|doctors?|surgeons?)\b[^.?!\n]{0,20}\b(recommendations?|recs?|suggestions?)\b"
                        r"|\bany\s+(?:good\s+)?(clinics?|derms?|dermatologists?|surgeons?)\b|\bwhat\s+(clinic|hospital|derm\w*)\b", re.I)
HOW_TO_CHOOSE = re.compile(r"\bhow\b[^.?!\n]{0,25}\b(choose|pick|find|vet|select|evaluate|tell|know|check|spot|avoid|research)\b"
                           r"|\bwhat\b[^.?!\n]{0,20}\blook\s+(?:for|out)\b|\bred\s+flags?\b|\bwarning\s+signs?\b|\bquestions?\s+(?:to|should\s+I)\s+ask\b"
                           r"|\bcriteria\b|\btips?\s+(?:for|on)\s+(?:choosing|picking|finding|vetting)\b", re.I)


def clinic_question(text: str) -> bool:
    """병원을 묻거나 고르는 법을 묻는 글. 답은 이름 없이 고르는 기준(등록 명단·면허·견적서 등) — 의료 사실이 아니라 사실 번호 없이도 된다."""
    return bool(CLINIC_ASK.search(text) or HOW_TO_CHOOSE.search(text))


def prompt(name: str, **values) -> str:
    """Reddit 프롬프트는 블로그 규칙(_rules.md) 대신 Reddit 규칙(_reddit_rules.md)을 쓴다 (2026-11-04 운영자 요청)."""
    return common.prompt(name, rules=common.read_prompt("_reddit_rules"), **values)
ID_ANCHOR, ID_ANCHOR_DATE = "1nm0000", date(2025, 9, 20)
ID_PER_DAY = (int("1nm0000", 36) - int("1cx0000", 36)) / 487  # 487일 = 2024-05-21 ~ 2025-09-20
# 운영자 공개 문구 — 코드가 항상 붙이고 설정으로 끌 수 없다. "AI-assisted" 줄만 설정(disclosure_ai_label)을 따른다 (2026-11-02)
OPERATOR_LINE = "Disclosure: I run Skinbound Korea, an information site about skin treatments in Korea."
AI_LINE = "AI-assisted and human-checked."
END_LINE = "Not medical advice."
DISCLOSURE = f"{OPERATOR_LINE} {AI_LINE} {END_LINE}"           # AI 가 쓴 글
DISCLOSURE_OPERATOR = f"{OPERATOR_LINE} {END_LINE}"           # 운영자가 직접 쓴 글
TRANSLATED_LINE = "Written in Korean and machine-translated."  # 운영자가 한국어로 쓰고 Google 번역한 글

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
    links = mode in ("active", "manual") and bool(rec.get("links"))  # 링크는 운영자가 켤 때만 (2026-11-03, 조사 결과로는 안 붙임)
    if treatments_banned(c, rec, research):
        why.append("시술 주제 금지 (화장품 글만 근거)")
    return {**_st(mode, why), "links": links, "writer": writer, "treatments_banned": treatments_banned(c, rec, research),
            "links_from": "human" if links else "",
            "mt": mode == "active" or bool(rec.get("mt_ok"))}


def _st(mode: str, why: list[str]) -> dict:
    return {"mode": mode, "label": MODE_LABEL[mode], "why": [w for w in why if w], "links": False, "writer": None, "links_from": "", "mt": False,
            "treatments_banned": False}


def treatments_banned(c: dict, rec: dict, research: dict) -> bool:
    """시술·성형 주제를 막는 커뮤니티 (2026-11-02 검토 항목 추가, r/AsianBeauty 규칙 7). 그곳에서는 화장품 글만 근거로 쓴다."""
    return c.get("treatments") == "banned" or research.get("treatments") == "banned" or bool(rec.get("treatments_banned"))


def check_marks(state: dict, sub: str) -> dict[str, str]:
    """검토 항목 세 가지 — AI 글·링크·시술 주제. ○ 허용 / △ 조건부 / ✕ 금지 / ? 모름. 사람 기록이 조사보다 먼저."""
    c, rec = _community(sub), _record(state, sub)
    research = rec.get("research") or {}
    def mark(human_ban: bool, human_ok: bool, *values) -> str:
        if human_ban:
            return "✕"
        if human_ok:
            return "○"
        for v in values:
            if v in ("banned", "limited", "allowed"):
                return {"banned": "✕", "limited": "△", "allowed": "○"}[v]
        return "?"
    read = bool(rec.get("read") or rec.get("mods") == "allowed")
    return {"AI": mark(bool(rec.get("ai_banned")), False, c.get("ai_text"), research.get("ai_text")),
            "링크": mark(False, bool(rec.get("links")), c.get("links"), research.get("links")),
            "시술": mark(bool(rec.get("treatments_banned")), read and not rec.get("treatments_banned") and c.get("treatments") != "banned"
                       and research.get("treatments") != "banned", c.get("treatments"), research.get("treatments"))}


def _researched(c: dict) -> bool:
    """설정에 조사 결과(note·ai_text)가 있는가. 단계만 적힌 곳은 규칙을 모르는 곳으로 본다."""
    return bool(c.get("note") or c.get("ai_text"))


def _blocked_sub(state: dict, sub: str) -> bool:
    return standing(state, sub)["mode"] in ("stopped", "excluded")


def can_post(st: dict, written_by: str) -> bool:
    """지금 기준으로 이 글을 게시해도 되는가. 직접 작성 단계에서는 운영자가 쓴 글만."""
    return st["mode"] == "active" or (st["mode"] == "manual" and written_by == "operator")


def disclosure(written_by: str, translated: bool = False) -> str:
    label = settings().get("disclosure_ai_label", "ai_only")
    ai = label == "always" or (label == "ai_only" and written_by != "operator")
    base = DISCLOSURE if ai else DISCLOSURE_OPERATOR
    return base.replace(f" {END_LINE}", f" {TRANSLATED_LINE} {END_LINE}") if translated else base


# ── 후보 ─────────────────────────────────────────────────────

def _add(state: dict, url: str, title: str, question: str, posted: str = "", any_sub: bool = False) -> dict | None:
    m = THREAD.match(url or "")
    if not m or any(i["thread"] == m.group(2).lower() for i in state["items"]):
        return None
    sub = m.group(1)
    if _blocked_sub(state, sub) and not any_sub:
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
                        "facts": social.social_facts(load_draft(post["path"])), "skincare": draft.get("content_type") == "skincare"})
    return out


def _posts_for(st: dict, sub: str = "") -> list[dict]:
    """시술 주제를 막는 커뮤니티에서는 화장품 글만 근거로."""
    posts = _live_posts()
    if not st.get("treatments_banned"):
        return posts
    posts = [p for p in posts if p.get("skincare")]
    if not posts:
        raise RedditError(f"r/{sub} 은 시술 주제 금지라 화장품 글만 근거로 쓸 수 있는데, 게시된 화장품 글이 없습니다")
    return posts


def focus_subs() -> list[str]:
    """보는 커뮤니티 (2026-11-03 운영자 결정: 본거지·보조만). 비어 있으면 제한 없음."""
    return [str(s).removeprefix("r/") for s in (settings().get("focus") or [])]


def in_focus(sub: str) -> bool:
    focus = {s.lower() for s in focus_subs()}
    return not focus or sub.lower() in focus


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
                                                      excluded=", ".join(_skip_subs(state)) or "(none)",
                                                      only=", ".join(f"r/{s}" for s in focus_subs()) or "any subreddit"))
    added = []
    for c in data.get("threads", []) if isinstance(data, dict) else []:
        m = THREAD.match(c.get("url", ""))
        if m and in_focus(m.group(1)) and not too_old(m.group(2)):  # 보는 커뮤니티 밖의 글은 버린다
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
    items = [i for i in state["items"] if i["status"] in ("candidate", "briefed", "ready", "blocked", "manual")] if items is None else items
    if not items:
        return "대기 중인 Reddit 후보가 없습니다"
    label = {"candidate": "정리 전", "briefed": "정리됨 · 작성 대기", "ready": "붙여넣기 준비됨", "blocked": "검수 걸림", "manual": "직접 작성 대기"}
    lines = [head + " 글을 열어 날짜와 내용을 확인해 주세요 (AI 검색 결과라 틀릴 수 있음)"]
    for i in items:
        practice = " · 연습" if i.get("practice") and i["status"] == "ready" else ""
        lines += ["", f"{i['n']}. {i['title']} ({label.get(i['status'], i['status'])}{practice}{', ' + i['posted'] if i.get('posted') else ''})",
                  f"   {i['question'][:200]}", f"   {_sub_line(state, i['sub'])}", f"   {i['url']}"]
    lines += ["", "정리: `레딧 1 정리` → 한국어로 써서 `레딧 1 검사: 본문` · 제외: `레딧 1 빼기` · 기준: `레딧 기준`"]
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
                    "operators_banned": bool(data.get("operators_banned")), "ai_text": data.get("ai_text", "unknown"),
                    "links": data.get("links", "unknown"), "treatments": data.get("treatments", "unknown"),
                    "sources": [g.get("url", "") for g in res.grounding_urls][:5]}
        break
    return {"found": False, "checked": date.today().isoformat()}


def _ensure_research(state: dict, sub: str) -> None:
    rec = _record(state, sub)
    if _researched(_community(sub)) or rec.get("research") or rec.get("read") or rec.get("mods"):
        return
    state["subs"].setdefault(sub.lower(), {})["research"] = look_up_rules(sub)
    save(state)


RESEARCH_FIELDS = {"self_promotion": "홍보", "operators_banned": "운영자 글", "links": "링크", "ai_text": "AI 글", "treatments": "시술 주제"}
VALUE_KO = {"banned": "금지", "limited": "제한", "allowed": "허용", "unknown": "모름", True: "금지", False: "금지 아님", None: "기록 없음"}


def _all_subs(state: dict) -> list[str]:
    names = {n.lower(): n for n in (settings().get("communities") or {})}
    for key in state["subs"]:
        names.setdefault(key, next((i["sub"] for i in state["items"] if i["sub"].lower() == key), key))
    return sorted(names.values(), key=str.lower)


def refresh_rules(force: bool = False) -> list[str]:
    """주 1회 규칙 다시 조사 (2026-11-02 운영자 요청). 아는 커뮤니티(제외·정지 빼고) 전부를 Gemini 검색으로 다시 보고, 홍보·운영자 글·링크·AI 글
    판정이 바뀐 곳과 단계가 바뀐 곳만 돌려준다. 못 찾으면 전 조사를 그대로 둔다. 조사 결과의 "링크 허용"은 표시만 하고, 링크는 운영자가 켤 때만 붙인다 (2026-11-03)."""
    state = load()
    cutoff = (date.today() - timedelta(days=int(settings().get("rules_refresh_days", 7)))).isoformat()
    lines = []
    for sub in focus_subs() or _all_subs(state):  # 보는 커뮤니티가 정해져 있으면 그곳만
        before = standing(state, sub)
        if before["mode"] in ("excluded", "stopped", "paused"):
            continue
        rec = state["subs"].setdefault(sub.lower(), _record(state, sub))
        old = rec.get("research") or {}
        if not force and old.get("checked", "") > cutoff:
            continue
        new = look_up_rules(sub)
        if not new.get("found"):
            if old.get("found"):
                old["refresh_failed"] = date.today().isoformat()
            else:
                rec["research"] = new
            continue
        changes = [f"{label} {VALUE_KO.get(old.get(k), old.get(k))}→{VALUE_KO.get(new.get(k), new.get(k))}"
                   for k, label in RESEARCH_FIELDS.items() if old.get("found") and old.get(k) != new.get(k)]
        rec["research"] = new
        after = standing(state, sub)
        if changes or not old.get("found") or after["mode"] != before["mode"]:
            lines.append(f"r/{sub}: " + ", ".join(changes or ["첫 조사"])
                         + (f" (단계 {before['label']}→{after['label']})" if after["mode"] != before["mode"] else "")
                         )
    save(state)
    return lines


def _rule_lines(rule: dict) -> str:
    return "\n".join([f"- {r}" for r in rule.get("rules_ko", [])] + ([rule["url"]] if rule.get("url") else []))


# ── 사실·초안·검사 ───────────────────────────────────────────

def _facts_block(posts: list[dict], researched: list[dict] | None = None) -> tuple[str, dict[str, dict]]:
    """researched: 이 글을 위해 조사한 출처의 사실(`research`). 우리 글이 아니라 post 가 None — 관련 글 링크로 쓰지 않는다."""
    by_id, out = {}, []
    for p_no, post in enumerate(posts, 1):
        out.append(f"\nPOST P{p_no}: {post['title']}")
        for f in post["facts"]:
            key = f"P{p_no}:{f['id']}"
            by_id[key] = {**f, "post": post}
            out.append(f"[{key}] {f.get('text', '')}")
    if researched:
        out.append("\nRESEARCHED FOR THIS THREAD (other sources, each checked against its page):")
        for f in researched:
            key = f"R:{f['id']}"
            by_id[key] = {**f, "post": None}
            out.append(f"[{key}] {f.get('text', '')}")
    return "\n".join(out), by_id


def _review_facts(used: list[dict], by_id: dict[str, dict]) -> str:
    """검수 근거: 인용한 사실이 있는 글의 사실 전부 (쓴 쪽이 인용 번호를 빠뜨린 문장도 대조되게)."""
    titles = {f["post"]["title"] for f in used if f.get("post")}
    return "\n".join(f"- {f.get('text', '')}" for f in by_id.values()
                     if (f.get("post") and f["post"]["title"] in titles) or (not f.get("post") and f in used))


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
    facts, by_id = _facts_block(_posts_for(st, item["sub"]), (item.get("research") or {}).get("facts"))
    social = importlib.import_module("pipeline.social")
    numbers = _numbers(by_id, item["question"])
    problems: list[str] = []
    stage = "reddit_direct" if direction else "reddit_answer"
    choosing = clinic_question(f"{item['title']}\n{item['question']}")
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
        if not used and not direction and not choosing:  # 병원 고르는 기준(등록 명단·면허 확인 등)은 의료 사실이 아니라 근거 없이도 된다
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
    ours = next((f["post"]["url"] for f in used if f.get("post")), "")  # 관련 글 링크는 우리 글에서만
    _finish(item, problems, body, ours if st["links"] and not item["practice"] else "")
    save(state)
    return item


def _finish(item: dict, problems: list[str], body: str, link: str) -> None:
    if problems:
        item.update(status="blocked", paste="")
        return
    parts = [body] + ([f"More with sources: {link}"] if link else []) + [disclosure(item.get("written_by", "ai"), bool(item.get("translated")))]
    item.update(status="ready", paste="\n\n".join(parts))
    item["seal"] = seal_of(item)


def brief(n: int) -> dict:
    """운영자에게만 보내는 한국어 정리 (2026-11-03 운영자 요청): 질문 요약, 우리 글에 있는 사실(원문 그대로), 관련 글, 없는 부분,
    작성 권고. 게시하는 글이 아니라 커뮤니티 규칙(AI 글 금지 등)과 상관없이 만든다. 운영자는 이걸 보고 한국어로 써서 `레딧 N 검사:` 로 보낸다."""
    state = load()
    item = _item(state, n)
    posts = _live_posts()
    facts, by_id = _facts_block(posts)
    data, _r = llm.generate_json("reddit_brief", prompt("reddit_brief", subreddit=item["sub"], title=item["title"],
                                                        question=item["question"], facts=facts or "(none)"))
    data = data if isinstance(data, dict) else {}
    covered = [c for c in (data.get("covered") or []) if isinstance(c, dict) and c.get("fact_id") in by_id][:8]
    rel = data.get("related_post", "")
    post = posts[int(rel[1:]) - 1] if re.fullmatch(r"P\d+", rel or "") and 0 < int(rel[1:]) <= len(posts) else None
    item["brief"] = {"summary": data.get("summary_ko", ""), "asks": [str(a) for a in (data.get("asks_ko") or [])][:6],
                     "covered": [{"id": c["fact_id"], "why": c.get("why_ko", ""), "text": by_id[c["fact_id"]].get("text", ""),
                                  "url": by_id[c["fact_id"]].get("url", "")} for c in covered],
                     "related": post["url"] if post else "", "related_title": post["title"] if post else "",
                     "gaps": [str(g) for g in (data.get("gaps_ko") or [])][:6], "consider": [str(c) for c in (data.get("consider_ko") or [])][:8],
                     "skip": data.get("skip_ko", ""), "at": now_iso()}
    if item["status"] not in ("posted", "removed"):
        item["status"] = "briefed"
    save(state)
    return item


def brief_message(item: dict) -> str:
    b = item.get("brief") or {}
    st = standing(load(), item["sub"])
    lines = [f"[Reddit {item['n']}번 정리] r/{item['sub']}: {item['title']}", item["url"] or "(주소 없음 — 게시한 뒤 `레딧 N 완료 <댓글 주소>` 로 기록)"]
    if item["sub"] == "unknown":
        lines.append(f"❔ 어느 커뮤니티 글인지 못 찾았습니다: `레딧 {item['n']} 커뮤니티 KoreaSeoulBeauty` 처럼 알려 주세요 (번역·게시 기준에 필요)")
    elif item.get("guessed_sub"):
        lines.append(f"❔ 본문에 커뮤니티 이름이 없어 본거지 r/{item['sub']} 글로 봤습니다. 다른 곳이면 `레딧 {item['n']} 커뮤니티 <이름>`")
    if b.get("skip"):
        lines += ["", f"⚠️ 답하지 않는 게 좋습니다: {b['skip']}"]
    lines += ["", f"📝 질문: {b.get('summary', '')}"] + [f"- {a}" for a in b.get("asks", [])]
    if b.get("covered"):
        lines += ["", "✅ 우리 글에 있는 내용 (원문 그대로, 숫자는 옮기지 말 것):"]
        for c in b["covered"]:
            lines += [f"- {c['text']}", f"  → {c['why']}" if c.get("why") else "", f"  {c['url']}" if c.get("url") else ""]
    else:
        lines += ["", "✅ 우리 글에 바로 맞는 내용은 없습니다"]
    if b.get("related"):
        lines += ["", f"🔗 관련 글: {b.get('related_title', '')} {b['related']}" + ("" if st["links"] else " (본문 링크는 켤 때만)")]
    if b.get("gaps"):
        lines += ["", "❓ 우리 글에 없는 부분:"] + [f"- {g}" for g in b["gaps"]]
    if b.get("consider"):
        lines += ["", "💡 쓸 때 고려할 점:"] + [f"- {c}" for c in b["consider"]]
    how = "한국어로 써도 됩니다 (Google 번역)" if st["mt"] else "영어로 써 주세요 (이 커뮤니티는 번역 허용 기록 없음)"
    if will_draft(item, st):
        lines += ["", "✍️ 이어서 ② 조사(출처 찾기·사실 확인) → ③ 초안을 보냅니다. 직접 쓰고 싶으면 "
                      f"`레딧 {item['n']} 검사: 본문` — {how}. 법적 문제·숫자 문장만 빼고 공개 문구를 붙입니다."]
    else:
        lines += ["", f"✍️ 직접 써서 보내 주세요: `레딧 {item['n']} 검사: 본문` — {how}. 법적 문제·숫자 문장만 빼고 공개 문구를 붙입니다."]
    if st["mode"] in BLOCKED_MODES or not can_post(st, "operator"):
        lines.append(f"(참고: r/{item['sub']} 은 지금 {st['label']} 단계라 게시용 글은 만들지 않습니다)")
    return "\n".join(x for x in lines if x != "")


RESEARCH_PAGES = 6      # 조사 때 읽는 페이지 수
RESEARCH_CHARS = 8000   # 페이지당 넘기는 본문 길이


def research(n: int) -> dict:
    """질문 조사 (2026-11-05 운영자 요청): Gemini 검색으로 출처 페이지 후보 → 받아 오기(병원·대행사 사이트 제외, 02_draft.fetch_sources)
    → Claude 가 페이지에서 사실과 원문 인용을 뽑고 → 인용이 페이지에 실제로 있는 사실만 남긴다(02_draft.verify_facts). 초안이 우리 글 사실과 함께 쓴다."""
    state = load()
    item = _item(state, n)
    drafting = importlib.import_module("pipeline.02_draft")
    b = item.get("brief") or {}
    gaps = "\n".join(f"- {g}" for g in (b.get("gaps") or []) + (b.get("asks") or [])) or "(not known — research the whole question)"
    data, result = llm.generate_json("reddit_research", prompt("reddit_research", today=date.today().isoformat(), subreddit=item["sub"],
                                                               title=item["title"], question=item["question"], gaps=gaps))
    picked = [{"url": str(x.get("url", "")).strip(), "title": x.get("title", "")}
              for x in ((data or {}).get("sources") or []) if isinstance(x, dict)] if isinstance(data, dict) else []
    grounded = [{"url": g["url"], "title": g.get("title", "")} for g in (result.grounding_urls if result else [])]
    sources = drafting.fetch_sources(picked + grounded)[:RESEARCH_PAGES]
    facts, missing = [], []
    if sources:
        pages = "\n\n".join(f"[{x['id']}] {x['title'] or x['url']}\nURL: {x['url']}\n<page>\n{x['text'][:RESEARCH_CHARS]}\n</page>" for x in sources)
        raw, _r = llm.generate_json("reddit_research_read", prompt("reddit_research_read", subreddit=item["sub"], title=item["title"],
                                                                   question=item["question"], pages=pages))
        missing = [str(m) for m in ((raw or {}).get("missing_ko") or [])][:6] if isinstance(raw, dict) else []
        raw = (raw or {}).get("facts", []) if isinstance(raw, dict) else []
        facts = [f for f in drafting.verify_facts(raw, sources) if f.get("kind") != "practice"]
    state = load()
    item = _item(state, n)
    item["research"] = {"facts": [{"id": f["id"], "text": f["text"], "url": f["url"], "title": f.get("source_title", "")} for f in facts],
                        "missing": missing, "pages": len(sources), "at": now_iso()}
    save(state)
    return item


def research_message(item: dict) -> str:
    """보강이 필요한 내용만 (2026-11-05 운영자 요청 — 출처를 하나하나 설명하지 않는다)."""
    r = item.get("research") or {}
    head = f"[Reddit {item['n']}번 조사]"
    if not r.get("facts"):
        lines = [f"{head} 쓸 만한 자료를 찾지 못해 우리 글 내용으로만 씁니다."]
    else:
        lines = [f"{head} 확인된 내용 {len(r['facts'])}개를 초안에 넣었습니다."]
    lines += (["", "보강 필요:"] + [f"- {m}" for m in r["missing"]]) if r.get("missing") else ["보강 필요한 내용은 없습니다."]
    return "\n".join(lines)


def _deliver(text: str) -> str:
    worker = importlib.import_module("pipeline.worker")
    target = worker.reddit_target()
    return "✅ 보냈습니다" if (worker.hermes_send(text, target=target) if target else worker.hermes_send(text)) else text


def will_draft(item: dict, st: dict) -> bool:
    """정리 뒤에 초안까지 이어서 쓰는가 (2026-11-03 운영자 요청). AI 글을 올릴 수 있는 활동 단계에서만, 답하지 말라는 정리가 아닐 때."""
    return (bool(settings().get("auto_draft", True)) and st["writer"] == "post" and item["sub"] != "unknown"
            and not (item.get("brief") or {}).get("skip"))


def no_draft_reason(item: dict, st: dict) -> str:
    """정리 뒤 초안을 이어서 쓰지 않는 이유 (운영자가 '초안이 안 온다'고 헷갈리지 않게, 2026-11-05)."""
    if not settings().get("auto_draft", True):
        return "설정에서 자동 초안이 꺼져 있습니다 (`config/reddit.yaml auto_draft`)"
    if item["sub"] == "unknown":
        return f"어느 커뮤니티 글인지 몰라서입니다 — `레딧 {item['n']} 커뮤니티 KoreaSeoulBeauty` 처럼 알려 준 뒤 `레딧 {item['n']} 초안`"
    if (item.get("brief") or {}).get("skip"):
        return "정리에서 답하지 않는 게 좋다고 봤습니다"
    if st["writer"] != "post":
        return (f"r/{item['sub']} 이 지금 '{st['label']}' 단계라서입니다 (초안은 활동 단계만) — `레딧 기준` 으로 확인, "
                f"원문을 읽었으면 `레딧 규칙확인 {item['sub']}`")
    return ""


def continue_draft(n: int) -> list[str] | str:
    """② 조사 → ③ 초안. 조사는 어떤 이유로 실패해도 우리 글로 초안을 쓴다."""
    out = []
    try:
        out.append(_deliver(research_message(research(n))))
    except Exception as e:  # noqa: BLE001 — 검색·페이지 받기·JSON 오류 모두 초안을 막지 않게
        log.warning("Reddit %s번 조사 실패: %s: %s", n, type(e).__name__, e)
        out.append(_deliver(f"[Reddit {n}번 조사] 조사를 하지 못해 우리 글 내용으로만 씁니다 ({type(e).__name__})"))
    try:
        out.append(send(draft(n)))
    except RedditError as e:
        out.append(f"초안은 만들지 않았습니다: {e}")
    except llm.LLMError as e:
        log.warning("Reddit %s번 초안 실패: %s", n, e)
        out.append(f"초안을 만들지 못했습니다 (나중에 `레딧 {n} 초안`): {str(e)[:120]}")
    rest = [o for o in out if o != "✅ 보냈습니다"]
    return "\n\n".join(rest) if rest else "✅ 보냈습니다"


def brief_and_draft(n: int) -> str:
    """정리를 보내고, 활동 단계면 초안도 보낸다. 초안이 안 되면(한도·답할 사실 없음·LLM 실패) 정리는 그대로 두고 이유만 알린다."""
    item = brief(n)
    out = [send(item)]
    if will_draft(item, standing(load(), item["sub"])):
        out.append(continue_draft(n))
    else:
        why = no_draft_reason(item, standing(load(), item["sub"]))
        if why:
            out.append(_deliver(f"[Reddit {n}번] 초안은 만들지 않았습니다: {why}"))
    rest = [o for o in out if o != "✅ 보냈습니다"]
    return "\n\n".join(rest) if rest else "✅ 보냈습니다"


def notes(n: int) -> dict:
    """사실 정리: 직접 쓸 운영자에게 질문에 맞는 사실을 원문 그대로 준다. Claude(reddit_facts)는 고르기만 하고 사실 문장은 코드가 목록에서 꺼낸다."""
    state, item, st = _prepare(n)
    facts, by_id = _facts_block(_posts_for(st, item["sub"]))
    data, _r = llm.generate_json("reddit_facts", prompt("reddit_facts", subreddit=item["sub"], title=item["title"], question=item["question"], facts=facts))
    ids = [k for k in (data.get("fact_ids") or []) if k in by_id][:8] if isinstance(data, dict) else []
    if not ids or not data.get("answerable", True):
        item.update(status="blocked", problems=[f"우리 글로 답할 수 없음: {(data or {}).get('reason', '맞는 사실 없음')}"], paste="", note_ids=[])
    else:
        item.update(status="manual", note_ids=ids, gaps=data.get("gaps_ko", ""), problems=[], paste="",
                    practice=not can_post(st, "operator"))
    save(state)
    return item


SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+[\"')\]]*|$)")
MIN_LEFT_WORDS = 5


def _sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in SENTENCE.finditer(text) if m.group(0).strip()]


def legal_hits(sentence: str) -> list[str]:
    """운영자가 쓴 문장에서 코드로 잡는 법적 문제 (의료법 §56·화장품법 §13·병원 알선 시비). 사실 목록 대조·수치 검사는 하지 않는다."""
    review = importlib.import_module("pipeline.02b_auto_review")
    out = []
    for term, pat in review.load_banned():
        m = pat.search(sentence)
        if m and not (review.NEGATABLE.search(m.group(0)) and review.NEGATION.search(sentence[:m.start()])):
            out.append(f"과장·보장·비교 표현: {term}")
    for rule, label, pat in review.RULE_PATTERNS:
        m = pat.search(sentence)
        if not m or (rule == "clinic_name" and (review.INSTITUTION.search(m.group(0)) or review._generic(m.group(0)))):
            continue
        if rule == "testimonial" and not re.search(rf"\b{TREATMENT}\b", sentence, re.I):
            continue  # 화장품·여행 개인 경험은 운영자 본인 의견. 시술 경험담만 (의료법 §56②2 치료경험담)
        out.append(label)
    out += [f"시술·주사제 추천: {m.group(0)}" for m in PROMOTES_TREATMENT.finditer(sentence)]
    for m in review.COSMETIC_PATTERNS.finditer(sentence):
        if not review.NEGATION.search(sentence[max(0, m.start() - 30):m.start()]):
            out.append(f"화장품을 의약품처럼 표현: {m.group(0)}")
    return out


# 운영자가 쓴 글에는 숫자(기간·횟수·용량·%)를 쓰지 않는다 — 사실 대조를 하지 않으니 틀린 숫자를 걸러낼 방법이 없다 (2026-11-02 운영자 결정)
NUMBER_WORDS = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|hundred|a\s+couple\s+of|a\s+few|several|half\s+an?)"
UNITS = r"(?:seconds?|minutes?|mins?|hours?|hrs?|days?|nights?|weeks?|months?|years?|sessions?|treatments?|times?|shots?|units?|cc|ml|mg|percent)"
HAS_NUMBER = re.compile(rf"\d|%|\b{NUMBER_WORDS}\s+(?:more\s+|full\s+)?{UNITS}\b|\b(?:once|twice)\b", re.I)


def _related_block(posts: list[dict]) -> str:
    return "\n".join(f"P{i}: {p['title']}" for i, p in enumerate(posts, 1)) or "(none)"


def check(n: int, text: str) -> dict:
    """운영자가 직접 쓴 영문 본문 — 가볍게 검사한다 (2026-11-02 운영자 요청). 운영자 본인의 말이라 사실 목록 대조는 하지 않고,
    ① 코드 규칙(병원·의사명, 연락처, 과장·보장·비교, 시술 추천, 시술 경험담, 화장품 의약품 오인)에 걸린 문장과
    ② Gemini(reddit_light_check)가 일반 의학 지식으로 본 명백히 틀린 의료 내용·법적 문제 문장만 **지운다**.
    AI 가 단어를 보태지 않도록 고쳐 쓰지 않고 원문 문장을 빼기만 한다 (AI 글 금지 커뮤니티에서도 운영자 글로 남게).
    관련된 우리 글이 있으면 링크 — 본문에는 링크 허용 기록이 있는 커뮤니티에서만 붙이고, 아니면 안내 메시지에만 보여 준다."""
    cfg = settings()
    state, item, st = _prepare(n)
    body = text.strip()
    if not body:
        raise RedditError("검사할 본문이 없습니다: `레딧 N 검사: 영어 본문`")
    practice = not can_post(st, "operator")
    if not practice:
        _check_limits(state, item["sub"])
    original_ko, translated = "", False
    if mt.is_korean(body):  # 한국어로 쓴 글 → Google 번역 (LLM 이 아닌 기계 번역, 번역 허용된 곳만)
        if not st["mt"]:
            raise RedditError(f"r/{item['sub']} 은 번역 허용 기록이 없어 한국어 글을 번역하지 않았습니다. 영어로 써 주시거나, "
                              f"규칙·운영진 답변에서 번역이 괜찮다고 확인되면 `레딧 운영진답변 {item['sub']} 허용 번역허용`")
        try:
            original_ko, body, translated = body, mt.to_english(body), True
        except mt.TranslateError as e:
            raise RedditError(f"{e}. Google 번역으로 직접 옮겨 영어로 보내 주세요") from None
    removed: list[dict] = []
    keep = body
    for sentence in _sentences(body):
        hits = legal_hits(sentence) + (["숫자 (기간·횟수·용량은 쓰지 않음)"] if HAS_NUMBER.search(sentence) else []) \
            + (["이 커뮤니티는 시술 주제 금지"] if st["treatments_banned"] and re.search(rf"\b{TREATMENT}\b|plastic surgery|cosmetic surgery|procedure", sentence, re.I) else [])
        if hits:
            removed.append({"sentence": sentence, "why": "; ".join(dict.fromkeys(hits))})
            keep = keep.replace(sentence, "", 1)
    keep = re.sub(r"https?://\S+", "", keep)  # 링크는 코드가 붙인다
    posts = _posts_for(st, item["sub"])
    review, _r = llm.generate_json("reddit_light_check", prompt("reddit_light_check", question=item["question"], answer=_tidy(keep),
                                                                posts=_related_block(posts)))
    review = review if isinstance(review, dict) else {}
    problems = []
    for issue in review.get("remove", []) or []:
        sentence, why = (issue.get("sentence", ""), issue.get("why", "")) if isinstance(issue, dict) else (str(issue), "")
        if sentence and sentence in keep:
            keep = keep.replace(sentence, "", 1)
            removed.append({"sentence": sentence, "why": why})
        elif sentence:
            problems.append(f"직접 고쳐 주세요: \"{sentence}\" — {why}")
    keep = _tidy(keep)
    if len(keep.split()) < MIN_LEFT_WORDS:
        problems.append("문제 문장을 빼고 나니 남는 내용이 거의 없습니다")
    rel = review.get("related_post", "")
    post = posts[int(rel[1:]) - 1] if re.fullmatch(r"P\d+", rel or "") and 0 < int(rel[1:]) <= len(posts) else None
    item.update(written_by="operator", direction="", body=keep, original=original_ko or body, translated=translated, removed=removed,
                ko=review.get("ko", ""),
                problems=problems, practice=practice, related=post["url"] if post else "",
                long=len(keep.split()) > int(cfg.get("max_words", 180)), sources=[])
    _finish(item, problems, keep, post["url"] if post and st["links"] and not practice else "")
    save(state)
    return item


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,!?])", r"\1", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()


# ── 메시지 ───────────────────────────────────────────────────

def paste_messages(item: dict) -> list[str]:
    """[안내, 붙여넣을 본문]. 본문 메시지에는 다른 글자를 넣지 않는다. 지금 기준으로 게시할 수 없는 글은 메시지 한 개."""
    head = f"[Reddit {item['n']}번] r/{item['sub']}: {item['title']}"
    if item["status"] == "blocked":
        return [f"{head}\n⛔ 붙여넣을 글을 만들지 않았습니다\n" + "\n".join(f"- {p}" for p in item.get("problems", []))
                + (f"\n고쳐서 다시: `레딧 {item['n']} 검사: 본문`" if item.get("written_by") == "operator"
                   else f"\n직접 써서: `레딧 {item['n']} 검사: 본문` · 방향만 주고 다시: `레딧 {item['n']} 방향: 원하는 내용`")]
    state = load()
    st = standing(state, item["sub"])
    if item["status"] == "manual":
        return [notes_message(item, st)]
    if item["status"] == "briefed":
        return [brief_message(item)]
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
    if item.get("written_by", "ai") == "ai":
        meta += ["", f"✏️ 다르게 쓰려면 `레딧 {item['n']} 방향: 원하는 내용(한국어 가능)` — 그 방향으로 다시 씁니다"]
    if item.get("written_by") == "operator":
        meta += ["", "✂️ 뺀 문장: " + ("없음" if not item.get("removed") else "")] + [f"- \"{r['sentence']}\" — {r['why']}" for r in item.get("removed", [])]
        if item.get("translated"):
            meta += ["", "🌐 한국어 원문을 Google 번역했습니다. 번역문이 뜻과 맞는지 확인해 주세요 (공개 문구에 번역 표시가 붙습니다)"]
        if item.get("related") and item["related"] not in item["paste"]:
            meta += ["", f"🔗 관련 글: {item['related']}", f"링크는 켜 두신 커뮤니티에서만 본문에 붙습니다. 켜려면 `레딧 규칙확인 {item['sub']} 링크허용`"]
        if item.get("long"):
            meta.append(f"(참고: {settings().get('max_words', 180)}단어가 넘습니다)")
    research = _record(state, item["sub"]).get("research") or {}
    if research.get("found"):
        meta += ["", f"📜 r/{item['sub']} 규칙 (AI 검색 요약)", _rule_lines(research)]
    return ["\n".join(meta), item["paste"]]


def notes_message(item: dict, st: dict) -> str:
    _facts, by_id = _facts_block(_posts_for(st, item["sub"]))
    lines = [f"[Reddit {item['n']}번] r/{item['sub']}: {item['title']}", item["url"], "",
             f"✍️ 직접 작성 — r/{item['sub']}: {st['label']}" + (f" ({'; '.join(st['why'])})" if st["why"] else "")
             + ("\n🧪 지금은 게시용 아님 (연습)" if item.get("practice") else ""),
             "AI 가 쓴 글을 올리지 않는 곳입니다. 아래 사실을 참고해 직접 써서 보내 주세요: "
             f"`레딧 {item['n']} 검사: 본문`" + (" (한국어로 써도 됩니다 — Google 번역)" if st.get("mt") else " (영어로 — 이 커뮤니티는 번역 허용 기록 없음)"),
             "⚠️ 숫자(기간·횟수·용량·%)는 쓰지 마세요. \"며칠\" 대신 \"for a while\", \"the period your doctor gives you\"처럼. 숫자가 든 문장은 뺍니다.",
             "검사는 가볍게 합니다: 법적으로 문제 될 문장과 명백히 틀린 의료 내용만 빼고(고쳐 쓰지 않음), "
             "관련 글 링크와 공개 문구는 코드가 붙입니다.", "", "참고 사실 (원문 그대로, 숫자는 옮기지 말 것):"]
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
    worker = importlib.import_module("pipeline.worker")
    target = worker.reddit_target()  # Reddit 전용 방이 있으면 그쪽으로
    if all(worker.hermes_send(m, target=target) if target else worker.hermes_send(m) for m in messages):
        return "✅ 보냈습니다"
    return "\n\n".join(messages)


WAIT_MINUTES = 30


def waiting_url() -> str:
    """주소만 받고 글 내용을 기다리는 중이면 그 주소."""
    wait = load().get("waiting") or {}
    if wait and datetime.fromisoformat(wait["at"]) >= datetime.fromisoformat(now_iso()) - timedelta(minutes=WAIT_MINUTES):
        return wait["url"]
    return ""


SUB_MENTION = re.compile(r"(?<![A-Za-z0-9_/])r/([A-Za-z0-9_]{3,21})\b")
PASTED_MIN_CHARS = 150  # Reddit 방에서 명령이 아닌 이만큼 긴 글은 붙여 넣은 Reddit 글로 본다


def want_text(text: str) -> str:
    """주소 없이 붙여 넣은 Reddit 글 (캡처 텍스트 추출 등, 2026-11-03) → 운영자용 정리. 커뮤니티는 본문의 r/이름으로 찾고,
    없으면 `레딧 N 커뮤니티 <이름>` 으로 알려 달라고 한다. 게시 뒤 기록은 댓글 주소로 남긴다."""
    text = text.strip()
    state = load()
    state.pop("waiting", None)
    thread = "x" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    item = next((i for i in state["items"] if i["thread"] == thread), None)
    if not item:
        m = SUB_MENTION.search(text)
        home = (focus_subs() or ["unknown"])[0]  # r/이름이 없으면 본거지 글로 본다 (2026-11-05, 대부분 본거지에서 복사)
        title = next((line.strip() for line in text.splitlines() if len(line.strip()) > 15 and not SUB_MENTION.fullmatch(line.strip())), text[:100])
        item = {"n": max((i["n"] for i in state["items"]), default=0) + 1, "thread": thread, "url": "", "sub": m.group(1) if m else home,
                "guessed_sub": not m, "title": title[:100], "question": text, "posted": "", "status": "candidate", "at": now_iso()}
        state["items"].append(item)
    save(state)
    try:
        return brief_and_draft(item["n"])
    except RedditError as e:
        return f"⚠️ {e}"


def set_sub(n: int, sub: str) -> str:
    state = load()
    item = _item(state, n)
    item["sub"] = _name(sub)
    item.pop("guessed_sub", None)
    save(state)
    line = f"{n}번 커뮤니티: r/{item['sub']} — {_sub_line(state, item['sub'])}"
    if not (will_draft(item, standing(state, item["sub"])) and item["status"] == "briefed"):
        return line
    _deliver(line)  # 정리만 받은 글이면 바로 조사 → 초안으로 이어 간다 (2026-11-05)
    return continue_draft(n)


def want(url: str, question: str = "") -> str:
    """사람이 "이 글에 답하자" 고 보낸 글 → 운영자용 정리(brief, 2026-11-03). PC 에서 Reddit 을 열 수 없어(403) 글 내용은 사람이 붙여 넣는다."""
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
        item = _add(state, m.group(0), question[:100], question, any_sub=True)  # 정리는 운영자만 보는 글이라 커뮤니티 단계와 상관없이
    save(state)
    if not item:
        return "⚠️ 글을 기록하지 못했습니다 (주소를 확인해 주세요)"
    try:
        return brief_and_draft(item["n"])
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


def allow(sub: str, links: bool, no_ai: bool = False, mt: bool = False, no_treatments: bool = False) -> str:
    """원문 확인 기록. 운영자 참여 금지로 나온 곳은 이것만으로 풀리지 않는다 (운영진 허락 필요)."""
    state = load()
    rec = _editable(state, sub)
    rec.update(read=date.today().isoformat(), links=links, ai_banned=no_ai, mt_ok=mt, treatments_banned=no_treatments)
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 규칙 원문 확인 기록 ({'링크 허용' if links else '링크 없이'}{', AI 글 금지' if no_ai else ''}"
                              f"{', 번역 허용' if mt else ''}{', 시술 주제 금지' if no_treatments else ''})")


def asked(sub: str) -> str:
    state = load()
    _editable(state, sub)["asked"] = date.today().isoformat()
    save(state)
    return _after(state, sub, f"r/{_name(sub)} 운영진 문의 보냄 기록")


def mods(sub: str, decision: str, links: bool = False, no_ai: bool = False, note: str = "", mt: bool = False, no_treatments: bool = False) -> str:
    if decision not in ("allowed", "refused"):
        raise RedditError("운영진 답변은 허용 또는 거절입니다")
    state = load()
    rec = _editable(state, sub)
    rec.update(mods=decision, mods_at=date.today().isoformat(), mods_note=note)
    if decision == "allowed":
        rec.update(links=links or bool(rec.get("links")), ai_banned=no_ai or bool(rec.get("ai_banned")), mt_ok=mt or bool(rec.get("mt_ok")),
                   treatments_banned=no_treatments or bool(rec.get("treatments_banned")))
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
    for key in ("stopped", "stop_reason", "read", "links", "ai_banned", "mt_ok", "treatments_banned"):
        rec.pop(key, None)
    if rec.get("mods") == "allowed":
        rec.pop("mods", None)
    save(state)
    return _after(state, name, f"r/{name} 정지 해제 — 원문 확인부터 다시 (`레딧 규칙확인 {name} …`)")


def communities_message() -> str:
    """커뮤니티별 검토 항목(AI 글·링크·시술 주제)과 단계를 한 줄씩."""
    state = load()
    names = {n.lower(): n for n in (settings().get("communities") or {})}
    for key in state["subs"]:
        names.setdefault(key, next((i["sub"] for i in state["items"] if i["sub"].lower() == key), key))
    paused = state.get("paused")
    lines = ["[Reddit 커뮤니티] AI 글 · 링크 · 시술 주제 (○ 허용 △ 조건부 ✕ 금지 ? 모름) → 단계"]
    if paused:
        lines.append(f"⛔ 전체 정지 중: {paused.get('at', '')[:10]} {paused.get('note', '')}")
    for name in focus_subs():
        names.setdefault(name.lower(), name)
    for key in sorted(names, key=lambda k: (not (focus_subs() and in_focus(k)), k)):
        m, st = check_marks(state, names[key]), standing(state, names[key])
        star = "★ " if focus_subs() and in_focus(names[key]) else ""
        lines.append(f"{star}r/{names[key]}  AI {m['AI']} · 링크 {m['링크']} · 시술 {m['시술']} → {st['label']}")
    lines += ["", ("★ = 보는 커뮤니티 (후보 찾기·규칙 조사 대상). " if focus_subs() else "") + "기록: `레딧 규칙확인 <이름> [링크허용] [AI금지] [시술금지]`"]
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
    """정해진 요일에 한 번: 규칙 다시 조사 → 후보 찾기 (2026-11-02 사람 요청: 규칙 확인과 상관없이 매주). 전체 정지 중에는 안 한다."""
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
    try:
        changes = refresh_rules()
    except Exception as e:  # noqa: BLE001 — 규칙 조사가 실패해도 후보 찾기는 한다
        log.warning("Reddit 규칙 조사 실패: %s", e)
        changes = ["⚠️ 규칙 조사 실패 (로그 확인)"]
    head = ("[Reddit 규칙 주간 조사] 바뀐 곳\n" + "\n".join(changes)) if changes else "[Reddit 규칙 주간 조사] 바뀐 곳 없음"
    return head + "\n\n" + find()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reddit 답변 준비")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("find", "list", "communities"):
        sub.add_parser(name)
    sub.add_parser("rules").add_argument("--force", action="store_true")
    p = sub.add_parser("add")
    p.add_argument("url")
    p.add_argument("question")
    p = sub.add_parser("want-text")
    p.add_argument("text")
    p = sub.add_parser("set-sub")
    p.add_argument("n", type=int)
    p.add_argument("sub")
    p = sub.add_parser("want")
    p.add_argument("url")
    p.add_argument("question", nargs="?", default="")
    p = sub.add_parser("allow")
    p.add_argument("sub")
    p.add_argument("--links", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--mt", action="store_true")
    p.add_argument("--no-treatments", action="store_true")
    for name in ("asked", "reset", "resume"):
        sub.add_parser(name).add_argument("sub")
    p = sub.add_parser("mods")
    p.add_argument("sub")
    p.add_argument("decision", choices=["allowed", "refused"])
    p.add_argument("--links", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--mt", action="store_true")
    p.add_argument("--no-treatments", action="store_true")
    p.add_argument("--note", default="")
    p = sub.add_parser("warn")
    p.add_argument("sub")
    p.add_argument("--note", default="")
    for name in ("draft", "show", "skip", "facts", "brief", "research"):
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
    load_env_file()  # 터미널에서 직접 돌려도 Reddit 방(SKIN_REDDIT_DELIVER)·번역 키를 읽게 — 이미 있는 환경변수는 그대로
    try:
        if args.cmd == "find":
            print(find())
        elif args.cmd == "list":
            print(list_message())
        elif args.cmd == "communities":
            print(communities_message())
        elif args.cmd == "rules":
            changes = refresh_rules(args.force)
            print("[Reddit 규칙 조사] 바뀐 곳\n" + "\n".join(changes) if changes else "[Reddit 규칙 조사] 바뀐 곳 없음")
        elif args.cmd == "add":
            state = load()
            item = _add(state, args.url, args.question[:100], args.question)
            if not item:
                raise RedditError("추가하지 못했습니다: 주소가 틀렸거나, 이미 있거나, 제외·정지 커뮤니티이거나, 병원 추천을 묻는 글입니다")
            save(state)
            print(list_message([item], f"[Reddit {item['n']}번 추가]"))
        elif args.cmd in ("allow", "mods"):
            print(allow(args.sub, args.links, args.no_ai, args.mt, args.no_treatments) if args.cmd == "allow"
                  else mods(args.sub, args.decision, args.links, args.no_ai, args.note, args.mt, args.no_treatments))
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
        elif args.cmd == "want-text":
            print(want_text(args.text))
        elif args.cmd == "set-sub":
            print(set_sub(args.n, args.sub))
        elif args.cmd == "want":
            print(want(args.url, args.question))
        elif args.cmd == "draft":
            print(send(draft(args.n)))
        elif args.cmd == "direct":
            print(send(draft(args.n, args.text)))
        elif args.cmd == "brief":
            print(send(brief(args.n)))
        elif args.cmd == "research":
            print(_deliver(research_message(research(args.n))))
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
