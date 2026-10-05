"""웹 페이지 수집·판별 — 초안 작성(02_draft)과 자동 검수(02b)가 같은 기준을 쓴다.

- fetch_page: (HTTP 상태, 본문 텍스트)
- source_url: Gemini 검색 연동의 임시 리디렉션 주소 → 실제 출처 주소
- is_clinic_host: 병원 사이트로 보이는 도메인 (병원 자체 사이트는 출처로 쓰지 않는다, _rules.md)
- quote_in_page: 사실의 근거 인용문이 페이지 본문에 실제로 있는지
"""

from __future__ import annotations

import html
import re
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlparse

FETCH_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; skin-marketing-source-check/1.0)"

# Gemini 검색 연동이 주는 임시 리디렉션 주소 — 만료되므로 실제 출처 주소로 바꿔 저장한다
REDIRECT_HOSTS = {"vertexaisearch.cloud.google.com"}

# 도메인으로 추정하므로 검수에서는 ⚠️ 주의 (사람이 판단), 출처 수집에서는 제외
CLINIC_HOST = re.compile(r"clinic|derma|hospital|plastic|surgery|aesthetic|medispa", re.I)
# 도메인에 clinic·surgery 등이 들어가도 공신력 있는 의학 정보원·학회
MEDICAL_REFERENCE_HOSTS = ("mayoclinic.org", "clevelandclinic.org", "plasticsurgery.org", "asds.net", "bad.org.uk")


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_clinic_host(url_or_host: str) -> bool:
    h = host(url_or_host) if "://" in url_or_host else url_or_host.lower().removeprefix("www.")
    if any(h == r or h.endswith("." + r) for r in MEDICAL_REFERENCE_HOSTS):
        return False
    return bool(CLINIC_HOST.search(h))


def fetch_page(url: str) -> tuple[int, str]:
    """(HTTP 상태, 본문 텍스트). 연결 실패는 상태 0."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read(2_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            ctype = resp.headers.get_content_type()
            status = resp.status
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""
    if ctype == "application/pdf":
        return status, ""  # PDF 본문 대조는 사람 확인
    return status, html_to_text(raw.decode(charset, errors="replace"))


_fetcher = fetch_page


def set_fetcher(fn=None) -> None:
    """테스트용: 페이지 수집 함수를 바꾼다 (None 이면 기본)."""
    global _fetcher
    _fetcher = fn or fetch_page


def get(url: str) -> tuple[int, str]:
    """출처 수집용 진입점 (set_fetcher 로 바꿀 수 있다)."""
    return _fetcher(url)


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


# ---- 리디렉션 해석 ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 따라가지 않고 3xx 를 HTTPError 로 받는다


def resolve_redirect(url: str) -> str | None:
    """리디렉션이 가리키는 출처 주소 (Location). 목적지 페이지에는 접속하지 않는다. 실패는 None."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=FETCH_TIMEOUT).close()
        return None  # 리디렉션이 아니면 원래 출처를 알 수 없다
    except urllib.error.HTTPError as e:
        target = e.headers.get("Location", "") if 300 <= e.code < 400 else ""
    except (urllib.error.URLError, OSError, ValueError):
        return None
    ok = target.startswith("http") and host(target) not in REDIRECT_HOSTS
    return target if ok else None


_resolve = resolve_redirect


def set_resolver(fn=None) -> None:
    """테스트용: 리디렉션 해석 함수를 바꾼다 (None 이면 기본)."""
    global _resolve
    _resolve = fn or resolve_redirect


def source_url(url: str) -> str | None:
    if host(url) not in REDIRECT_HOSTS:
        return url
    return _resolve(url)


# ---- 인용문 대조 ----

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[‘’‚′`´]", "'", text)
    text = re.sub(r"[“”„″]", '"', text)
    text = re.sub(r"[‐‑‒–—―]", "-", text)
    text = re.sub(r"[^\w%$₩.,'\"-]+", " ", text)  # 기호·줄바꿈 차이는 무시
    return re.sub(r"\s+", " ", text).strip()


def quote_in_page(quote: str, page: str) -> bool:
    """인용문(공백·따옴표·대시 차이 무시)이 페이지 본문에 그대로 있는지. 너무 짧은 인용은 근거로 인정하지 않는다."""
    q = _normalize(quote).strip(" .,\"'")
    return len(q) >= 20 and q in _normalize(page)
