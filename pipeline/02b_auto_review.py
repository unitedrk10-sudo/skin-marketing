"""2.5 자동 검수 — 기획서 6-0. 규칙 검사 → 출처 검증 → 교차 모델 검수 → 등급.

    python -m pipeline.02b_auto_review                 # drafts/ 중 review.json 없는 초안 전부
    python -m pipeline.02b_auto_review <draft_id> ...  # 지정 초안
    python -m pipeline.02b_auto_review --auto-regenerate   # ⛔ 차단 건은 1회 자동 재생성 후 재검수

등급: pass(✅) / caution(⚠️) / block(⛔). 결과는 <draft>/review.json, 요약은 stdout.
종료 코드는 검수 등급과 무관하게 실행 성공이면 0.
"""

from __future__ import annotations

import argparse
import html
import importlib
import re
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline import llm
from pipeline.common import (
    CONFIG_DIR,
    blog_text,
    content_dir,
    draft_dirs,
    facts_text,
    find_draft,
    get_logger,
    load_draft,
    now_iso,
    prompt,
    read_prompt,
    render,
    run_cli,
    save_json,
    script_text,
)

log = get_logger("02b_auto_review")

GRADE_ICON = {"pass": "✅", "caution": "⚠️", "block": "⛔"}
FETCH_TIMEOUT = 20
PAGE_CHARS = 15000
USER_AGENT = "Mozilla/5.0 (compatible; skin-marketing-source-check/1.0)"
AI_DISCLOSURE = re.compile(r"AI[- ]generated", re.I)
BLOG_DISCLOSURE = re.compile(r"produced with AI assistance", re.I)
FACT_REF = re.compile(r"\[(F\d+)\]")


@dataclass
class Finding:
    check: str  # rules | sources | cross
    severity: str  # block | caution | info
    message: str
    quote: str = ""


# ---------------- ① 규칙 검사 (LLM 없음) ----------------

RULE_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("clinic_name", "병원명으로 보이는 표현",
     re.compile(r"\b(?:[A-Z][\w&'.-]*\s+){1,4}(?:Clinic|Dermatology|Derma|Hospital|Medical Center|Skin Center|Aesthetic[s]?)\b")),
    ("clinic_name", "병원명으로 보이는 표현 (한글)", re.compile(r"[가-힣A-Za-z]{2,}(?:의원|피부과의원|클리닉|성형외과|병원)")),
    ("doctor_name", "의사명으로 보이는 표현", re.compile(r"\bDr\.?\s+[A-Z][a-z]+|[가-힣]{2,4}\s?원장")),
    ("phone", "전화번호", re.compile(r"\+82[\s-]?\d|\b0\d{1,2}-\d{3,4}-\d{4}\b|\(\d{3}\)\s?\d{3}-\d{4}")),
    ("booking", "예약·메신저 연락처",
     re.compile(r"(?:pf|open)\.kakao\.com|(?:wa|line)\.me/|kakao(?:talk)?\s*(?:id|channel)\b|whatsapp\s*[:+]\s*\d"
                r"|https?://\S*(?:/book|/reserv|booking)", re.I)),
    ("credential", "자격·감수 표기 (허위 위험)",
     re.compile(r"board[- ]certified|medically reviewed|reviewed by (?:a )?(?:dr|doctor|dermatologist)|전문의 감수", re.I)),
    ("testimonial", "1인칭 체험담",
     re.compile(r"\bI\s+(?:got|tried|had|did|received|booked|went)\b|\bmy\s+(?:skin|treatment|experience|results?|face)\b"
                r"|\bwhen I\b|제가\s?받아", re.I)),
]

# 병원명 패턴에 걸려도 학회·기관·학술지 이름이면 제외 (예: American Academy of Dermatology)
# 제목식 대문자 표기의 일반 명사구 (예: "How To Choose A Clinic") 는 병원명이 아니다
GENERIC_WORDS = {
    "a", "an", "the", "your", "any", "each", "this", "that", "which", "what", "how", "to", "at", "in", "of", "for",
    "choose", "choosing", "find", "finding", "pick", "picking", "visit", "visiting", "before", "after", "and", "or",
    "korean", "korea", "seoul", "gangnam", "local", "licensed", "reputable", "right", "good", "skin", "dermatology",
    "derma", "aesthetic", "aesthetics", "medical", "cosmetic", "most", "many", "some", "every",
}
INSTITUTION = re.compile(r"\b(?:Academy|Association|Society|Journal|College|Institute|Ministry|Agency|Administration|Board)\b", re.I)

PRICE = re.compile(r"[$₩฿]|\b(?:USD|KRW|SGD|THB|CAD|won|dollars?)\b|\bprice|\bcost", re.I)


def load_banned(path: Path | None = None) -> list[tuple[str, re.Pattern]]:
    out = []
    for raw in (path or CONFIG_DIR / "banned_terms.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append((line[3:], re.compile(line[3:], re.I)))
        else:
            esc = re.escape(line)
            # 단어 문자로 시작/끝나는 항목만 경계 적용 (한글·기호 포함 항목은 부분 일치)
            left = r"\b" if re.match(r"[A-Za-z0-9]", line) else ""
            right = r"\b" if re.search(r"[A-Za-z0-9]$", line) and not line.endswith("%") else ""
            out.append((line, re.compile(left + esc + right, re.I)))
    return out


def content_texts(draft: dict) -> dict[str, str]:
    """검사 대상 본문 (출처 목록은 제외 — 출처 URL·제목에 병원명이 있어도 본문 노출이 아님)."""
    sf = draft.get("shortform") or {}
    blog = draft.get("blog") or {}
    script = "\n".join(
        [sf.get("title", ""), sf.get("hook", "")]
        + [f"{ln.get('voice', '')}\n{ln.get('caption', '')}\n{ln.get('visual', '')}" for ln in sf.get("lines", [])]
        + sf.get("hashtags", [])
    )
    return {
        "script": script,
        "blog": "\n".join([blog.get("title", ""), blog.get("meta_description", ""), blog.get("markdown", "")]),
    }


def _generic(phrase: str) -> bool:
    *words, _suffix = phrase.split()
    return all(w.lower().strip("'.-") in GENERIC_WORDS for w in words)


def _snippet(text: str, m: re.Match, pad: int = 30) -> str:
    return text[max(0, m.start() - pad) : m.end() + pad].replace("\n", " ").strip()


def check_rules(draft: dict, banned: list[tuple[str, re.Pattern]] | None = None) -> tuple[list[Finding], set[str]]:
    """반환: (발견 목록, 항상 사람 검수 사유)"""
    banned = banned if banned is not None else load_banned()
    findings: list[Finding] = []
    human: set[str] = set()
    texts = content_texts(draft)
    fact_urls = {f.get("url", "") for f in draft.get("facts", [])}

    for where, text in texts.items():
        for term, pat in banned:
            for m in pat.finditer(text):
                findings.append(Finding("rules", "block", f"[{where}] 금지 표현: {term}", _snippet(text, m)))
        for kind, label, pat in RULE_PATTERNS:
            for m in pat.finditer(text):
                if kind == "clinic_name" and (INSTITUTION.search(_snippet(text, m)) or _generic(m.group(0))):
                    continue
                findings.append(Finding("rules", "block", f"[{where}] {label}", _snippet(text, m)))
                if kind in ("clinic_name", "doctor_name"):
                    human.add("병원·의사 언급")
        for m in re.finditer(r"https?://[^\s)\]>\"']+", text):
            if m.group(0).rstrip(".,") not in fact_urls:
                findings.append(Finding("rules", "block", f"[{where}] 출처 목록에 없는 URL", m.group(0)))
        if PRICE.search(text):
            human.add("가격 포함")

    sf = draft.get("shortform") or {}
    if not AI_DISCLOSURE.search(sf.get("on_screen_disclosure", "")):
        findings.append(Finding("rules", "block", "[script] 영상 내 'AI-generated' 표기 없음 (AI 기본법 §31)"))
    if not BLOG_DISCLOSURE.search(texts["blog"]):
        findings.append(Finding("rules", "block", "[blog] AI 활용·출처 기반 고지 문장 없음"))

    # 출처 연결: 모든 사실 참조가 실제 사실 목록에 있어야 한다
    known = {f["id"] for f in draft.get("facts", [])}
    refs = set(FACT_REF.findall(texts["blog"]))
    for ln in sf.get("lines", []):
        refs |= set(ln.get("fact_ids", []))
    for fid in sorted(refs - known):
        findings.append(Finding("rules", "block", f"존재하지 않는 사실 참조 {fid} (출처 없는 주장)"))
    if not FACT_REF.search(texts["blog"]):
        findings.append(Finding("rules", "block", "[blog] 사실 참조 [F#] 가 하나도 없음"))
    for i, ln in enumerate(sf.get("lines", []), 1):
        if re.search(r"\d", ln.get("voice", "")) and not ln.get("fact_ids"):
            findings.append(Finding("rules", "block", f"[script] {i}번 줄 수치에 출처 없음", ln.get("voice", "")))
    return findings, human


# ---------------- ② 출처 검증 ----------------

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


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def check_sources(draft: dict, fetch=fetch_page) -> tuple[list[Finding], list[dict]]:
    """사용된 사실만 검증. 404·연결 실패·본문 불일치는 차단, 봇 차단(401/403/429)·PDF 는 주의."""
    texts = content_texts(draft)
    used = set(FACT_REF.findall(texts["blog"]))
    for ln in (draft.get("shortform") or {}).get("lines", []):
        used |= set(ln.get("fact_ids", []))
    by_url: dict[str, list[dict]] = defaultdict(list)
    for f in draft.get("facts", []):
        if f["id"] in used:
            by_url[f["url"]].append(f)

    findings: list[Finding] = []
    details: list[dict] = []
    for url, facts in by_url.items():
        ids = ",".join(f["id"] for f in facts)
        status, page = fetch(url)
        entry = {"url": url, "status": status, "facts": {}}
        details.append(entry)
        if status in (401, 403, 429):
            findings.append(Finding("sources", "caution", f"출처 접속 차단({status}) — 사람이 확인 [{ids}]", url))
            continue
        if status != 200:
            findings.append(Finding("sources", "block", f"출처 접속 실패({status or '연결 오류'}) [{ids}]", url))
            continue
        if len(page) < 200:
            findings.append(Finding("sources", "caution", f"출처 본문 추출 불가(PDF·스크립트 렌더링 등) — 사람이 확인 [{ids}]", url))
            continue
        text = render(read_prompt("source_check"), url=url, page=page[:PAGE_CHARS],
                      claims="\n".join(f"[{f['id']}] {f['text']}" for f in facts))
        data, _ = llm.generate_json("source_check", text)
        verdicts = {r.get("id"): r for r in data.get("results", [])}
        for f in facts:
            r = verdicts.get(f["id"], {"verdict": "unsupported", "evidence": "(판정 누락)"})
            entry["facts"][f["id"]] = r
            if r.get("verdict") == "unsupported":
                findings.append(Finding("sources", "block", f"출처에 없는 주장 [{f['id']}]", f["text"]))
            elif r.get("verdict") == "weak":
                findings.append(Finding("sources", "caution", f"출처 근거 약함 [{f['id']}]", f["text"]))
    return findings, details


# ---------------- ③ 교차 모델 검수 ----------------

def check_cross(draft: dict) -> tuple[list[Finding], dict]:
    text = prompt("cross_review", facts=facts_text(draft.get("facts", [])),
                  script=script_text(draft), blog=blog_text(draft))
    data, result = llm.generate_json("cross_review", text)
    findings = [
        Finding("cross", "caution", f"[{f.get('where', '?')}/{f.get('severity', 'minor')}] {f.get('issue', '')}"
                f" → {f.get('suggestion', '')}", f.get("quote", ""))
        for f in data.get("findings", [])
    ]
    return findings, {"model": result.model, "overall": data.get("overall", ""), "raw": data.get("findings", [])}


# ---------------- 등급 ----------------

def grade(findings: list[Finding]) -> str:
    """① 금지 항목 또는 ② 출처 실패 → 차단. ③ 지적 또는 ② 약한 출처 → 주의."""
    severities = {f.severity for f in findings}
    if "block" in severities:
        return "block"
    if "caution" in severities:
        return "caution"
    return "pass"


def is_first_of_type(draft: dict) -> bool:
    """새 콘텐츠 유형의 첫 게시물 여부 — 승인 이후 단계에 같은 유형이 없으면 첫 게시물."""
    for state in ("approved", "rendered", "ready_to_publish", "published"):
        for d in draft_dirs(state):
            if load_draft(d).get("content_type") == draft.get("content_type"):
                return False
    return True


def review(path: Path, fetch=fetch_page) -> dict:
    draft = load_draft(path)
    rule_findings, human = check_rules(draft)
    findings = list(rule_findings)
    source_findings, source_details = check_sources(draft, fetch)
    findings += source_findings
    cross_findings, cross = check_cross(draft)
    findings += cross_findings
    for issue in (draft.get("precheck") or {}).get("issues", []):  # 생성 모델 자체 점검은 참고용
        findings.append(Finding("precheck", "info", f"[1차 점검/{issue.get('severity')}] {issue.get('fix', '')}",
                                issue.get("quote", "")))
    if is_first_of_type(draft):
        human.add(f"새 유형 첫 게시물({draft.get('content_type')})")
    g = grade(findings)
    result = {
        "draft_id": draft["id"],
        "grade": g,
        "reviewed_at": now_iso(),
        "always_human": sorted(human),
        "findings": [asdict(f) for f in findings],
        "sources": source_details,
        "cross_review": cross,
        "regenerated": draft.get("regenerated", 0),
    }
    save_json(path / "review.json", result)
    return result


def summary_line(result: dict, title: str = "") -> str:
    g = result["grade"]
    top = next((f for f in result["findings"] if f["severity"] == ("block" if g == "block" else "caution")), None)
    if top is None:
        reason = " 자동점검 통과"
    else:
        reason = f" {top['message']}" + (f" “{top['quote'][:60]}”" if top["quote"] else "")
        extra = sum(1 for f in result["findings"] if f["severity"] in ("block", "caution")) - 1
        reason += f" 외 {extra}건" if extra > 0 else ""
    human = f" 👤{', '.join(result['always_human'])}" if result["always_human"] else ""
    return f"{GRADE_ICON[g]} {title or result['draft_id']}:{reason}{human}"


def regeneration_note(result: dict) -> str:
    items = [f"- {f['message']}: {f['quote'][:120]}" for f in result["findings"] if f["severity"] == "block"]
    return "Automatic review blocked this draft. Fix every item below, keep everything else:\n" + "\n".join(items[:20])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="초안 자동 검수")
    parser.add_argument("draft_ids", nargs="*")
    parser.add_argument("--all", action="store_true", help="이미 검수된 초안도 다시 검수")
    parser.add_argument("--auto-regenerate", action="store_true", help="차단 건 1회 자동 재생성 후 재검수")
    args = parser.parse_args(argv)

    if args.draft_ids:
        paths = []
        for draft_id in args.draft_ids:
            state, path = find_draft(draft_id)
            if state != "drafts":
                raise ValueError(f"{draft_id} 는 {state}/ 에 있습니다 (drafts/ 만 검수)")
            paths.append(path)
    else:
        paths = [p for p in draft_dirs("drafts") if args.all or not (p / "review.json").exists()]
    if not paths:
        log.info("검수할 초안 없음")
        return 0

    draft_mod = importlib.import_module("pipeline.02_draft")
    for path in paths:
        result = review(path)
        if args.auto_regenerate and result["grade"] == "block" and result["regenerated"] == 0:
            log.info("차단 → 자동 재생성 1회: %s", path.name)
            draft_mod.revise(path.name, regeneration_note(result), auto=True)
            result = review(path)
        print(summary_line(result, load_draft(path)["shortform"].get("title", "")))
    return 0


if __name__ == "__main__":
    run_cli("02b_auto_review", main)
