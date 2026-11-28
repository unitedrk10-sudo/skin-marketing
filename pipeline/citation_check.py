"""AI 인용 점검 — 독자가 AI 에 물을 법한 질문에 우리 사이트가 출처로 잡히는지 (2026-10-19, agency-agents AI Citation Strategist 참고).

같은 질문을 세 엔진으로 묻는다 (2026-11-28): Gemini 검색 연동(stage citation_check — 추이 비교의 기준),
ChatGPT(codex exec --search, ChatGPT 로그인), Claude(claude -p + WebSearch, Pro 로그인). 답의 출처 주소와 본문에
skinboundkorea.com 이 있는지 센다. 모두 기억 없는 새 질문이라 운영자 계정의 개인화가 섞이지 않는다.
질문 목록은 config/citation_prompts.yaml (같은 질문을 계속 써야 추이를 비교할 수 있다).
질문은 세 묶음 (2026-11-20): 일반(questions — 추이 비교는 이것만), 이름(brand — AI 가 우리를 아는가),
우리만 답할 질문(original — 공개 데이터·시술 × 한국 여행 조합. 일반 질문은 원출처가 직접 인용된다).
4주마다 주간 리포트(07_report.trust_check 와 같은 주)에 들어가고, 결과는 content/reports/citations/<날짜>.json.
엔진 하나가 안 되면(설치·로그인·한도) 그 엔진만 빼고 리포트에 이유를 적는다.

    python -m pipeline.citation_check [--limit 5] [--engine ChatGPT]
"""

from __future__ import annotations

import argparse
import urllib.parse
from datetime import date

from pipeline import llm
from pipeline.common import content_dir, get_logger, load_json, load_yaml, run_cli, save_json

log = get_logger("citation_check")

# (이름, stage) — 첫 엔진이 기준 (맨 위 숫자·지난번 비교는 예전처럼 Gemini)
ENGINES = [("Gemini", "citation_check"), ("ChatGPT", "citation_check_chatgpt"), ("Claude", "citation_check_claude")]


def domain() -> str:
    return (load_yaml("site.yaml").get("domain") or "").lower()


GROUPS = {"general": "questions", "brand": "brand", "original": "original"}
LABELS = {"brand": "이름 질문", "original": "우리만 답할 질문"}


def questions() -> list[tuple[str, str]]:
    cfg = load_yaml("citation_prompts.yaml")
    return [(g, str(q)) for g, key in GROUPS.items() for q in (cfg.get(key) or [])]


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


ASK = "Search the web first, then answer from what you find and cite your sources.\n\nQuestion: {q}"
# CLI 엔진은 출처 목록을 따로 주지 않는다 — 본문 끝에 주소를 적게 한다 (질문은 맨 끝 그대로)
ASK_URLS = ("Search the web first, then answer from what you find. After the answer, list every source URL you used, "
            "one per line.\n\nQuestion: {q}")
TRIES = 2


def _ask(q: str, stage: str = "citation_check"):
    """검색 연동은 모델이 쓸지 정한다 — 출처 없이 답하면 한 번 더 (2026-11-02: 20개 중 16개가 출처 없이 왔다)."""
    ask = ASK if stage == "citation_check" else ASK_URLS

    def one():
        res = llm.generate(stage, ask.format(q=q))
        if not res.grounding_urls and stage != "citation_check":  # CLI 엔진은 본문 끝의 주소가 출처
            res.grounding_urls = llm.text_urls(res.text)
        return res
    res = one()
    for _ in range(TRIES - 1):
        if res.grounding_urls:
            break
        res = one()
    return res


def _summary(rs: list[dict]) -> dict:
    return {"total": len(rs), "measured": sum(r["measured"] for r in rs), "cited": sum(r["cited"] for r in rs)}


def check(limit: int | None = None, engines: list[str] | None = None) -> dict:
    """{date, total, measured, cited, groups, results: [{q, engine, measured, cited, sources}], engines, skipped} — 저장까지.
    맨 위 숫자·groups 는 기준 엔진(Gemini)만. 출처가 없으면 인용 여부를 알 수 없으므로 '측정 실패' 로 따로 센다."""
    site = domain()
    results, skipped = [], {}
    for name, stage in ENGINES:
        if engines and name not in engines:
            continue
        for i, item in enumerate(questions()[:limit]):
            group, q = item if isinstance(item, tuple) else ("general", item)
            try:
                res = _ask(q, stage)
            except llm.LLMError as e:
                log.warning("인용 점검 실패 (%s): %s — %s", name, q, e)
                if i == 0:  # 첫 질문부터 안 되면 설치·로그인·한도 문제 — 이 엔진은 건너뛴다
                    skipped[name] = str(e)[:120]
                    break
                continue
            hosts = [(u.get("title") or "").lower().removeprefix("www.") if "vertexaisearch" in u["url"] else _host(u["url"])
                     for u in res.grounding_urls]  # 검색 연동 리디렉션 주소는 title 에 실제 도메인이 온다
            hosts = [h for h in hosts if h]
            cited = bool(site) and (any(site in h for h in hosts) or site in res.text.lower())
            results.append({"q": q, "group": group, "engine": name, "measured": bool(hosts) or cited, "cited": cited,
                            "sources": list(dict.fromkeys(hosts))[:8]})
    base = [r for r in results if r["engine"] == ENGINES[0][0]]
    general = [r for r in base if r["group"] == "general"]  # 맨 위 숫자는 예전과 같은 일반 질문만 (추이 비교)
    out = {"date": date.today().isoformat(), **_summary(general), "results": results,
           "groups": {g: _summary(rs) for g in GROUPS if (rs := [r for r in base if r["group"] == g])},
           "engines": {name: {**_summary([r for r in rs if r["group"] == "general"]),
                              "groups": {g: _summary(gs) for g in LABELS if (gs := [r for r in rs if r["group"] == g])}}
                       for name, _ in ENGINES if (rs := [r for r in results if r["engine"] == name])},
           "skipped": skipped}
    save_json(content_dir() / "reports" / "citations" / f"{out['date']}.json", out)
    return out


def previous() -> dict | None:
    folder = content_dir() / "reports" / "citations"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    # 측정 실패를 구분하기 전(2026-11-02 이전) 결과는 비교하지 않는다
    return next((d for d in map(load_json, reversed(files[:-1])) if "measured" in d and d.get("total")), None)


def report_lines(result: dict) -> list[str]:
    if not result["results"]:
        return ["🔎 AI 인용 점검: 질문을 돌리지 못함"] + [f"   {n}: 측정 못함 ({why})" for n, why in (result.get("skipped") or {}).items()]
    prev = previous()
    lines = []
    if result["total"]:
        trend = f" (지난번 {prev['cited']}/{prev['measured']})" if prev else ""
        failed = result["total"] - result["measured"]
        lines.append(f"🔎 AI 인용 점검 (Gemini 검색, 질문 {result['total']}개): 우리 사이트 인용 {result['cited']}/{result['measured']}{trend}"
                     + (f" · 측정 실패 {failed}" if failed else ""))
        extra = [f"{LABELS[g]} {c['cited']}/{c['measured']}" for g, c in (result.get("groups") or {}).items() if g in LABELS]
        if extra:
            lines.append("   " + " · ".join(extra))
    else:
        lines.append("🔎 AI 인용 점검")
    for name, e in (result.get("engines") or {}).items():
        if name == ENGINES[0][0]:
            continue
        p = ((prev or {}).get("engines") or {}).get(name)
        extra = [f"{LABELS[g]} {c['cited']}/{c['measured']}" for g, c in e.get("groups", {}).items()]
        failed = e["total"] - e["measured"]
        lines.append(f"   {name} 검색: 인용 {e['cited']}/{e['measured']}" + (f" (지난번 {p['cited']}/{p['measured']})" if p else "")
                     + (f" · 측정 실패 {failed}" if failed else "") + "".join(f" · {x}" for x in extra))
    lines += [f"   {n}: 측정 못함 ({why})" for n, why in (result.get("skipped") or {}).items()]
    top = {}
    for r in result["results"]:
        if r.get("group", "general") != "general":
            continue
        for h in r["sources"]:
            top[h] = top.get(h, 0) + 1
    common = sorted(top.items(), key=lambda x: -x[1])[:4]
    if common:
        lines.append("   자주 인용된 출처: " + ", ".join(f"{h} {n}" for h, n in common))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 인용 점검")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--engine", action="append", choices=[n for n, _ in ENGINES], help="이 엔진만 (여러 번 가능)")
    args = parser.parse_args(argv)
    res = check(args.limit, args.engine)
    print("\n".join(report_lines(res)))
    for r in res["results"]:
        tag = f"[{LABELS[r['group']]}] " if r.get("group") in LABELS else ""
        print(f"{'✅' if r['cited'] else '·' if r['measured'] else '?'} {r['engine']} {tag}{r['q']}  ← {', '.join(r['sources'][:4])}")
    return 0


if __name__ == "__main__":
    run_cli("citation_check", main)
