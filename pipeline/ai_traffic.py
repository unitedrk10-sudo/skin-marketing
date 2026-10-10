"""AI 유입·AI 봇 조회 — 주간 리포트용 (Cloudflare).

- AI 답변에서 클릭해 들어온 방문: Web Analytics(쿠키 없음)의 유입 도메인(refererHost)이 AI 서비스인 페이지 조회.
  토큰은 조회수(pipeline.demand)와 같다 (Account Analytics Read).
- AI 봇 조회: 영역(zone) 요청 기록의 User-Agent. 사람이 질문했을 때 실시간으로 읽어 가는 봇(ChatGPT-User,
  Perplexity-User, Claude-User)이 "답변에 인용될 후보"에 가장 가까운 지표다. 학습·색인용 봇은 따로 센다.
  토큰에 Zone Analytics Read 권한이 있어야 한다 — 없으면 이 줄만 빠진다.
답변 안에서만 인용되고 클릭이 없는 경우는 측정할 수 없다.

    python -m pipeline.ai_traffic [--days 7]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta

from pipeline.common import get_logger, load_yaml, run_cli

log = get_logger("ai_traffic")

CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
CF_API = "https://api.cloudflare.com/client/v4"
AI_REFERRERS = {  # 유입 도메인 → 서비스 이름 (tracker/src/logic.js 의 AI 목록과 같다)
    "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "perplexity.ai": "Perplexity",
    "gemini.google.com": "Gemini", "bard.google.com": "Gemini", "copilot.microsoft.com": "Copilot",
    "claude.ai": "Claude", "you.com": "You.com", "phind.com": "Phind", "meta.ai": "Meta AI",
    "chat.mistral.ai": "Mistral", "chat.deepseek.com": "DeepSeek",
}
# 사람이 질문할 때 실시간으로 페이지를 읽는 봇 (인용 후보) / 색인·학습용 봇
LIVE_BOTS = {"ChatGPT-User": "ChatGPT", "Perplexity-User": "Perplexity", "Claude-User": "Claude", "MistralAI-User": "Mistral"}
INDEX_BOTS = {"OAI-SearchBot": "ChatGPT 검색", "GPTBot": "OpenAI 학습", "PerplexityBot": "Perplexity 색인",
              "ClaudeBot": "Claude 학습", "Claude-SearchBot": "Claude 검색", "bingbot": "Bing",
              "Applebot": "Apple", "meta-externalagent": "Meta"}

REFERRER_QUERY = """query ($account: String!, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer { accounts(filter: {accountTag: $account}) {
    rumPageloadEventsAdaptiveGroups(limit: 5000, filter: $filter) { count dimensions { requestPath refererHost } }
  } }
}"""
BOT_QUERY = """query ($zone: String!, $filter: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 5000, filter: $filter) { count dimensions { userAgent clientRequestPath } }
  } }
}"""


class Unavailable(RuntimeError):
    pass


def _creds() -> tuple[str, str]:
    token = os.environ.get("CF_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CF_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not (token and account):
        raise Unavailable("Cloudflare 토큰 없음")
    return token, account


def _post(query: str, variables: dict, token: str) -> dict:
    req = urllib.request.Request(CF_GRAPHQL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 method="POST", headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unavailable(str(e)) from e
    if body.get("errors"):
        msg = str(body["errors"][0].get("message", body["errors"]))
        raise Unavailable("권한 없음 (토큰에 Zone Analytics Read 추가 필요)" if "permission" in msg else msg[:200])
    return body["data"]


def ai_service(host: str) -> str | None:
    host = (host or "").lower().removeprefix("www.")
    for domain, name in AI_REFERRERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def referrals(start: date, end: date) -> dict:
    """AI 답변에서 넘어온 페이지 조회: {"total", "by_service": Counter, "by_path": Counter, "all_views"}."""
    token, account = _creds()
    site_tag = load_yaml("site.yaml").get("analytics_site_tag")
    if not site_tag:
        raise Unavailable("site.yaml analytics_site_tag 없음")
    data = _post(REFERRER_QUERY, {"account": account, "filter": {"AND": [
        {"datetime_geq": f"{start}T00:00:00Z", "datetime_leq": f"{end}T23:59:59Z"}, {"siteTag": site_tag}, {"bot": 0}]}}, token)
    by_service, by_path, total = Counter(), Counter(), 0
    for g in data["viewer"]["accounts"][0]["rumPageloadEventsAdaptiveGroups"]:
        total += int(g["count"])
        name = ai_service(g["dimensions"]["refererHost"])
        if name:
            by_service[name] += int(g["count"])
            by_path[g["dimensions"]["requestPath"]] += int(g["count"])
    return {"total": sum(by_service.values()), "by_service": by_service, "by_path": by_path, "all_views": total}


def _zone_id(token: str) -> str:
    domain = load_yaml("site.yaml").get("domain")
    req = urllib.request.Request(f"{CF_API}/zones?name={domain}", headers={"authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())["result"][0]["id"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        raise Unavailable(f"zone 조회 실패: {e}") from e


def bot_name(user_agent: str) -> tuple[str, str] | None:
    """(종류 live|index, 이름)."""
    for table, kind in ((LIVE_BOTS, "live"), (INDEX_BOTS, "index")):
        for token, name in table.items():
            if re.search(re.escape(token), user_agent or "", re.I):
                return kind, name
    return None


def crawls(start: date, end: date) -> dict:
    """AI·검색 봇 요청: {"live": Counter(이름), "index": Counter(이름), "live_paths": Counter}. 글 페이지만 센다."""
    token, _ = _creds()
    zone = _zone_id(token)
    names = [*LIVE_BOTS, *INDEX_BOTS]
    data = _post(BOT_QUERY, {"zone": zone, "filter": {"AND": [
        {"datetime_geq": f"{start}T00:00:00Z", "datetime_leq": f"{end}T23:59:59Z"},
        {"OR": [{"userAgent_like": f"%{n}%"} for n in names]}]}}, token)
    out = {"live": Counter(), "index": Counter(), "live_paths": Counter()}
    for g in data["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]:
        found = bot_name(g["dimensions"]["userAgent"])
        path = g["dimensions"]["clientRequestPath"] or "/"
        if not found or path.endswith((".png", ".jpg", ".ico", ".css", ".js", ".woff2", ".webmanifest")):
            continue
        kind, name = found
        out[kind][name] += int(g["count"])
        if kind == "live":
            out["live_paths"][path] += int(g["count"])
    return out


def _top(counter: Counter, n: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common(n))


def report_lines(days: int = 7, today: date | None = None) -> list[str]:
    """주간 리포트용 줄. 측정 못 한 항목은 이유를 짧게."""
    end = (today or date.today()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    lines = []
    try:
        r = referrals(start, end)
        share = f" (전체 조회 {r['all_views']} 중 {round(r['total'] * 100 / r['all_views'])}%)" if r["all_views"] else ""
        lines.append(f"🤖 AI 답변에서 온 방문 {r['total']}{share}" + (f" — {_top(r['by_service'])}" if r["total"] else ""))
        if r["by_path"]:
            lines.append("   많이 들어온 글: " + _top(r["by_path"]))
    except Unavailable as e:
        lines.append(f"🤖 AI 유입: 확인 못 함 ({e})")
    try:
        c = crawls(start, end)
        live = sum(c["live"].values())
        lines.append(f"🔎 AI 실시간 조회(답변 작성 중 읽어 감) {live}" + (f" — {_top(c['live'])}" if live else "")
                     + (f" · 색인·학습 봇 {sum(c['index'].values())} ({_top(c['index'])})" if c["index"] else ""))
        if c["live_paths"]:
            lines.append("   AI 가 읽어 간 글: " + _top(c["live_paths"]))
    except Unavailable as e:
        lines.append(f"🔎 AI 봇 조회: 확인 못 함 ({e})")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 유입·AI 봇 조회")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    print("\n".join(report_lines(args.days)))
    return 0


if __name__ == "__main__":
    run_cli("ai_traffic", main)
