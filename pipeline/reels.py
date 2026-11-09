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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from pipeline.common import content_dir, get_logger, load_env_file, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("reels")

GRAPH = "https://graph.facebook.com"
MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,like_count,comments_count,timestamp"
VIDEO_TYPES = {"VIDEO", "REELS"}
# "광고 아님", "협찬X", "체험단 아니고" 같은 부정 표현 — 제외 검사 전에 지운다 (내돈내산 글이 빠지지 않게)
NEGATION = re.compile(r"(?:(?:협찬|광고|체험단|수수료|원고료|제공)\s*[•·・,/&\s]*)+(은|이)?\s*"
                      r"(아님|아닙니다|아니에요|아니고|아니며|아니예요|[xX]|❌|🙅\S*|없음|없어요|없습니다|0원)")
REASON_LABEL = {"sponsored": "협찬·체험단", "clinic": "병원·의사", "dm_funnel": "DM 유도", "promo": "공구·할인 홍보",
                "before_after": "전후 비교"}

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
        with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            api_code = (json.loads(body).get("error") or {}).get("code")
        except ValueError:
            api_code = None
        raise ReelsError(f"GET {path} → {e.code}: {_explain(body)}", status=e.code, code=api_code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
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
    """Meta 가 '요청하는 데이터 양을 줄이라'고 할 때 (HTTP 500 · code 1, 2026-11-08 실제 응답)."""
    return e.code == 1 or e.status == 500


def top_media(tag_id: str, pages: int = 1, page_size: int | None = None) -> list[dict]:
    """해시태그 인기 게시물 (pages 페이지까지 — 다음 페이지는 after 커서).
    '데이터 양을 줄이라'(500·code 1)면 페이지 크기를 반으로 줄여 다시 (최소 10).
    첫 페이지 뒤의 페이지가 실패하면 받은 데까지만 쓴다 — 페이지 하나 때문에 찾기 전체가 멈추지 않게."""
    size = page_size or int(settings().get("page_size", 25))
    out, after = [], None
    for page in range(max(1, pages)):
        while True:
            params = {"user_id": user_id(), "fields": MEDIA_FIELDS, "limit": size}
            if after:
                params["after"] = after
            try:
                data = graph(f"{tag_id}/top_media", params)
                break
            except ReelsError as e:
                if _too_much_data(e) and size > 10:
                    size = max(10, size // 2)
                    log.warning("top_media 데이터 양 줄임 → 페이지당 %d개로 다시", size)
                    continue
                if page > 0:
                    log.warning("top_media %d페이지 실패 — 받은 %d개까지만 씀: %s", page + 1, len(out), e)
                    return out
                raise
        out += data.get("data") or []
        after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (data.get("paging") or {}).get("next"):
            break
    return out


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
        for m in top_media(hashtag_id(tag, state), int(cfg.get("max_pages", 3))):
            if m.get("id") in counted:
                continue
            counted.add(m["id"])
            if m.get("media_type") not in VIDEO_TYPES and m.get("media_product_type") != "REELS":
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
            pool[m["id"]] = {**m, "tag": tag, "warn": exclusion_reasons(m.get("caption", ""), cfg.get("warn", {}))}
    picked = sorted(pool.values(), key=rank_key)[:limit]
    at = now_iso()
    items = [{"n": i, "id": m["id"], "permalink": m.get("permalink", ""), "tag": m["tag"],
              "like_count": m.get("like_count"), "comments_count": m.get("comments_count"),
              "posted": (m.get("timestamp") or "")[:10], "caption": _excerpt(m.get("caption", ""), excerpt),
              "warn": m.get("warn") or []}
             for i, m in enumerate(picked, 1)]
    for it in items:
        seen[it["id"]] = at
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
    head = f"[릴스 후보] {last.get('label', '')} — 인기 영상 {len(items)}개 (좋아요순)"
    lines = [head + (f"\n제외: {dropped}" if dropped else ""), ""]
    if not items:
        lines.append("보낼 후보가 없습니다 — 해시태그를 늘리거나 다음 주에 다시.")
    for it in items:
        likes = f"❤ {it['like_count']:,}" if isinstance(it.get("like_count"), int) else "❤ 숨김"
        warn = "".join(f" · ⚠️ {REASON_LABEL.get(w, w)}" for w in it.get("warn") or [])
        lines.append(f"{it['n']}. {likes} · 💬 {it.get('comments_count') or 0:,} · {it.get('posted', '')} · #{it['tag']}{warn}")
        lines.append(f"   {it['permalink']}")
        if it.get("caption"):
            lines.append(f"   {it['caption']}")
    lines += ["", "앱에서 확인: 리믹스 허용 · 고정 댓글의 'DM 드려요' 유도 · '정보 좀' 댓글에 단 답글 · 사람 태그 계정(병원·의사인지) · 병원 간판·이름 · 시술 장면(주사)·직후 피·자국 클로즈업 · "
              "시술자 칭찬('꼼꼼 시술') · 전후 비교 · 원작자 외 얼굴 · 영상 속 '제공받아' 같은 말"]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="리믹스 번역 릴스 — 원본 후보 찾기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("find")
    p.add_argument("topic", nargs="?", default="rejuran")
    p.add_argument("--limit", type=int)
    p.add_argument("--send", action="store_true", help="텔레그램으로 보낸다 (못 보내면 출력)")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if argv is None:
        load_env_file()  # 터미널에서 직접 실행해도 .env 의 Instagram 토큰을 읽게 — 이미 있는 환경변수는 그대로
    try:
        if args.cmd == "check":
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
