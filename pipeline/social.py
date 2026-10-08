"""X·Threads 텍스트 게시 — 게시된 블로그 글마다 짧은 소개 글을 만들어 올린다.

흐름: 승인된(=블로그 게시된) 중립 글 → 소개 글 생성(stage social, 사실 목록만) → 규칙 검사 → 텔레그램으로 보냄
  - X: 기본은 **작성 링크(intent)** — 텔레그램의 링크를 누르면 글이 채워진 X 작성 화면이 열리고 사람이 [게시]를 누른다.
       X API 는 유료(게시 1건 약 $0.015, 링크 포함 약 $0.20)라 주 5편 수준에서는 쓰지 않는다. 키를 넣고
       config/channels.yaml text_social.x.mode: api 로 바꾸면 API 로도 올릴 수 있다 (OAuth 1.0a 사용자 토큰).
       웹 화면을 자동 조작해 올리는 방식은 X 약관(공개 인터페이스 외 자동 접근 금지) 위반·계정 정지 위험이라 만들지 않는다.
  - Threads: Threads API(무료, THREADS_ACCESS_TOKEN — 우리 계정을 앱 테스터로 등록하면 앱 심사 없이 사용)로 올린다.
       토큰은 60일 만료 → 50일마다 자동 갱신해 ~/.skinbound/threads_token.json (권한 600)에 저장. 키가 없으면 작성 링크.
  - 스폰서 글은 channels.yaml sponsored_policy 에서 x·threads 를 허용하기 전까지 만들지 않는다.

    python -m pipeline.social compose            # 새 글의 소개 글 생성·검사
    python -m pipeline.social list               # 텔레그램용: 번호·글·작성 링크·상태
    python -m pipeline.social post 2 threads     # API 게시 (번호 또는 draft_id)
    python -m pipeline.social done 2 x https://x.com/skinboundkorea/status/...   # 손으로 올린 게시 기록
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
from datetime import datetime, timezone
from pathlib import Path

from pipeline import llm, sponsors
from pipeline.common import (
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
PUBLIC_STATES = ("approved", "rendered", "ready_to_publish", "published")
URL_WEIGHT = 23  # X 는 링크를 길이와 관계없이 23자로 센다
INTENT = {"x": "https://x.com/intent/post?text={text}", "threads": "https://www.threads.net/intent/post?text={text}"}
POST_HOSTS = {"x": ("x.com", "twitter.com"), "threads": ("threads.net", "threads.com")}
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)


class SocialError(RuntimeError):
    pass


def settings() -> dict:
    cfg = load_yaml("channels.yaml").get("text_social") or {}
    return {"x": {"mode": "intent", "max_chars": 280, **(cfg.get("x") or {})},
            "threads": {"mode": "api", "max_chars": 500, **(cfg.get("threads") or {})}}


def blog_url(draft: dict) -> str:
    site = load_yaml("site.yaml")
    slug = slugify(draft["blog"].get("slug") or draft["blog"].get("title") or draft["id"], 60)
    return f"https://{site['domain']}/{slug}/" if site.get("domain") else f"/{slug}/"


def footer(url: str) -> str:
    return f"\n\nAI-assisted guide with sources: {url}"


def x_length(text: str) -> int:
    return len(URL.sub("x" * URL_WEIGHT, text))


# ---------------- 생성·검사 ----------------

def allowed(draft: dict) -> bool:
    """스폰서 글은 X·Threads 광고 정책을 확인해 sponsored_policy 에 허용하기 전까지 제외."""
    if not draft.get("sponsor"):
        return True
    policy = sponsors.platform_policy()
    return all(ch in policy["allowed"] for ch in CHANNELS)


def check(draft: dict, texts: dict[str, str]) -> list[str]:
    """금지 표현·병원명·연락처·출처 없는 수치·링크·길이 (02b 와 같은 목록)."""
    import importlib
    review = importlib.import_module("pipeline.02b_auto_review")
    problems = []
    fact_text = " ".join(f.get("text", "") for f in draft.get("facts", []))
    known_numbers = set(NUMBER.findall(fact_text))
    cfg = settings()
    for ch, text in texts.items():
        for term, pat in review.load_banned():
            if pat.search(text):
                problems.append(f"[{ch}] 금지 표현: {term}")
        for kind, label, pat in review.RULE_PATTERNS:
            m = pat.search(text)
            if m and not (kind == "clinic_name" and (review.INSTITUTION.search(m.group(0)) or review._generic(m.group(0)))):
                problems.append(f"[{ch}] {label}: {m.group(0)}")
        if URL.search(text):
            problems.append(f"[{ch}] 본문에 링크 (블로그 링크는 코드가 붙인다)")
        for n in NUMBER.findall(text):
            if n not in known_numbers:
                problems.append(f"[{ch}] 사실 목록에 없는 수치: {n}")
        limit = cfg[ch]["max_chars"] - (URL_WEIGHT if ch == "x" else len(blog_url(draft))) - len(footer("").rstrip())
        size = x_length(text) if ch == "x" else len(text)
        if size > limit:
            problems.append(f"[{ch}] 너무 김 ({size}/{limit}자)")
    if not draft.get("sponsor"):
        problems += [f.message for f in review.check_no_sponsor_names(texts)]
    return problems


def generate(draft: dict) -> dict:
    cfg = settings()
    url = blog_url(draft)
    room = {ch: cfg[ch]["max_chars"] - (URL_WEIGHT if ch == "x" else len(url)) - len(footer("").rstrip()) for ch in CHANNELS}
    text = prompt("social_post", title=draft["blog"].get("title", ""), hook=(draft.get("shortform") or {}).get("hook", ""),
                  facts=facts_text(draft.get("facts", [])), x_chars=str(room["x"]), threads_chars=str(room["threads"]))
    data, _ = llm.generate_json("social", text)
    if not isinstance(data, dict):
        raise llm.LLMError("소개 글 형식 오류")
    return {ch: str(data.get(ch) or "").strip() for ch in CHANNELS}


def compose(path: Path) -> dict:
    """소개 글 생성 + 검사 (실패하면 1번 다시 생성). social.json 저장."""
    draft = load_draft(path)
    texts, problems = {}, ["생성 안 됨"]
    for _ in range(2):
        texts = generate(draft)
        problems = check(draft, texts)
        if not problems:
            break
    url = blog_url(draft)
    data = {"draft_id": draft["id"], "title": draft["blog"].get("title", ""), "created_at": now_iso(), "blog_url": url,
            "status": "ready" if not problems else "blocked", "problems": problems,
            "posts": {ch: {"text": texts[ch] + footer(url), "posted": None} for ch in CHANNELS}}
    save_json(path / "social.json", data)
    return data


def pending_paths() -> list[Path]:
    return [p for state in PUBLIC_STATES for p in draft_dirs(state)
            if (p / "draft.json").exists() and not (p / "social.json").exists() and allowed(load_draft(p))]


def compose_pending(limit: int = 5) -> tuple[list[str], list[str]]:
    done, failed = [], []
    for path in pending_paths()[:limit]:
        try:
            compose(path)
            done.append(path.name)
        except llm.LLMError as e:
            failed.append(f"{path.name}: {e}")
    return done, failed


# ---------------- 목록·기록 ----------------

def items() -> list[tuple[Path, dict]]:
    out = []
    for state in PUBLIC_STATES:
        for p in draft_dirs(state):
            if (p / "social.json").exists():
                data = load_json(p / "social.json")
                if any(not v["posted"] for v in data["posts"].values()):
                    out.append((p, data))
    return sorted(out, key=lambda x: x[1]["created_at"])


def intent_link(channel: str, text: str) -> str:
    return INTENT[channel].format(text=urllib.parse.quote(text, safe=""))


def list_message() -> str:
    rows = items()
    if not rows:
        return ""
    cfg = settings()
    lines = ["[X·Threads 소개 글] 작성 링크를 누르면 글이 채워진 화면이 열립니다 → [게시] 후 'N 게시 완료 x <주소>'"]
    for i, (path, data) in enumerate(rows, 1):
        if data["status"] == "blocked":
            lines.append(f"{i}. ⛔ {data['title']} — " + "; ".join(data["problems"][:3]))
            continue
        lines.append(f"{i}. {data['title']}")
        for ch in CHANNELS:
            post = data["posts"][ch]
            if post["posted"]:
                lines.append(f"   {ch} ✅ {post['posted']['url']}")
            elif ch == "threads" and cfg[ch]["mode"] == "api" and threads_configured():
                lines.append(f"   threads: '{i} 스레드 올려' (API)\n   {post['text']}")
            elif ch == "x" and cfg[ch]["mode"] == "api" and x_configured():
                lines.append(f"   x: '{i} X 올려' (API, 유료)\n   {post['text']}")
            else:
                lines.append(f"   {ch}: {intent_link(ch, post['text'])}")
    return "\n".join(lines)


def resolve(ref: str) -> Path:
    rows = items()
    if ref.isdigit() and 1 <= int(ref) <= len(rows):
        return rows[int(ref) - 1][0]
    for state in PUBLIC_STATES:
        p = content_dir(state) / ref
        if (p / "social.json").exists():
            return p
    raise SocialError(f"소개 글을 찾을 수 없습니다: {ref}")


def record(path: Path, channel: str, url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    if urllib.parse.urlparse(url).scheme != "https" or not any(host == h or host.endswith("." + h) for h in POST_HOSTS[channel]):
        raise SocialError(f"{channel} 게시물 주소가 아닙니다: {url}")
    data = load_json(path / "social.json")
    data["posts"][channel]["posted"] = {"url": url, "at": now_iso()}
    save_json(path / "social.json", data)
    return f"✅ {data['title']} — {channel} 게시 기록"


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


def x_post(text: str) -> str:
    if not x_configured():
        raise SocialError("X API 키가 없습니다 — 작성 링크로 올리세요 (X API 는 유료)")
    body = json.dumps({"text": text}).encode()
    res = _http("POST", X_API, headers={"authorization": _oauth1_header("POST", X_API), "content-type": "application/json"},
                body=body)
    return f"https://x.com/i/web/status/{res['data']['id']}"


def post(path: Path, channel: str) -> str:
    data = load_json(path / "social.json")
    if data["status"] != "ready":
        raise SocialError(f"검사를 통과하지 못한 글입니다: {'; '.join(data['problems'][:3])}")
    if data["posts"][channel]["posted"]:
        return f"이미 게시됨: {data['posts'][channel]['posted']['url']}"
    url = (threads_post if channel == "threads" else x_post)(data["posts"][channel]["text"])
    return record(path, channel, url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X·Threads 소개 글")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compose")
    sub.add_parser("list")
    p = sub.add_parser("post")
    p.add_argument("ref")
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
                print(f"⚠️ 소개 글 생성 실패 {f}")
            if done:
                print(list_message())
            return 1 if failed and not done else 0
        if args.cmd == "list":
            print(list_message() or "올릴 소개 글 없음")
            return 0
        path = resolve(args.ref)
        print(post(path, args.channel) if args.cmd == "post" else record(path, args.channel, args.url))
        return 0
    except SocialError as e:
        print(f"❓ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    run_cli("social", main)
