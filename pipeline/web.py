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
MEDICAL_REFERENCE_HOSTS = ("mayoclinic.org", "clevelandclinic.org", "plasticsurgery.org", "asds.net", "bad.org.uk",
                           "nhs.uk", "mskcc.org", "hopkinsmedicine.org", "nih.gov")
# 공공·대학 병원의 환자 안내문 (실무 수치 — 며칠·몇 주·SPF — 가 있는 출처, 2026-10-10): 공공·학술 도메인은 병원 사이트로 보지 않는다
PUBLIC_SUFFIXES = (".gov", ".edu", ".ac.uk", ".gov.uk", ".nhs.uk", ".gov.au", ".edu.au")


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_clinic_host(url_or_host: str) -> bool:
    h = host(url_or_host) if "://" in url_or_host else url_or_host.lower().removeprefix("www.")
    if any(h == r or h.endswith("." + r) for r in MEDICAL_REFERENCE_HOSTS) or h.endswith(PUBLIC_SUFFIXES):
        return False
    return bool(CLINIC_HOST.search(h))


PDF_MAX_BYTES = 15_000_000
PDF_MAX_PAGES = 40


# NCBI(PMC·PubMed) 웹 페이지는 2026-10 부터 자동 접근에 봇 확인(reCAPTCHA) 화면을 띄운다.
# 확인을 우회하지 않고, NCBI 가 프로그램 접근용으로 제공하는 공식 E-utilities 로 같은 논문 본문을 받는다.
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?tool=skin-marketing&"
PMC_ARTICLE = re.compile(r"^(?:pmc\.ncbi\.nlm\.nih\.gov/articles|(?:www\.)?ncbi\.nlm\.nih\.gov/pmc/articles)/(PMC\d+)", re.I)
PUBMED_ARTICLE = re.compile(r"^pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)


def ncbi_api_url(url: str) -> str | None:
    """PMC·PubMed 논문 주소 → E-utilities 주소 (본문 대조는 원래 주소 기준 그대로)."""
    path = re.sub(r"^https?://", "", url.strip())
    if m := PMC_ARTICLE.match(path):
        return EUTILS + f"db=pmc&id={m.group(1)}&rettype=xml"
    if m := PUBMED_ARTICLE.match(path):
        return EUTILS + f"db=pubmed&id={m.group(1)}&rettype=abstract&retmode=text"
    return None


def fetch_page(url: str) -> tuple[int, str]:
    """(HTTP 상태, 본문 텍스트). 연결 실패는 상태 0. PDF(FDA 문서·논문 등)는 글자를 뽑는다.
    PMC·PubMed 논문은 E-utilities 로 받는다 (웹 페이지는 봇 확인 화면)."""
    if api := ncbi_api_url(url):
        status, text = _fetch(api)
        if status == 200 and text.lstrip().startswith("<"):
            text = html_to_text(re.sub(r"(?is)<(ref-list|back)\b.*?</\1>", " ", text))  # 참고문헌 목록은 뺀다
        return status, text
    status, text = _fetch(url)
    if status == 200 and text and (pid := oliveyoung_product_id(url)) and (info := oliveyoung_product_info(pid)):
        # 제목 바로 뒤에 — 페이지 뒤쪽은 메뉴·리뷰라 작성·검수에 넘기는 앞부분(12,000자)에서 잘린다
        head, sep, rest = text.partition(OLIVE_YOUNG_TITLE_END)
        text = f"{head}{sep}{info}{rest}" if sep else text + info
    return status, text


# 올리브영 글로벌 제품 페이지는 전성분·사용 대상 표가 화면에서 따로 불러와져 HTML 에 없다 (2026-11-14: 보습제 글 표가
# 전부 "—" 였다). 같은 페이지가 쓰는 공개 데이터에서 그 표를 받아 본문 끝에 붙인다 — 화장품법상 판매 페이지에 싣는 표시 정보.
OLIVE_YOUNG_INFO = "https://global.oliveyoung.com/product/description-info"
OLIVE_YOUNG_PRODUCT = re.compile(r"^https://global\.oliveyoung\.com/product/detail\?(?:.*&)?prdtNo=([A-Z]{2}\d{6,})", re.I)
OLIVE_YOUNG_TITLE_END = "| OLIVE YOUNG Global"
OLIVE_YOUNG_FIELDS = ("Content volume or weight", "Ideal for", "Ingredients")  # 연락처·제조사 줄은 넣지 않는다


def oliveyoung_product_id(url: str) -> str | None:
    m = OLIVE_YOUNG_PRODUCT.match((url or "").strip())
    return m.group(1).upper() if m else None


def oliveyoung_product_info(product_id: str) -> str:
    """" Product information: Ideal for: … Ingredients: …" — 못 받으면 빈 문자열 (출처는 화면 글자만으로 그대로)."""
    import json
    req = urllib.request.Request(OLIVE_YOUNG_INFO, data=json.dumps({"prdtNo": product_id}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read(500_000) or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    rows = {str(r.get("codeDtlName", "")): str(r.get("itemCont") or "") for r in (data.get("description") or [])
            if isinstance(r, dict)}
    parts = [name + ": " + re.sub(r"\s+", " ", html.unescape(rows[name])).strip() for name in OLIVE_YOUNG_FIELDS
             if rows.get(name, "").strip()]
    return (" Product information (Olive Young Global): " + " ".join(f"{x.rstrip('.')}." for x in parts)) if parts else ""


def _fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get_content_type()
            pdf = ctype == "application/pdf" or urlparse(url).path.lower().endswith(".pdf")
            raw = resp.read(PDF_MAX_BYTES if pdf else 2_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            status = resp.status
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""
    if pdf or raw[:5] == b"%PDF-":
        return status, pdf_to_text(raw)
    return status, html_to_text(raw.decode(charset, errors="replace"))


def pdf_to_text(raw: bytes) -> str:
    """PDF 앞쪽 PDF_MAX_PAGES 쪽의 글자. 스캔 이미지뿐인 PDF·깨진 파일은 빈 문자열 (→ 사람이 확인)."""
    import io
    import logging
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # 글꼴 인코딩 경고는 추출 결과와 무관
    try:
        reader = PdfReader(io.BytesIO(raw))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:PDF_MAX_PAGES])
    except Exception:  # noqa: BLE001 — 암호화·손상 PDF
        return ""
    return re.sub(r"\s+", " ", text).strip()


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
