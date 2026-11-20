"""AI 인용 점검 — 독자가 AI 에 물을 법한 질문에 우리 사이트가 출처로 잡히는지 (2026-10-19, agency-agents AI Citation Strategist 참고).

Gemini 검색 연동(stage citation_check)으로 질문마다 답을 받아, 답의 출처 주소(grounding)와 본문에 skinboundkorea.com 이
있는지 센다. 질문 목록은 config/citation_prompts.yaml (같은 질문을 계속 써야 추이를 비교할 수 있다).
4주마다 주간 리포트(07_report.trust_check 와 같은 주)에 들어가고, 결과는 content/reports/citations/<날짜>.json.
ChatGPT·Perplexity·Claude 는 자동화하지 않는다 — 같은 질문으로 사람이 가끔 확인.

    python -m pipeline.citation_check [--limit 5]
"""

from __future__ import annotations

import argparse
import urllib.parse
from datetime import date

from pipeline import llm
from pipeline.common import content_dir, get_logger, load_json, load_yaml, run_cli, save_json

log = get_logger("citation_check")


def domain() -> str:
    return (load_yaml("site.yaml").get("domain") or "").lower()


def questions() -> list[str]:
    return [str(q) for q in (load_yaml("citation_prompts.yaml").get("questions") or [])]


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


ASK = "Search the web first, then answer from what you find and cite your sources.\n\nQuestion: {q}"
TRIES = 2


def _ask(q: str):
    """검색 연동은 모델이 쓸지 정한다 — 출처 없이 답하면 한 번 더 (2026-11-02: 20개 중 16개가 출처 없이 왔다)."""
    res = llm.generate("citation_check", ASK.format(q=q))
    for _ in range(TRIES - 1):
        if res.grounding_urls:
            break
        res = llm.generate("citation_check", ASK.format(q=q))
    return res


def check(limit: int | None = None) -> dict:
    """{date, total, measured, cited, results: [{q, measured, cited, sources}]} — 저장까지.
    출처가 없으면 인용 여부를 알 수 없으므로 '측정 실패' 로 따로 센다."""
    site = domain()
    results = []
    for q in questions()[:limit]:
        try:
            res = _ask(q)
        except llm.LLMError as e:
            log.warning("인용 점검 실패: %s — %s", q, e)
            continue
        hosts = [(u.get("title") or "").lower().removeprefix("www.") if "vertexaisearch" in u["url"] else _host(u["url"])
                 for u in res.grounding_urls]  # 검색 연동 리디렉션 주소는 title 에 실제 도메인이 온다
        hosts = [h for h in hosts if h]
        cited = bool(site) and (any(site in h for h in hosts) or site in res.text.lower())
        results.append({"q": q, "measured": bool(hosts) or cited, "cited": cited, "sources": list(dict.fromkeys(hosts))[:8]})
    out = {"date": date.today().isoformat(), "total": len(results), "measured": sum(r["measured"] for r in results),
           "cited": sum(r["cited"] for r in results), "results": results}
    save_json(content_dir() / "reports" / "citations" / f"{out['date']}.json", out)
    return out


def previous() -> dict | None:
    folder = content_dir() / "reports" / "citations"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    # 측정 실패를 구분하기 전(2026-11-02 이전) 결과는 비교하지 않는다
    return next((d for d in map(load_json, reversed(files[:-1])) if "measured" in d), None)


def report_lines(result: dict) -> list[str]:
    if not result["total"]:
        return ["🔎 AI 인용 점검: 질문을 돌리지 못함"]
    prev = previous()
    trend = f" (지난번 {prev['cited']}/{prev['measured']})" if prev else ""
    failed = result["total"] - result["measured"]
    lines = [f"🔎 AI 인용 점검 (Gemini 검색, 질문 {result['total']}개): 우리 사이트 인용 {result['cited']}/{result['measured']}{trend}"
             + (f" · 측정 실패 {failed}" if failed else "")]
    top = {}
    for r in result["results"]:
        for h in r["sources"]:
            top[h] = top.get(h, 0) + 1
    common = sorted(top.items(), key=lambda x: -x[1])[:4]
    if common:
        lines.append("   자주 인용된 출처: " + ", ".join(f"{h} {n}" for h, n in common))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 인용 점검")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    res = check(args.limit)
    print("\n".join(report_lines(res)))
    for r in res["results"]:
        print(f"{'✅' if r['cited'] else '·' if r['measured'] else '?'} {r['q']}  ← {', '.join(r['sources'][:4])}")
    return 0


if __name__ == "__main__":
    run_cli("citation_check", main)
