"""X·Threads 텍스트 게시 — 게시된 블로그 글마다 SNS 글 3개를 만들어 하루 1개씩 보낸다.

흐름: 블로그에 배포된 중립 글 → SNS 글 생성(stage social, 사실 목록만) → 규칙 검사 → 매일 오전(한국 시간) 텔레그램으로 "오늘의 글" 1개
  - 글마다 3종: link(새 글 소개, 링크) · fact(사실 하나, 링크 없음) · angle(여행 팁 또는 흔한 오해 vs 출처, 링크 없음)
    + 일요일: 그 주에 게시된 글 정리(roundup, 코드가 제목으로 만든다)
  - 고르는 순서 (2026-10-10, 블로그 월·수·금): 일요일 = 정리 → 안 보낸 link(오래된 순) → 안 보낸 fact·angle(오래된 글부터).
    오늘 게시 예정 글이 아직 안 올라왔으면 정오까지 기다린다 (그날은 그 글의 link 가 나가게).
  - X: 기본은 **작성 링크(intent)** — 텔레그램의 링크를 누르면 글이 채워진 X 작성 화면이 열리고 사람이 [게시]를 누른다.
       link·정리 글은 본문에 링크를 넣지 않고(외부 링크가 있으면 노출이 줄어든다고 알려져 있다) 답글로 단다:
       'N 게시 완료 x <주소>' 를 받으면 그 게시물에 다는 링크 답글 작성 링크(in_reply_to)를 돌려준다.
       X API 는 유료라 기본 꺼짐 (config/channels.yaml text_social.x.mode: api 로 켤 수 있다, OAuth 1.0a 사용자 토큰).
       웹 화면을 자동 조작해 올리는 방식은 X 약관(공개 인터페이스 외 자동 접근 금지) 위반·계정 정지 위험이라 만들지 않는다.
  - Threads: Threads API(무료)로 올린다. link 는 본문 끝에 링크.
       토큰은 60일 만료 → 50일마다 자동 갱신해 ~/.skinbound/threads_token.json (권한 600)에 저장. 키가 없으면 작성 링크.
  - 스폰서 글은 channels.yaml sponsored_policy 에서 x·threads 를 허용하기 전까지 만들지 않는다.

    python -m pipeline.social compose            # 새 글의 SNS 글 생성·검사 (없는 종류만)
    python -m pipeline.social today              # 오늘의 글 (오전 9시 이후 하루 한 번, 워커가 부른다)
    python -m pipeline.social list               # 남은 글: 번호·종류·작성 링크·상태
    python -m pipeline.social post 오늘 threads   # API 게시 (번호 · 오늘 · draft_id[:종류])
    python -m pipeline.social done 오늘 x https://x.com/skinboundkorea/status/...   # 손으로 올린 게시 기록 (+ X 링크 답글 작성 링크)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline import llm, sponsors
from pipeline.common import (
    PRACTICE_KIND,
    content_dir,
    draft_dirs,
    facts_text,
    get_logger,
    load_draft,
    load_env_file,
    load_json,
    load_yaml,
    now_iso,
    prompt,
    run_cli,
    save_json,
    slugify,
)

log = get_logger("social")

CHANNELS = ("x", "threads")
KINDS = ("link", "fact", "angle")  # 글마다 만드는 종류 (보내는 순서도 이 순서)
LABEL = {"link": "새 글 소개", "fact": "사실 하나", "angle": "여행 팁·흔한 오해", "roundup": "한 주 정리"}
PUBLIC_STATES = ("approved", "rendered", "ready_to_publish", "published")
URL_WEIGHT = 23  # X 는 링크(도메인만 쓴 것 포함)를 길이와 관계없이 23자로 센다
INTENT = {"x": "https://x.com/intent/post?text={text}", "threads": "https://www.threads.net/intent/post?text={text}"}
POST_HOSTS = {"x": ("x.com", "twitter.com"), "threads": ("threads.net", "threads.com")}
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
LINKISH = re.compile(r"https?://\S+|www\.\S+|(?<!\S)(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?", re.I)
STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")
TODAY = ("오늘", "today")
KST = timezone(timedelta(hours=9))
DAY_NAMES = "월화수목금토일"
X_LINK_FOOTER = "\n\nSources in the reply ↓ (AI-assisted)"
ROUNDUP_X_FOOTER = "\n\nLinks in the reply ↓ (AI-assisted)"


class SocialError(RuntimeError):
    pass


def settings() -> dict:
    cfg = load_yaml("channels.yaml").get("text_social") or {}
    return {"x": {"mode": "intent", "max_chars": 280, **(cfg.get("x") or {})},
            "threads": {"mode": "api", "max_chars": 500, **(cfg.get("threads") or {})},
            "daily": {"hour": 9, "wait_until": 12, **(cfg.get("daily") or {})}}


def now_kst() -> datetime:
    return datetime.now(KST)


def live_slugs() -> dict[str, str]:
    """블로그에 실제로 배포된 글: draft_id → 주소(slug). 배포 기록(content/site/state.json)에 있는 글만."""
    import importlib
    site_mod = importlib.import_module("pipeline.site")
    state = site_mod.state_file()
    if not state.exists():
        return {}
    deployed = set(load_json(state).get("slugs", []))
    return {p["draft"]["id"]: p["slug"] for p in site_mod.collect_posts() if p["slug"] in deployed}


def home_url() -> str:
    site = load_yaml("site.yaml")
    return f"https://{site['domain']}/" if site.get("domain") else "/"


def blog_url(draft: dict, live: dict[str, str] | None = None) -> str:
    """배포된 글은 실제 주소(같은 제목이면 사이트가 꼬리표를 붙인다), 아니면 예정 주소."""
    live = live_slugs() if live is None else live
    slug = live.get(draft["id"]) or slugify(draft["blog"].get("slug") or draft["blog"].get("title") or draft["id"], 60)
    return home_url().rstrip("/") + f"/{slug}/"


def footer(url: str) -> str:
    """Threads 새 글 소개의 끝줄 (본문에 링크)."""
    return f"\n\nAI-assisted guide with sources: {url}"


def reply_text(url: str) -> str:
    """X 새 글 소개의 링크 답글."""
    return f"Full guide with sources: {url}"


def source_footer(domains: list[str]) -> str:
    """사실·팁 글의 끝줄 — 링크 대신 출처 이름."""
    return f"\n\nSource: {', '.join(domains)} · AI-assisted, not medical advice"


def x_length(text: str) -> int:
    return len(LINKISH.sub("x" * URL_WEIGHT, text))


def size(channel: str, text: str) -> int:
    return x_length(text) if channel == "x" else len(text)


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def social_facts(draft: dict) -> list[dict]:
    """SNS 에 쓸 수 있는 사실 — 여러 병원 공통 실무 정보(practice)는 특정 병원 출처라 뺀다."""
    return [f for f in draft.get("facts", []) if f.get("kind") != PRACTICE_KIND]


def fact_domains(draft: dict, fact_ids: list[str]) -> list[str]:
    by_id = {f["id"]: f for f in social_facts(draft)}
    return list(dict.fromkeys(_domain(by_id[i]["url"]) for i in fact_ids if i in by_id and by_id[i].get("url")))[:2]


# ---------------- 생성·검사 ----------------

def allowed(draft: dict) -> bool:
    """스폰서 글은 X·Threads 광고 정책을 확인해 sponsored_policy 에 허용하기 전까지 제외."""
    if not draft.get("sponsor"):
        return True
    policy = sponsors.platform_policy()
    return all(ch in policy["allowed"] for ch in CHANNELS)


def build_channels(kind: str, bodies: dict[str, str], url: str, domains: list[str]) -> dict:
    """본문 → 실제로 올라갈 글 (끝줄은 코드가 붙인다). X 의 link·정리 글은 링크를 답글로."""
    if kind == "link":
        return {"x": {"text": bodies["x"] + X_LINK_FOOTER, "reply": reply_text(url), "posted": None},
                "threads": {"text": bodies["threads"] + footer(url), "posted": None}}
    end = source_footer(domains)
    return {ch: {"text": bodies[ch] + end, "posted": None} for ch in CHANNELS}


def check_item(draft: dict, kind: str, bodies: dict[str, str], channels: dict | None = None,
               fact_ids: list[str] | None = None) -> list[str]:
    """금지 표현·병원명·연락처·출처 없는 수치·링크·길이 (02b 와 같은 목록). 길이는 끝줄까지 붙인 실제 글로 센다."""
    import importlib
    review = importlib.import_module("pipeline.02b_auto_review")
    problems = []
    known_numbers = set(NUMBER.findall(" ".join(f.get("text", "") for f in draft.get("facts", []))))
    cfg = settings()
    if channels is None:
        channels = build_channels(kind, bodies, blog_url(draft), fact_domains(draft, fact_ids or []) or ["source"])
    if kind != "link" and not fact_domains(draft, fact_ids or []):
        problems.append(f"[{kind}] 근거 사실(fact_ids) 없음")
    for ch, text in bodies.items():
        if not text:
            problems.append(f"[{kind}·{ch}] 빈 글")
            continue
        for term, pat in review.load_banned():
            if pat.search(text):
                problems.append(f"[{ch}] 금지 표현: {term}")
        for rule, label, pat in review.RULE_PATTERNS:
            m = pat.search(text)
            if m and not (rule == "clinic_name" and (review.INSTITUTION.search(m.group(0)) or review._generic(m.group(0)))):
                problems.append(f"[{ch}] {label}: {m.group(0)}")
        if URL.search(text):
            problems.append(f"[{ch}] 본문에 링크 (블로그 링크는 코드가 붙인다)")
        for n in NUMBER.findall(text):
            if n not in known_numbers:
                problems.append(f"[{ch}] 사실 목록에 없는 수치: {n}")
        limit = cfg[ch]["max_chars"]
        if (n := size(ch, channels[ch]["text"])) > limit:
            problems.append(f"[{ch}] 너무 김 ({n}/{limit}자)")
    if not draft.get("sponsor"):
        problems += [f.message for f in review.check_no_sponsor_names(bodies)]
    return problems


def check(draft: dict, texts: dict[str, str]) -> list[str]:
    """새 글 소개(link) 검사 — 예전 호출 방식."""
    return check_item(draft, "link", texts)


def rooms(url: str) -> dict[str, dict[str, int]]:
    """종류·채널별 본문 글자 수 (끝줄 제외, 여유 5자 — 모델이 글자 수를 정확히 세지 못한다)."""
    cfg = settings()
    src = len(source_footer(["x" * 24, "x" * 24]))  # 출처 이름 2개 (X 는 도메인을 23자로 센다)
    out = {"link": {"x": cfg["x"]["max_chars"] - len(X_LINK_FOOTER),
                    "threads": cfg["threads"]["max_chars"] - len(footer(url))}}
    for kind in ("fact", "angle"):
        out[kind] = {ch: cfg[ch]["max_chars"] - src for ch in CHANNELS}
    return {k: {ch: n - 5 for ch, n in v.items()} for k, v in out.items()}


def generate(draft: dict) -> dict[str, dict]:
    """한 번의 호출로 3종 — {kind: {"x": 본문, "threads": 본문, "fact_ids": [...]}}."""
    room = rooms(blog_url(draft))
    values = {f"{k}_{ch}": str(room[k][ch]) for k in KINDS for ch in CHANNELS}
    text = prompt("social_post", title=draft["blog"].get("title", ""), hook=(draft.get("shortform") or {}).get("hook", ""),
                  facts=facts_text(social_facts(draft)), travel="yes" if "procedure_travel" in (draft.get("axis"), draft.get("content_type")) else "no",
                  **values)
    data, _ = llm.generate_json("social", text)
    if not isinstance(data, dict):
        raise llm.LLMError("SNS 글 형식 오류")
    out = {}
    for kind in KINDS:
        item = data.get(kind) if isinstance(data.get(kind), dict) else {}
        out[kind] = {**{ch: str(item.get(ch) or "").strip() for ch in CHANNELS},
                     "fact_ids": [str(i) for i in item.get("fact_ids") or []]}
    return out


def social_file(path: Path) -> Path:
    return path / "social.json"


def load_social(path: Path) -> dict | None:
    """social.json (예전 형식 — 글당 소개 1개 — 은 link 하나로 읽는다)."""
    f = social_file(path)
    if not f.exists():
        return None
    data = load_json(f)
    if "items" not in data:  # 2026-10-10 이전 형식
        posts = data.get("posts") or {}
        sent = any(v.get("posted") for v in posts.values())
        data = {"version": 2, "draft_id": data["draft_id"], "title": data.get("title", ""),
                "created_at": data.get("created_at", ""), "blog_url": data.get("blog_url", ""),
                "items": {"link": {"status": data.get("status", "ready"), "problems": data.get("problems", []),
                                   "fact_ids": [], "channels": {ch: {"reply": None, **posts[ch]} if ch == "x" else posts[ch]
                                                                for ch in CHANNELS if ch in posts},
                                   "scheduled_on": (data.get("created_at") or "")[:10] if sent else None}}}
    return data


def compose(path: Path) -> dict:
    """없는 종류만 생성 + 검사 (검사에 걸린 종류만 1번 다시 생성). social.json 저장."""
    draft = load_draft(path)
    data = load_social(path) or {"version": 2, "draft_id": draft["id"], "title": draft["blog"].get("title", ""),
                                 "created_at": now_iso(), "items": {}}
    url = blog_url(draft)
    data["blog_url"] = url
    missing = [k for k in KINDS if k not in data["items"]]
    results: dict[str, dict] = {}
    for _ in range(2):
        todo = [k for k in missing if k not in results or results[k]["problems"]]
        if not todo:
            break
        generated = generate(draft)
        for kind in todo:
            g = generated[kind]
            bodies = {ch: g[ch] for ch in CHANNELS}
            channels = build_channels(kind, bodies, url, fact_domains(draft, g["fact_ids"]) or ["source"])
            results[kind] = {"status": "ready", "fact_ids": g["fact_ids"], "channels": channels, "scheduled_on": None,
                             "problems": check_item(draft, kind, bodies, channels, g["fact_ids"])}
    for kind, item in results.items():
        item["status"] = "blocked" if item["problems"] else "ready"
        data["items"][kind] = item
    save_json(social_file(path), data)
    return data


def pending_paths() -> list[Path]:
    """SNS 글을 만들 글 — 블로그에 실제로 배포된 글만 (배포 전이면 링크가 열리지 않는다), 아직 없는 종류가 있는 글."""
    live = live_slugs()
    out = []
    for state in PUBLIC_STATES:
        for p in draft_dirs(state):
            if p.name not in live or not (p / "draft.json").exists() or not allowed(load_draft(p)):
                continue
            data = load_social(p)
            if data is None or any(k not in data["items"] for k in KINDS):
                out.append(p)
    return out


def compose_pending(limit: int = 5) -> tuple[list[str], list[str]]:
    done, failed = [], []
    for path in pending_paths()[:limit]:
        try:
            compose(path)
            done.append(path.name)
        except llm.LLMError as e:
            failed.append(f"{path.name}: {e}")
    return done, failed


# ---------------- 한 주 정리 (일요일) ----------------

def roundup_dir() -> Path:
    return content_dir() / "social"


def week_key(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"roundup-{y}-W{w:02d}"


def roundup(day: date) -> str | None:
    """그 주(월~일)에 게시된 글(2편 이상) 제목으로 정리 글을 만든다 (코드가 만든다, 이미 있으면 그대로). 반환: 키 또는 None."""
    key = week_key(day)
    f = roundup_dir() / f"{key}.json"
    if f.exists():
        return key
    import importlib
    site_mod = importlib.import_module("pipeline.site")
    live = live_slugs()
    monday = day - timedelta(days=day.weekday())
    titles = []
    for p in sorted(site_mod.collect_posts(), key=lambda p: p["date"]):
        when = datetime.fromisoformat(p["date"]).astimezone(KST).date() if p["date"] else None
        if p["draft"]["id"] in live and when and monday <= when <= day and not p["draft"].get("sponsor"):
            titles.append(p["draft"]["blog"].get("title", ""))
    if len(titles) < 2:  # 1편이면 그 주 새 글 소개를 되풀이하는 셈
        return None
    cfg = settings()
    home = home_url()
    for n in range(len(titles), 0, -1):  # 길면 제목을 줄인다
        body = "This week on Skinbound:\n" + "\n".join(f"→ {t}" for t in titles[:n])
        channels = {"x": {"text": body + ROUNDUP_X_FOOTER, "reply": f"All guides with sources: {home}", "posted": None},
                    "threads": {"text": body + f"\n\nEvery guide cites its sources: {home}\nAI-assisted, human-reviewed.",
                                "posted": None}}
        if all(size(ch, channels[ch]["text"]) <= cfg[ch]["max_chars"] for ch in CHANNELS):
            break
    save_json(f, {"version": 2, "draft_id": key, "title": f"한 주 정리 ({key[8:]})", "created_at": now_iso(),
                  "blog_url": home, "items": {"roundup": {"status": "ready", "problems": [], "fact_ids": [],
                                                          "channels": channels, "scheduled_on": None}}})
    return key


# ---------------- 목록·오늘의 글·기록 ----------------

@dataclass(frozen=True)
class Ref:
    key: str   # draft_id 또는 roundup-YYYY-Www
    kind: str


def _file(key: str) -> Path:
    for state in PUBLIC_STATES:
        p = content_dir(state) / key
        if social_file(p).exists():
            return social_file(p)
    f = roundup_dir() / f"{key}.json"
    if f.exists():
        return f
    raise SocialError(f"SNS 글을 찾을 수 없습니다: {key}")


def _load(ref: Ref) -> dict:
    f = _file(ref.key)
    data = load_social(f.parent) if f.name == "social.json" else load_json(f)
    if ref.kind not in data["items"]:
        raise SocialError(f"{ref.key} 에 {ref.kind} 글이 없습니다")
    return data


def _save(ref: Ref, data: dict) -> None:
    save_json(_file(ref.key), data)


def entries(include_done: bool = False) -> list[tuple[Ref, dict, dict]]:
    """(Ref, 파일 데이터, 항목) — 아직 다 안 올린 것만 (include_done=True 면 전부). 만든 순 → 종류 순."""
    files = [social_file(p) for s in PUBLIC_STATES for p in draft_dirs(s) if social_file(p).exists()]
    files += sorted(roundup_dir().glob("roundup-*.json")) if roundup_dir().exists() else []
    out = []
    for f in files:
        data = load_social(f.parent) if f.name == "social.json" else load_json(f)
        for kind, item in data["items"].items():
            if include_done or any(not c.get("posted") for c in item["channels"].values()):
                out.append((Ref(data["draft_id"], kind), data, item))
    order = {k: i for i, k in enumerate((*KINDS, "roundup"))}
    return sorted(out, key=lambda e: (e[1].get("created_at", ""), order.get(e[0].kind, 9)))


def intent_link(channel: str, text: str) -> str:
    return INTENT[channel].format(text=urllib.parse.quote(text, safe=""))


def reply_link(status_url: str, text: str) -> str | None:
    m = STATUS_ID.search(status_url)
    if not m:
        return None
    return f"https://x.com/intent/post?in_reply_to={m.group(1)}&text={urllib.parse.quote(text, safe='')}"


def _channel_lines(ref_word: str, item: dict) -> list[str]:
    cfg = settings()
    lines = []
    for ch in ("threads", "x"):
        c = item["channels"].get(ch)
        if not c:
            continue
        if c.get("posted"):
            lines.append(f"{ch} ✅ {c['posted']['url']}")
        elif ch == "threads" and cfg[ch]["mode"] == "api" and threads_configured():
            lines.append(f"threads: '{ref_word} 스레드 올려' (API)\n{c['text']}")
        elif ch == "x" and cfg[ch]["mode"] == "api" and x_configured():
            lines.append(f"x: '{ref_word} X 올려' (API, 유료)\n{c['text']}")
        else:
            after = (f"게시 후 '{ref_word} 게시 완료 {ch} <주소>'" +
                     (" → 링크 답글 작성 링크를 보내드립니다" if c.get("reply") else ""))
            lines.append(f"{ch}: {intent_link(ch, c['text'])}\n   {after}")
    return lines


def list_message() -> str:
    rows = entries()
    if not rows:
        return ""
    lines = ["[X·Threads 글] 작성 링크를 누르면 글이 채워진 화면이 열립니다 → [게시] 후 'N 게시 완료 x <주소>'"]
    for i, (ref, data, item) in enumerate(rows, 1):
        head = f"{i}. [{LABEL.get(ref.kind, ref.kind)}] {data['title']}"
        if item["status"] == "blocked":
            lines.append(f"{head} — ⛔ " + "; ".join(item["problems"][:3]))
            continue
        lines.append(head + (f" (오늘의 글 {item['scheduled_on']})" if item.get("scheduled_on") else ""))
        lines += [f"   {line}" for line in _channel_lines(str(i), item)]
    return "\n".join(lines)


def daily_file() -> Path:
    return roundup_dir() / "daily.json"


def _waiting_for_blog(now: datetime) -> bool:
    """오늘 게시 예정인 글의 새 글 소개가 아직 준비 안 됐으면 (정오 전까지) 기다린다."""
    import importlib
    site_mod = importlib.import_module("pipeline.site")
    if now.hour >= settings()["daily"]["wait_until"] or not site_mod.publish_schedule():
        return False
    f = site_mod.schedule_file()
    today = now.date().isoformat()
    for did, slot in (load_json(f) if f.exists() else {}).items():
        if slot["date"] != today:
            continue
        path = next((content_dir(s) / did for s in PUBLIC_STATES if (content_dir(s) / did / "draft.json").exists()), None)
        if path is None or not allowed(load_draft(path)):
            continue  # 스폰서 글은 SNS 글을 만들지 않는다
        data = load_social(path)
        if data is None or "link" not in data["items"]:
            return True  # 아직 배포·생성 전
    return False


def pick_today(now: datetime) -> Ref | None:
    """오늘 보낼 글 하나. 이미 골랐거나 시각 전이면 None."""
    state = load_json(daily_file()) if daily_file().exists() else {}
    if now.date().isoformat() in state or now.hour < settings()["daily"]["hour"]:
        return None
    if now.weekday() == 6 and (key := roundup(now.date())):
        item = _load(Ref(key, "roundup"))["items"]["roundup"]
        if not item.get("scheduled_on"):
            return Ref(key, "roundup")
    if _waiting_for_blog(now):
        return None
    candidates = [(ref, item) for ref, _, item in entries()
                  if ref.kind in KINDS and item["status"] == "ready" and not item.get("scheduled_on")
                  and not any(c.get("posted") for c in item["channels"].values())]
    for kinds in (("link",), ("fact", "angle")):
        for ref, _ in candidates:
            if ref.kind in kinds:
                return ref
    return None


def daily_message(now: datetime | None = None) -> str:
    """오전 9시(한국 시간) 이후 첫 호출에 오늘의 글 1개 (하루 한 번). 보낼 게 없으면 빈 문자열."""
    now = now or now_kst()
    ref = pick_today(now)
    if not ref:
        return ""
    data = _load(ref)
    item = data["items"][ref.kind]
    today = now.date().isoformat()
    item["scheduled_on"] = today
    _save(ref, data)
    state = load_json(daily_file()) if daily_file().exists() else {}
    state[today] = {"key": ref.key, "kind": ref.kind}
    save_json(daily_file(), state)
    head = f"[오늘의 SNS 글] {now:%m/%d}({DAY_NAMES[now.weekday()]}) · {LABEL[ref.kind]} — {data['title']}"
    return "\n".join([head, *_channel_lines("오늘", item)])


def resolve(ref: str) -> Ref:
    """번호(list 순서) · 오늘 · draft_id[:종류]."""
    ref = ref.strip()
    if ref in TODAY:
        state = load_json(daily_file()) if daily_file().exists() else {}
        days = sorted(d for d in state if d <= now_kst().date().isoformat())
        if not days:
            raise SocialError("보낸 오늘의 SNS 글이 없습니다")
        latest = state[days[-1]]  # 자정을 넘겨 답해도 가장 최근에 보낸 글
        return Ref(latest["key"], latest["kind"])
    rows = entries()
    if ref.isdigit() and 1 <= int(ref) <= len(rows):
        return rows[int(ref) - 1][0]
    key, _, kind = ref.partition(":")
    found = Ref(key, kind or ("roundup" if key.startswith("roundup-") else "link"))
    _load(found)
    return found


def _ref(target: Ref | Path | str, kind: str = "link") -> Ref:
    if isinstance(target, Ref):
        return target
    if isinstance(target, Path):
        return Ref(target.name, kind)
    return resolve(target)


def record(target: Ref | Path | str, channel: str, url: str, kind: str = "link") -> str:
    """손으로 올린 게시 기록. X 이고 링크 답글이 있으면 그 게시물에 다는 답글 작성 링크를 같이 돌려준다."""
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    if urllib.parse.urlparse(url).scheme != "https" or not any(host == h or host.endswith("." + h) for h in POST_HOSTS[channel]):
        raise SocialError(f"{channel} 게시물 주소가 아닙니다: {url}")
    url = url.split("?")[0]  # 공유 꼬리표(?s=20 등) 제거
    ref = _ref(target, kind)
    data = _load(ref)
    item = data["items"][ref.kind]
    item["channels"][channel]["posted"] = {"url": url, "at": now_iso()}
    item["scheduled_on"] = item.get("scheduled_on") or now_kst().date().isoformat()
    _save(ref, data)
    out = f"✅ {data['title']} ({LABEL.get(ref.kind, ref.kind)}) — {channel} 게시 기록"
    reply = item["channels"][channel].get("reply")
    if channel == "x" and reply:
        link = reply_link(url, reply)
        out += (f"\n↩️ 링크 답글 — 눌러서 [답글] 게시:\n{link}" if link
                else f"\n↩️ 주소에서 게시물 번호를 못 찾았습니다. 답글로 직접 붙여 넣으세요:\n{reply}")
    return out


# ---------------- Threads API ----------------

THREADS_GRAPH = "https://graph.threads.com"  # Meta 문서 기준 새 주소 (예전 graph.threads.net)
THREADS_API = f"{THREADS_GRAPH}/v1.0"
REFRESH_AFTER_DAYS = 50
THREADS_SCOPES = "threads_basic,threads_content_publish"
# 처음 연결(threads-auth)에서 로그인 뒤 돌아올 주소 — 서버가 없으니 localhost 로 보내고 주소창의 code 를 복사한다.
# 앱 대시보드 Threads 사용 사례 설정의 "리디렉션 콜백 URL" 에 똑같이 넣어야 한다.
DEFAULT_REDIRECT = "https://localhost/"


def _save_token(token: str) -> None:
    path = threads_token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token, "refreshed_at": now_iso()}), encoding="utf-8")
    os.chmod(path, 0o600)


def _app_credentials() -> tuple[str, str, str]:
    app_id, secret = os.environ.get("THREADS_APP_ID", ""), os.environ.get("THREADS_APP_SECRET", "")
    if not app_id or not secret:
        raise SocialError("THREADS_APP_ID·THREADS_APP_SECRET 이 .env 에 없습니다 (앱 대시보드 → 앱 설정 → 기본 → Threads 앱 ID·시크릿)")
    return app_id, secret, os.environ.get("THREADS_REDIRECT_URI", DEFAULT_REDIRECT)


def threads_auth_url(state: str | None = None) -> str:
    """1단계: 브라우저로 열 로그인·권한 허용 주소 (우리 Threads 계정은 앱의 Threads 테스터여야 한다)."""
    app_id, _, redirect = _app_credentials()
    return "https://threads.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": app_id, "redirect_uri": redirect, "scope": THREADS_SCOPES, "response_type": "code",
         "state": state or secrets.token_hex(8)})


def code_from(value: str) -> str:
    """돌아온 주소 전체나 code 값만 — 끝에 붙는 '#_' 는 code 가 아니다."""
    value = value.strip()
    if value.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
        if query.get("error"):
            raise SocialError(f"권한 허용이 취소됐습니다: {query.get('error_description', query['error'])[0]}")
        value = (query.get("code") or [""])[0]
    value = value.split("#")[0]
    if not value:
        raise SocialError("주소에 code 가 없습니다 — 권한 허용 후 주소창의 주소를 그대로 넣어 주세요")
    return value


def threads_connect(code_or_url: str) -> str:
    """2단계: code → 단기 토큰(1시간) → 장기 토큰(60일) → 저장 (이후 50일마다 자동 갱신). 반환: 계정 이름."""
    app_id, secret, redirect = _app_credentials()
    form = urllib.parse.urlencode({"client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
                                   "redirect_uri": redirect, "code": code_from(code_or_url)}).encode()
    short = _http("POST", f"{THREADS_GRAPH}/oauth/access_token", body=form,
                  headers={"content-type": "application/x-www-form-urlencoded"})
    long = _http("GET", f"{THREADS_GRAPH}/access_token",
                 {"grant_type": "th_exchange_token", "client_secret": secret, "access_token": short["access_token"]})
    _save_token(long["access_token"])
    me = _http("GET", f"{THREADS_API}/me", {"fields": "id,username", "access_token": long["access_token"]})
    return me.get("username", "")


def threads_token_file() -> Path:
    return Path(os.environ.get("THREADS_TOKEN_FILE", Path.home() / ".skinbound" / "threads_token.json"))


def threads_configured() -> bool:
    return bool(os.environ.get("THREADS_ACCESS_TOKEN") or threads_token_file().exists())


def _http(method: str, url: str, params: dict | None = None, headers: dict | None = None, body: bytes | None = None) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body if body is not None else (b"" if method == "POST" else None),
                                 method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise SocialError(f"{method} {urllib.parse.urlparse(url).path} → {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SocialError(f"{method} {urllib.parse.urlparse(url).path} 실패: {e}") from e


def threads_token() -> str:
    """저장된 토큰(없으면 환경변수) — 50일이 지났으면 갱신해 저장 (60일 만료 전에)."""
    path = threads_token_file()
    data = load_json(path) if path.exists() else {}
    token = data.get("access_token") or os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        raise SocialError("THREADS_ACCESS_TOKEN 이 없습니다 (Meta 개발자 앱 → Threads 장기 토큰)")
    saved = datetime.fromisoformat(data["refreshed_at"]) if data.get("refreshed_at") else None
    if saved is None or (datetime.now(timezone.utc) - saved).days >= REFRESH_AFTER_DAYS:
        try:
            fresh = _http("GET", f"{THREADS_GRAPH}/refresh_access_token",
                          {"grant_type": "th_refresh_token", "access_token": token})
            token = fresh["access_token"]
        except (SocialError, KeyError) as e:
            if saved is not None:  # 저장된 토큰이 있는데 갱신 실패 → 만료 전에 알려야 한다
                raise SocialError(f"Threads 토큰 갱신 실패 — 새 토큰 발급 필요: {e}") from e
            log.warning("Threads 토큰 갱신 실패 (새로 받은 토큰이면 정상): %s", e)
        _save_token(token)
    return token


def threads_post(text: str) -> str:
    token = threads_token()
    me = _http("GET", f"{THREADS_API}/me", {"fields": "id,username", "access_token": token})
    container = _http("POST", f"{THREADS_API}/{me['id']}/threads", {"media_type": "TEXT", "text": text, "access_token": token})
    for attempt in range(3):  # 컨테이너 준비 전이면 잠깐 기다렸다 다시 (텍스트는 보통 바로 됨)
        try:
            post = _http("POST", f"{THREADS_API}/{me['id']}/threads_publish",
                         {"creation_id": container["id"], "access_token": token})
            break
        except SocialError:
            if attempt == 2:
                raise
            time.sleep(10)
    info = _http("GET", f"{THREADS_API}/{post['id']}", {"fields": "permalink", "access_token": token})
    return info.get("permalink") or f"https://www.threads.net/@{me.get('username', '')}/post/{post['id']}"


# ---------------- X API (선택, 유료) ----------------

X_API = "https://api.x.com/2/tweets"
X_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


def x_configured() -> bool:
    return all(os.environ.get(k) for k in X_KEYS)


def _oauth1_header(method: str, url: str) -> str:
    """OAuth 1.0a 사용자 서명 (JSON 본문은 서명에 넣지 않는다)."""
    oauth = {"oauth_consumer_key": os.environ["X_API_KEY"], "oauth_nonce": secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
             "oauth_token": os.environ["X_ACCESS_TOKEN"], "oauth_version": "1.0"}
    enc = lambda s: urllib.parse.quote(str(s), safe="")  # noqa: E731
    params = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(oauth.items()))
    base = "&".join([method.upper(), enc(url), enc(params)])
    key = f"{enc(os.environ['X_API_SECRET'])}&{enc(os.environ['X_ACCESS_SECRET'])}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))


def x_post(text: str, reply_to: str | None = None) -> str:
    if not x_configured():
        raise SocialError("X API 키가 없습니다 — 작성 링크로 올리세요 (X API 는 유료)")
    payload = {"text": text, **({"reply": {"in_reply_to_tweet_id": reply_to}} if reply_to else {})}
    body = json.dumps(payload).encode()
    res = _http("POST", X_API, headers={"authorization": _oauth1_header("POST", X_API), "content-type": "application/json"},
                body=body)
    return f"https://x.com/i/web/status/{res['data']['id']}"


def post(target: Ref | Path | str, channel: str, kind: str = "link") -> str:
    ref = _ref(target, kind)
    data = _load(ref)
    item = data["items"][ref.kind]
    if item["status"] != "ready":
        raise SocialError(f"검사를 통과하지 못한 글입니다: {'; '.join(item['problems'][:3])}")
    c = item["channels"][channel]
    if c["posted"]:
        return f"이미 게시됨: {c['posted']['url']}"
    url = (threads_post if channel == "threads" else x_post)(c["text"])
    out = record(ref, channel, url)
    if channel == "x" and c.get("reply"):  # API 로 올렸으면 링크 답글도 API 로
        x_post(c["reply"], reply_to=STATUS_ID.search(url).group(1))
        out = out.split("\n↩️")[0] + "\n↩️ 링크 답글도 게시"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X·Threads 글")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compose")
    sub.add_parser("list")
    sub.add_parser("today", help="오늘의 글 (오전 9시 이후 하루 한 번)")
    p = sub.add_parser("post")
    p.add_argument("ref", help="번호 · 오늘 · draft_id[:종류]")
    p.add_argument("channel", choices=CHANNELS)
    d = sub.add_parser("done")
    d.add_argument("ref")
    d.add_argument("channel", choices=CHANNELS)
    d.add_argument("url")
    a = sub.add_parser("threads-auth", help="처음 한 번: 인자 없이 → 로그인 주소, --code <돌아온 주소> → 장기 토큰 저장")
    a.add_argument("--code", help="권한 허용 후 주소창의 주소 전체 또는 code 값")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "threads-auth":
            load_env_file()  # 터미널에서 직접 실행하는 설정 명령 — Hermes 밖이라 .env 를 직접 읽는다
            if not args.code:
                print("1) 아래 주소를 브라우저로 열어 우리 Threads 계정으로 권한 허용\n"
                      "2) localhost 로 이동하며 '연결할 수 없음' 화면이 떠도 정상 — 주소창의 주소 전체를 복사\n"
                      '3) python -m pipeline.social threads-auth --code "<복사한 주소>"   (1시간 안에, 한 번만)\n')
                print(threads_auth_url())
                return 0
            name = threads_connect(args.code)
            print(f"✅ Threads 연결 완료: @{name} — 장기 토큰 저장 ({threads_token_file()}), 50일마다 자동 갱신")
            return 0
        if args.cmd == "compose":
            done, failed = compose_pending()
            for f in failed:
                print(f"⚠️ SNS 글 생성 실패 {f}")
            if done:
                print(list_message())
            return 1 if failed and not done else 0
        if args.cmd == "list":
            print(list_message() or "올릴 SNS 글 없음")
            return 0
        if args.cmd == "today":
            print(daily_message() or "오늘 보낼 글 없음 (이미 보냈거나 오전 9시 전)")
            return 0
        ref = resolve(args.ref)
        print(post(ref, args.channel) if args.cmd == "post" else record(ref, args.channel, args.url))
        return 0
    except SocialError as e:
        print(f"❓ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    run_cli("social", main)
