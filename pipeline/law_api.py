"""국가법령정보 공동활용 Open API 클라이언트.

- 신청: https://open.law.go.kr (OPEN API 활용 신청 → 승인 후 사용)
- 인증: 신청 시 등록한 이메일의 ID 부분(OC)을 환경변수 LAW_API_OC 로 전달
- 엔드포인트
    목록 조회: https://www.law.go.kr/DRF/lawSearch.do
    본문 조회: https://www.law.go.kr/DRF/lawService.do

CLI:
    python -m pipeline.law_api search 의료법
    python -m pipeline.law_api article 의료법 27
    python -m pipeline.law_api article 의료법 27의2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE_URL = "https://www.law.go.kr/DRF"
DEFAULT_TIMEOUT = 20
RETRIES = 4


class LawApiError(RuntimeError):
    pass


@dataclass
class LawSummary:
    name: str
    law_id: str
    mst: str  # 법령일련번호 (본문 조회 키)
    effective_date: str  # 시행일자 YYYYMMDD
    promulgation_date: str  # 공포일자 YYYYMMDD
    kind: str = ""  # 법령구분명 (법률/대통령령/부령 ...)


@dataclass
class Article:
    number: str  # "27", "27의2"
    title: str
    text: str  # 조문 전체 (항·호·목 포함) 평문


@dataclass
class LawDocument:
    name: str
    law_id: str
    effective_date: str
    promulgation_date: str
    articles: list[Article] = field(default_factory=list)


def _as_list(value) -> list:
    """API는 결과가 1건이면 dict, 여러 건이면 list 를 돌려준다."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value) -> str:
    """조문 내용 필드는 문자열이거나 문자열 리스트(2차원 포함)로 온다."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t)
    return str(value).strip()


def jo_code(article: str) -> str:
    """'27' → '002700', '27의2' → '002702' (JO 파라미터 형식: 조 4자리 + 가지 2자리)."""
    m = re.fullmatch(r"\s*(?:제)?(\d+)\s*(?:조)?\s*(?:의\s*(\d+))?\s*", article)
    if not m:
        raise ValueError(f"조문 번호 형식이 아님: {article!r}")
    return f"{int(m.group(1)):04d}{int(m.group(2) or 0):02d}"


def parse_search(payload: dict) -> list[LawSummary]:
    root = payload.get("LawSearch") or {}
    return [
        LawSummary(
            name=item.get("법령명한글", ""),
            law_id=item.get("법령ID", ""),
            mst=item.get("법령일련번호", ""),
            effective_date=item.get("시행일자", ""),
            promulgation_date=item.get("공포일자", ""),
            kind=item.get("법령구분명", ""),
        )
        for item in _as_list(root.get("law"))
    ]


def _render_article(unit: dict) -> str:
    lines = [_text(unit.get("조문내용"))]
    for hang in _as_list(unit.get("항")):
        lines.append(_text(hang.get("항내용")))
        for ho in _as_list(hang.get("호")):
            lines.append("  " + _text(ho.get("호내용")))
            for mok in _as_list(ho.get("목")):
                lines.append("    " + _text(mok.get("목내용")))
    return "\n".join(line for line in lines if line.strip())


def parse_document(payload: dict) -> LawDocument:
    root = payload.get("법령")
    if not root:
        raise LawApiError(f"본문 응답 형식이 예상과 다름: keys={list(payload)}")
    info = root.get("기본정보") or {}
    articles = []
    for unit in _as_list((root.get("조문") or {}).get("조문단위")):
        if unit.get("조문여부") == "전문":  # 장·절 제목 행
            continue
        number = str(unit.get("조문번호", ""))
        branch = str(unit.get("조문가지번호") or "").strip()
        if branch and branch != "0":
            number = f"{number}의{branch}"
        articles.append(Article(number=number, title=_text(unit.get("조문제목")), text=_render_article(unit)))
    return LawDocument(
        name=_text(info.get("법령명_한글")),
        law_id=_text(info.get("법령ID")),
        effective_date=_text(info.get("시행일자")),
        promulgation_date=_text(info.get("공포일자")),
        articles=articles,
    )


class LawApiClient:
    def __init__(self, oc: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.oc = oc or os.environ.get("LAW_API_OC", "")
        if not self.oc:
            raise LawApiError("LAW_API_OC 환경변수가 없습니다 (open.law.go.kr 신청 ID).")
        self.timeout = timeout

    def _get(self, endpoint: str, **params) -> dict:
        query = urllib.parse.urlencode({"OC": self.oc, "type": "JSON", **params})
        url = f"{BASE_URL}/{endpoint}?{query}"
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                break
            except (urllib.error.URLError, OSError) as e:  # 연결 재설정·타임아웃 포함
                last_err = e
                time.sleep(2**attempt)
        else:
            raise LawApiError(f"{endpoint} 요청 실패: {last_err}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            # OC 미승인·오류 시 JSON 대신 HTML 안내 페이지가 온다
            raise LawApiError(f"JSON 이 아닌 응답 (OC 승인 여부 확인): {body[:200]!r}") from e

    def search(self, query: str, display: int = 20) -> list[LawSummary]:
        return parse_search(self._get("lawSearch.do", target="law", query=query, display=display))

    def find(self, name: str) -> LawSummary:
        """법령명이 정확히 일치하는 현행 법령 1건."""
        for law in self.search(name):
            if law.name == name:
                return law
        raise LawApiError(f"법령을 찾지 못함: {name}")

    def document(self, mst: str, article: str | None = None) -> LawDocument:
        params = {"target": "law", "MST": mst}
        if article:
            params["JO"] = jo_code(article)
        return parse_document(self._get("lawService.do", **params))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="국가법령정보 Open API 조회")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    a = sub.add_parser("article")
    a.add_argument("law_name")
    a.add_argument("article", help="예: 27, 27의2")
    args = parser.parse_args(argv)

    try:
        client = LawApiClient()
        if args.cmd == "search":
            for law in client.search(args.query):
                print(f"{law.name}\t{law.kind}\t시행 {law.effective_date}\tMST={law.mst}")
        else:
            law = client.find(args.law_name)
            doc = client.document(law.mst, args.article)
            print(f"# {doc.name} (시행 {doc.effective_date})")
            for art in doc.articles:
                print(f"\n## 제{art.number}조 {art.title}\n{art.text}")
    except (LawApiError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
