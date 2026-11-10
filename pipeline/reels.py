"""리믹스 번역 릴스 — 원본 후보 찾기 (2026-11-05, docs/shortform-ops-handoff.md "리믹스 번역 릴스").

블로그에 이미 쓴 시술의 해시태그에서 인기 영상(top_media)을 공식 Instagram API(Facebook 로그인 방식)로 받아,
캡션에 협찬·체험단·병원·할인·전후 비교 신호가 있는 글을 빼고 좋아요순으로 운영자에게 보낸다.
리믹스 허용 여부와 화면(병원 간판·시술 장면·다른 사람 얼굴)은 API 로 알 수 없어 운영자가 앱에서 확인한다.
웹 자동 조작·수집은 만들지 않는다. 리믹스·게시는 사람이 앱에서 한다.

    python -m pipeline.reels check                     # 토큰·계정·해시태그 검색 권한 점검
    python -m pipeline.reels find [주제] [--limit N] [--send]
    python -m pipeline.reels list                      # 마지막 후보 목록

토큰·계정 ID 는 환경변수로만: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID (값은 로그에 남기지 않는다).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from pipeline.common import content_dir, get_logger, load_env_file, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("reels")

GRAPH = "https://graph.facebook.com"
# media_url: 고른 후보의 번역 작업용 (reels_work). 음원 저작권 등으로 Instagram 이 안 주는 영상이 있다 (2026-11-10 확인: 영상 절반)
MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,like_count,comments_count,timestamp,media_url"
MEDIA_KEEP_DAYS = 7  # 후보의 영상 주소(서명 포함, 며칠 뒤 만료)는 이 기간만 둔다
VIDEO_TYPES = {"VIDEO", "REELS"}
# "광고 아님", "협찬X", "체험단 아니고" 같은 부정 표현 — 제외 검사 전에 지운다 (내돈내산 글이 빠지지 않게)
NEGATION = re.compile(r"(?:(?:협찬|광고|체험단|수수료|원고료|제공)\s*[•·・,/&\s]*)+(은|이)?\s*"
                      r"(아님|아닙니다|아니에요|아니고|아니며|아니예요|[xX]|❌|🙅\S*|없음|없어요|없습니다|0원)")
REASON_LABEL = {"sponsored": "협찬·체험단", "clinic": "병원·의사", "dm_funnel": "DM 유도", "promo": "공구·할인 홍보",
                "before_after": "전후 비교", "clinic_voice": "병원 안내 말투"}

_http_impl = None  # 테스트용 가짜 HTTP (set_http)


class ReelsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status, self.code = status, code


def set_http(fn) -> None:
    """테스트: fn(path, params) -> dict 로 Graph API 호출을 바꾼다. None 이면 실제 호출."""
    global _http_impl
    _http_impl = fn


def settings() -> dict:
    return load_yaml("reels.yaml")


def _state_file():
    return content_dir() / "reels" / "state.json"


def load_state() -> dict:
    f = _state_file()
    return load_json(f) if f.exists() else {"seen": {}, "hashtags": {}, "queries": [], "last": {}}


def save_state(state: dict) -> None:
    save_json(_state_file(), state)


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ReelsError(f"{name} 가 .env 에 없습니다 (Meta 앱 → Instagram API(Facebook 로그인) → 장기 토큰, "
                         "계정 ID 는 me/accounts?fields=instagram_business_account)")
    return value


def user_id() -> str:
    return _env("INSTAGRAM_USER_ID")


def graph(path: str, params: dict) -> dict:
    """Graph API GET. 오류는 원인이 보이게 (토큰 만료 190, 권한 10·200, 해시태그 한도 등). 토큰 값은 메시지에 넣지 않는다."""
    if _http_impl is not None:
        return _http_impl(path, dict(params))
    token = _env("INSTAGRAM_ACCESS_TOKEN")
    url = f"{GRAPH}/{settings().get('graph_version', 'v23.0')}/{path}?" + urllib.parse.urlencode({**params, "access_token": token})
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            api_code = (json.loads(body).get("error") or {}).get("code")
        except ValueError:
            api_code = None
        raise ReelsError(f"GET {path} → {e.code}: {_explain(body)}", status=e.code, code=api_code) from e
    except TimeoutError as e:  # 응답이 늦음 — 요청 양을 줄이면 대개 풀린다 (2026-11-08)
        raise ReelsError(f"GET {path} 시간 초과: {e}", status=408) from e
    except urllib.error.URLError as e:
        status = 408 if isinstance(e.reason, TimeoutError) else None
        raise ReelsError(f"GET {path} 실패: {e}", status=status) from e
    except (OSError, ValueError) as e:
        raise ReelsError(f"GET {path} 실패: {e}") from e


def _explain(body: str) -> str:
    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        return body[:300]
    code, sub, msg = err.get("code"), err.get("error_subcode"), err.get("message", "")[:200]
    hint = {2207024: "해시태그를 찾을 수 없음 — 다른 해시태그로 (한글·영어 섞인 태그는 안 잡히는 경우가 있다)"}.get(sub) or {
        190: "토큰 만료·무효 — 새 장기 토큰을 .env 에",
        10: "권한 없음 — 앱에 Instagram Public Content Access(해시태그 검색)가 없으면 앱 심사 필요",
        200: "권한 없음 — 토큰 받을 때 페이지·인스타그램 계정을 체크했는지, instagram_basic 권한 확인",
        100: "요청 오류 — user_id 가 인스타그램 계정 ID(페이지 ID 아님)인지 확인"}.get(code, "")
    user_msg = err.get("error_user_msg", "")
    return (f"[{code}{'/' + str(sub) if sub else ''}] {msg}" + (f" ({user_msg[:160]})" if user_msg else "")
            + (f" → {hint}" if hint else ""))


# ── 선별 ───────────────────────────────────────────────────────────────────

def exclusion_reasons(caption: str, rules: dict | None = None) -> list[str]:
    """캡션에서 제외 신호 종류를 찾는다 (sponsored·clinic·dm_funnel·before_after). 빈 목록 = 통과.
    rules 를 주면 그 규칙으로 (warn 검사에도 쓴다)."""
    rules = rules if rules is not None else settings().get("exclude", {})
    text = NEGATION.sub(" ", caption or "")
    return [kind for kind, patterns in rules.items()
            if any(re.search(p, text, re.IGNORECASE) for p in patterns or [])]


def rank_key(media: dict) -> tuple:
    """좋아요순. 좋아요를 숨긴 글은 좋아요 있는 글 뒤에 댓글순."""
    likes = media.get("like_count")
    return (0, -likes) if isinstance(likes, int) else (1, -(media.get("comments_count") or 0))


# ── 해시태그 ───────────────────────────────────────────────────────────────

def _recent_tags(state: dict, now: datetime) -> set[str]:
    week_ago = now - timedelta(days=7)
    return {q["tag"] for q in state.get("queries", []) if datetime.fromisoformat(q["at"]) >= week_ago}


def hashtag_id(tag: str, state: dict) -> str:
    """해시태그 ID (한 번 받으면 저장 — 같은 해시태그는 Meta 한도에 다시 세지 않지만 호출을 줄인다).
    7일에 서로 다른 해시태그 weekly_hashtag_limit 개를 넘기면 새 해시태그는 막는다."""
    tag = tag.lstrip("#").strip()
    now = datetime.fromisoformat(now_iso())
    recent = _recent_tags(state, now)
    limit = int(settings().get("weekly_hashtag_limit", 25))
    if tag not in recent and len(recent) >= limit:
        raise ReelsError(f"최근 7일 해시태그 {len(recent)}개 — 한도({limit}) 때문에 '{tag}' 는 다음 주에")
    if tag not in state.setdefault("hashtags", {}):
        data = graph("ig_hashtag_search", {"user_id": user_id(), "q": tag}).get("data") or []
        if not data:
            raise ReelsError(f"해시태그 없음: {tag}")
        state["hashtags"][tag] = data[0]["id"]
    if tag not in recent:
        state.setdefault("queries", []).append({"tag": tag, "at": now.isoformat()})
    return state["hashtags"][tag]


def _too_much_data(e: ReelsError) -> bool:
    """Meta 가 '요청하는 데이터 양을 줄이라'고 하거나(HTTP 500 · code 1) 응답이 시간 초과일 때 (2026-11-08 실제 응답)."""
    return e.code == 1 or e.status in (500, 408)


def hashtag_media(tag_id: str, edge: str = "top_media", pages: int = 1, page_size: int | None = None) -> list[dict]:
    """해시태그 게시물 — edge 는 top_media(인기) 또는 recent_media(최근 24시간). pages 페이지까지 (after 커서).
    '데이터 양을 줄이라'(500·code 1)·시간 초과면 페이지 크기를 반으로 줄여 다시 (최소 10).
    첫 페이지 뒤의 페이지가 실패하면 받은 데까지만 쓴다 — 페이지 하나 때문에 찾기 전체가 멈추지 않게."""
    size = page_size or int(settings().get("page_size", 25))
    out, after = [], None
    for page in range(max(1, pages)):
        while True:
            params = {"user_id": user_id(), "fields": MEDIA_FIELDS, "limit": size}
            if after:
                params["after"] = after
            try:
                data = graph(f"{tag_id}/{edge}", params)
                break
            except ReelsError as e:
                if _too_much_data(e) and size > 10:
                    size = max(10, size // 2)
                    log.warning("%s 데이터 양 줄임 → 페이지당 %d개로 다시", edge, size)
                    continue
                if page > 0:
                    log.warning("%s %d페이지 실패 — 받은 %d개까지만 씀: %s", edge, page + 1, len(out), e)
                    return out
                raise
        out += data.get("data") or []
        after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (data.get("paging") or {}).get("next"):
            break
    return out


def top_media(tag_id: str, pages: int = 1, page_size: int | None = None) -> list[dict]:
    return hashtag_media(tag_id, "top_media", pages, page_size)


def recent_media(tag_id: str, pages: int = 1, page_size: int | None = None) -> list[dict]:
    """최근 24시간 게시물 — 인기 게시물은 오래된 글이 많아 새 릴스가 잘 안 잡힌다 (2026-11-08)."""
    return hashtag_media(tag_id, "recent_media", pages, page_size)


def too_old(media: dict, max_age_days: int, now: datetime) -> bool:
    ts = media.get("timestamp") or ""
    try:
        posted = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return False  # 날짜를 모르면 빼지 않는다 (목록에 날짜가 비어 보인다)
    return max_age_days > 0 and (now - posted).days > max_age_days


# ── 찾기 ───────────────────────────────────────────────────────────────────

def find(topic: str = "rejuran", limit: int | None = None) -> dict:
    cfg = settings()
    topics = cfg.get("topics", {})
    if topic not in topics:
        raise ReelsError(f"주제 없음: {topic} (config/reels.yaml topics: {', '.join(topics)})")
    limit = limit or int(cfg.get("per_message", 10))
    excerpt = int(cfg.get("caption_excerpt", 160))
    state = load_state()
    seen = state.setdefault("seen", {})
    pool, dropped, counted = {}, {}, set()
    max_age, min_likes = int(cfg.get("max_age_days", 365)), int(cfg.get("min_likes", 0))
    now = datetime.fromisoformat(now_iso())
    age_label = "1년 넘음" if max_age == 365 else f"{max_age}일 넘음"
    for tag in topics[topic].get("hashtags", []):
        tag_id = hashtag_id(tag, state)
        batches = [("top", top_media(tag_id, int(cfg.get("max_pages", 3))))]
        if cfg.get("recent", True):
            try:
                batches.append(("recent", recent_media(tag_id, int(cfg.get("recent_pages", 2)))))
            except ReelsError as e:  # 최근 게시물이 안 돼도 인기 게시물 후보는 보낸다
                log.warning("recent_media #%s 실패 — 인기 게시물만: %s", tag, e)
        for source, media in batches:
            for m in media:
                if m.get("id") in counted:
                    continue
                counted.add(m["id"])
                if m.get("media_type") not in VIDEO_TYPES and m.get("media_product_type") != "REELS":
                    dropped["영상 아님"] = dropped.get("영상 아님", 0) + 1
                    continue
                if too_old(m, max_age, now):
                    dropped[age_label] = dropped.get(age_label, 0) + 1
                    continue
                if isinstance(m.get("like_count"), int) and m["like_count"] < min_likes:
                    dropped["좋아요 적음"] = dropped.get("좋아요 적음", 0) + 1
                    continue
                if m["id"] in seen:
                    dropped["이미 보냄"] = dropped.get("이미 보냄", 0) + 1
                    continue
                reasons = exclusion_reasons(m.get("caption", ""), cfg.get("exclude", {}))
                if reasons:
                    for r in reasons:
                        dropped[REASON_LABEL.get(r, r)] = dropped.get(REASON_LABEL.get(r, r), 0) + 1
                    continue
                pool[m["id"]] = {**m, "tag": tag, "fresh": source == "recent",
                                "warn": exclusion_reasons(m.get("caption", ""), cfg.get("warn", {}))}
    picked = sorted(pool.values(), key=rank_key)[:limit]
    at = now_iso()
    items = [{"n": i, "id": m["id"], "permalink": m.get("permalink", ""), "tag": m["tag"],
              "like_count": m.get("like_count"), "comments_count": m.get("comments_count"),
              "posted": (m.get("timestamp") or "")[:10], "caption": _excerpt(m.get("caption", ""), excerpt),
              "warn": m.get("warn") or [], "fresh": bool(m.get("fresh"))}
             for i, m in enumerate(picked, 1)]
    for it in items:
        seen[it["id"]] = at
    media = state.setdefault("media", {})
    cutoff = now - timedelta(days=MEDIA_KEEP_DAYS)
    for mid in [k for k, v in media.items() if datetime.fromisoformat(v["at"]) < cutoff]:
        del media[mid]
    for it, m in zip(items, picked):  # transcribe 용 — 고른 후보만, 영상 주소는 목록·메시지에 내보내지 않는다
        media[it["id"]] = {"permalink": it["permalink"], "topic": topic, "tag": it["tag"], "at": at,
                           **({"media_url": m["media_url"]} if m.get("media_url") else {})}
        it["has_video_url"] = bool(m.get("media_url"))
    state["last"] = {"topic": topic, "label": topics[topic].get("label", topic), "at": at, "items": items,
                     "videos": len(counted), "dropped": dropped}
    save_state(state)
    log.info("릴스 후보 %s: 후보 %d개, 제외 %s", topic, len(items), dropped)
    return state["last"]


def _excerpt(caption: str, n: int) -> str:
    one = " ".join((caption or "").split())
    return one if len(one) <= n else one[:n].rstrip() + "…"


def message(last: dict) -> str:
    if not last:
        return "릴스 후보 없음 — python -m pipeline.reels find"
    items = last.get("items") or []
    dropped = ", ".join(f"{k} {v}" for k, v in sorted(last.get("dropped", {}).items(), key=lambda kv: -kv[1]))
    head = f"[릴스 후보] {last.get('label', '')} — 영상 {len(items)}개 (인기 + 최근 24시간, 좋아요순)"
    lines = [head + (f"\n제외: {dropped}" if dropped else ""), ""]
    if not items:
        lines.append("보낼 후보가 없습니다 — 해시태그를 늘리거나 다음 주에 다시.")
    for it in items:
        likes = f"❤ {it['like_count']:,}" if isinstance(it.get("like_count"), int) else "❤ 숨김"
        warn = "".join(f" · ⚠️ {REASON_LABEL.get(w, w)}" for w in it.get("warn") or [])
        warn += " · 🆕 최근 24시간" if it.get("fresh") else ""
        warn += " · 📼 녹화 필요" if it.get("has_video_url") is False else ""
        lines.append(f"{it['n']}. {likes} · 💬 {it.get('comments_count') or 0:,} · {it.get('posted', '')} · #{it['tag']}{warn}")
        lines.append(f"   {it['permalink']}")
        if it.get("caption"):
            lines.append(f"   {it['caption']}")
    lines += ["", "앱에서 확인: 리믹스 허용 · 고정 댓글의 'DM 드려요' 유도 · '정보 좀' 댓글에 단 답글 · 사람 태그 계정(병원·의사인지) · 병원 간판·이름 · 시술 장면(주사)·직후 피·자국 클로즈업 · "
              "시술자 칭찬('꼼꼼 시술') · 전후 비교 · 원작자 외 얼굴 · 영상 속 '제공받아' 같은 말",
              "고르면: python -m pipeline.reels transcribe <번호> (📼 = Instagram 이 영상 주소를 안 줌 → 화면 녹화를 --file 로)"]
    return "\n".join(lines)


def send(text: str) -> bool:
    return importlib.import_module("pipeline.worker").hermes_send(text)


# ── 점검 ───────────────────────────────────────────────────────────────────

def check() -> str:
    """연결 점검: 토큰 → 계정 → 해시태그 검색 → top_media. 단계마다 결과를 한 줄로."""
    out = []
    me = graph(user_id(), {"fields": "username"})
    out.append(f"✅ 계정: @{me.get('username', '?')}")
    tag = next(iter(next(iter(settings().get("topics", {}).values()), {}).get("hashtags", [])), "리쥬란")
    state = load_state()
    tag_id = hashtag_id(tag, state)
    save_state(state)
    out.append(f"✅ 해시태그 검색: #{tag}")
    media = top_media(tag_id)
    videos = sum(1 for m in media if m.get("media_type") in VIDEO_TYPES or m.get("media_product_type") == "REELS")
    out.append(f"✅ 인기 게시물 {len(media)}개 (영상 {videos}개)")
    recent = _recent_tags(state, datetime.fromisoformat(now_iso()))
    out.append(f"최근 7일 해시태그 {len(recent)}개 / 한도 {settings().get('weekly_hashtag_limit', 25)}")
    return "\n".join(out)


def _is_video(m: dict) -> bool:
    return m.get("media_type") in VIDEO_TYPES or m.get("media_product_type") == "REELS"


def probe(tags: list[str]) -> list[dict]:
    """해시태그마다 영상이 얼마나 나오는지 (인기 1페이지 + 최근 24시간 1페이지). 후보로 기록하지 않는다 —
    주제에 넣을 해시태그를 고르는 용도. 새 해시태그는 7일 한도에 센다."""
    cfg = settings()
    max_age = int(cfg.get("max_age_days", 365))
    now = datetime.fromisoformat(now_iso())
    state = load_state()
    rows = []
    try:
        for tag in tags:
            row = {"tag": tag.lstrip("#").strip()}
            try:
                tag_id = hashtag_id(tag, state)
            except ReelsError as e:
                rows.append({**row, "error": str(e)})
                continue
            for edge in ("top_media", "recent_media"):
                try:
                    media = hashtag_media(tag_id, edge, 1)
                except ReelsError as e:
                    row[edge] = {"error": str(e)}
                    continue
                videos = [m for m in media if _is_video(m)]
                fresh = [m for m in videos if not too_old(m, max_age, now)]
                clean = [m for m in fresh if not exclusion_reasons(m.get("caption", ""), cfg.get("exclude", {}))]
                row[edge] = {"posts": len(media), "videos": len(videos), "within_age": len(fresh), "clean": len(clean)}
            rows.append(row)
    finally:
        save_state(state)
    return rows


def probe_message(rows: list[dict]) -> str:
    lines = ["[해시태그 점검] 게시물 / 영상 / 기간 안 / 캡션 통과 (인기 1페이지 · 최근 24시간 1페이지)"]
    for r in rows:
        if r.get("error"):
            lines.append(f"#{r['tag']}: ⚠️ {r['error']}")
            continue
        parts = []
        for edge, name in (("top_media", "인기"), ("recent_media", "최근")):
            d = r.get(edge) or {}
            parts.append(f"{name} ⚠️ {d['error'][:80]}" if d.get("error") else
                         f"{name} {d.get('posts', 0)}/{d.get('videos', 0)}/{d.get('within_age', 0)}/{d.get('clean', 0)}")
        lines.append(f"#{r['tag']}: " + " · ".join(parts))
    return "\n".join(lines)


def traffic(days: int = 7, views: int | None = None, today=None, stats_fn=None, requests_fn=None) -> str:
    """릴스 성과 1순위 지표 = 인스타그램에서 홈페이지에 도착한 수 (2026-11-10 운영자 결정).
    프로필 링크(추적 링크, landing.link_label) 누름을 추적기에서 세고, /ig/ 페이지 요청(서버 기록, 봇 일부 포함 상한)을 같이 본다.
    views 를 주면 조회 1,000회당 도착 수도 (조회수는 앱 인사이트에서 운영자가 읽어 넣는다 — 지표 API 연결 전)."""
    from datetime import date
    tracker = importlib.import_module("pipeline.tracker")
    label = (settings().get("landing") or {}).get("link_label") or "Instagram profile"
    end = today or (date.today() - timedelta(days=1))
    start = end - timedelta(days=days - 1)
    prev_end, prev_start = start - timedelta(days=1), start - timedelta(days=days)
    stats_fn = stats_fn or tracker.stats
    lines = [f"[릴스 → 홈페이지 {start} ~ {end}]"]
    if stats_fn is tracker.stats and not tracker.configured():
        lines.append("추적기 미설정 (TRACKER_URL·TRACKER_TOKEN) — 프로필 링크 누름을 못 셈")
        now = prev = None
    else:
        def taps(data: dict) -> tuple[int | None, int]:
            own = [r for r in data.get("by_link", []) if (r.get("label") or "") == label]
            ig = sum(r["clicks"] for r in data.get("by_source", []) if r.get("source") == "instagram")
            return (sum(r["clicks"] for r in own) if own else None), ig
        now, ig_now = taps(stats_fn(start, end))
        prev, _ = taps(stats_fn(prev_start, prev_end))
        if now is None:
            lines.append(f"프로필 추적 링크('{label}') 클릭 기록 없음 — 아직 안 걸었으면: "
                         f"python -m pipeline.tracker add --target https://skinboundkorea.com/ig/ --label \"{label}\" "
                         "→ Instagram(?s=ig) 주소를 프로필 첫 번째 링크로")
        else:
            lines.append(f"① 홈페이지 도착 (프로필 링크 누름): {now}" + (f" · 지난 {days}일 {prev}" if prev is not None else ""))
        lines.append(f"   인스타그램에서 온 추적 링크 클릭 전체: {ig_now}")
        if views and now is not None:
            lines.append(f"   조회 1,000회당 도착 {now * 1000 / views:.2f} (조회 {views:,})")
    try:
        page = (requests_fn or importlib.import_module("pipeline.trend").page_requests)(start, end)
        lines.append(f"② /ig/ 페이지 요청: {page.get('/ig/', 0)} (서버 기록, 남은 봇이 있어 상한)")
    except Exception as e:  # noqa: BLE001 — Cloudflare 토큰이 없거나 보관 기간 밖
        lines.append(f"② /ig/ 페이지 요청: 확인 못 함 ({str(e)[:80]})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="리믹스 번역 릴스 — 원본 후보 찾기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("find")
    p.add_argument("topic", nargs="?", default="rejuran")
    p.add_argument("--limit", type=int)
    p.add_argument("--send", action="store_true", help="텔레그램으로 보낸다 (못 보내면 출력)")
    sub.add_parser("list")
    p = sub.add_parser("tags", help="해시태그별 영상 수 점검 (후보로 기록하지 않음)")
    p.add_argument("tags", nargs="+")
    p = sub.add_parser("transcribe", help="고른 영상 받아쓰기·번역·자막·캡션 (pipeline/reels_work.py)")
    p.add_argument("ref", nargs="?", help="list 의 후보 번호 또는 media id")
    p.add_argument("--file", help="운영자 화면 녹화 파일 (media_url 이 없을 때)")
    p.add_argument("--permalink", default="", help="--file 일 때 원본 릴스 주소")
    p.add_argument("--about", help="끝 문구의 시술 이름 (영어, 기본: 주제의 en)")
    p.add_argument("--keep", action="store_true", help="작업용 영상·프레임을 지우지 않음")
    p.add_argument("--force", action="store_true", help="선별 검사에 걸려도 번역까지 (오탐 확인용)")
    p = sub.add_parser("traffic", help="인스타그램 → 홈페이지 도착 (릴스 1순위 지표)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--views", type=int, help="같은 기간 릴스 조회 합계 (앱 인사이트) — 1,000회당 도착 계산")
    args = ap.parse_args(argv)
    if argv is None:
        load_env_file()  # 터미널에서 직접 실행해도 .env 의 Instagram 토큰을 읽게 — 이미 있는 환경변수는 그대로
        try:  # Windows 콘솔(cp949)에 없는 글자(—, 이모지)에서 출력이 멈추지 않게 (2026-11-10)
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        if args.cmd == "tags":
            print(probe_message(probe(args.tags)))
        elif args.cmd == "traffic":
            print(traffic(args.days, args.views))
        elif args.cmd == "transcribe":
            work = importlib.import_module("pipeline.reels_work")
            if not (args.ref or args.file):
                ap.error("transcribe: 후보 번호 또는 --file")
            try:
                result = work.run(args.ref, args.file, args.permalink, args.about, args.keep, args.force)
            except work.WorkError as e:
                raise ReelsError(str(e)) from e
            print(work.summary(result, work.work_dir(result["id"])))  # ⛔·⚠️ 판정도 정상 종료 (실패 = 오류일 때만)
        elif args.cmd == "check":
            print(check())
        elif args.cmd == "find":
            text = message(find(args.topic, args.limit))
            if not (args.send and send(text)):
                print(text)
        elif args.cmd == "list":
            print(message(load_state().get("last", {})))
    except ReelsError as e:
        log.error("%s", e)
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("reels", main)
