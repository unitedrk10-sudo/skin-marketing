"""올리브영 제휴 링크 (2026-11-10, 기획 docs/olive-young-affiliate-plan.md).

화장품 글(content_type skincare)에서 본문이 인용한 올리브영 글로벌 제품 페이지([F#] 의 출처)를 코드가 찾아
"Where to buy online" 구매 링크를 붙인다. LLM 은 제품 번호·링크·가격을 쓰지 않는다.

- 링크: 글로벌몰 제품 주소 + 리워드 코드 (site.yaml affiliates.oliveyoung.reward_code). 추적기를 거치면
  미국 방문자는 Olive Young US 로 보낸다 (글로벌몰은 2026-05-29 부터 미국 배송 안 함, 글로벌 코드는 US 몰에 안 먹음).
- 가격: 미국만, 미국 방문자에게만 (2026-11-10 운영자 결정). config/oliveyoung_us.yaml 에 기준일과 함께 있는 것만.
- 광고 표시: 공정위 추천·보증 심사지침 — 글 첫 부분(제목 바로 아래)과 구매 링크 바로 위, 단정형 문구 (site.py 가 넣는다).
- 적용: 설정의 since 날짜 이후 게시 글만 (이미 게시된 화장품 글은 요청 없이 고치지 않는다). 스폰서 글엔 없음.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta

from pipeline.common import content_dir, get_logger, load_json, load_yaml, now_iso, run_cli, save_json

log = get_logger("affiliates")

PRODUCT_URL = re.compile(r"^https://global\.oliveyoung\.com/product/detail\?(?:[^#]*&)?prdtNo=([A-Za-z0-9]{6,20})(?:&|$)")
TITLE_TAIL = re.compile(r"\s*[-|]\s*OLIVE\s*YOUNG(?:\s+Global)?\s*$", re.I)
FACT_REF = re.compile(r"\[(F\d+)\]")
US_HOSTS = ("us.oliveyoung.com", "click.linksynergy.com")  # US 몰 제품 주소 또는 Rakuten 제휴 링크
LINK_KEY = "_oy"  # tracked_links.json 키: _oy:<draft_id>:<제품 번호>

# 공정위 지침: "받을 수 있음" 같은 조건부 표현은 명확한 표시가 아니다 → 단정형
DISCLOSURE_TOP = "This post contains affiliate links. We earn a commission when you buy through them."
DISCLOSURE_LINKS = "Affiliate links — we earn a commission on purchases made through them."


def settings() -> dict | None:
    """site.yaml affiliates.oliveyoung — 꺼져 있거나 리워드 코드가 없으면 None (구매 링크·표시 모두 빠진다)."""
    cfg = ((load_yaml("site.yaml").get("affiliates") or {}).get("oliveyoung")) or {}
    code = str(cfg.get("reward_code") or "").strip()
    if not cfg.get("enabled") or not re.fullmatch(r"[A-Za-z0-9]{4,20}", code):
        return None
    return {"reward_code": code, "since": str(cfg.get("since") or "2026-11-10"), "human_first": int(cfg.get("human_first", 4)),
            "us_fallback": str(cfg.get("us_fallback") or "https://us.oliveyoung.com/"),
            "discount": str(cfg.get("discount") or "")}


def applies(draft: dict, created: str | None = None) -> bool:
    """제휴 링크를 붙일 글인가: 설정이 켜져 있고, 중립 화장품 글이고, since 이후 글."""
    cfg = settings()
    when = (created or draft.get("created_at") or "")[:10]
    return bool(cfg and draft.get("content_type") == "skincare" and not draft.get("sponsor") and when >= cfg["since"])


def products(draft: dict) -> list[dict]:
    """본문이 [F#] 로 인용한 사실 중 출처가 올리브영 글로벌 제품 페이지인 것 → 제품 (인용 순서, 제품 번호로 중복 제거)."""
    facts = {f["id"]: f for f in draft.get("facts", [])}
    out, seen = [], set()
    for fid in FACT_REF.findall((draft.get("blog") or {}).get("markdown", "")):
        fact = facts.get(fid) or {}
        m = PRODUCT_URL.match(fact.get("url") or "")
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        name = TITLE_TAIL.sub("", fact.get("source_title") or "").strip() or m.group(1)
        out.append({"id": m.group(1), "name": name[:120], "fact_id": fid})
    return out


PAGE_TITLE = re.compile(r"^\s*(.{3,160}?)\s*\|\s*OLIVE YOUNG Global(?![A-Za-z])")
SITE_NAME = "OLIVE YOUNG Global"


def is_product_url(url: str) -> bool:
    return bool(PRODUCT_URL.match(url or ""))


def page_product_name(page_text: str) -> str | None:
    """받아 온 글로벌몰 제품 페이지 본문의 첫머리(= 페이지 제목 "<제품명> | OLIVE YOUNG Global")에서 제품명.
    없는 제품 번호는 몰 첫 화면 제목이 나오므로 None."""
    m = PAGE_TITLE.match(page_text or "")
    name = m.group(1).strip() if m else ""
    return name if name and not name.lower().startswith("olive young") else None


def source_title(url: str, title: str, page_text: str) -> str | None:
    """출처 제목 — 글로벌몰 제품 페이지는 검색 결과가 준 제목 대신 받아 온 페이지의 실제 제목으로 (2026-11-12:
    검색 결과 제목과 실제 페이지의 제품이 달라 구매 링크 이름이 틀릴 뻔했다). 사라진 제품 페이지는 None (출처로 쓰지 않음).
    제품 페이지가 아니면 받은 제목 그대로."""
    if not is_product_url(url):
        return title
    name = page_product_name(page_text)
    return f"{name} | {SITE_NAME}" if name else None


def same_product(a: str, b: str) -> bool:
    """두 제품명이 같은 제품인가 — 대소문자·기호·"(Set/Single)" 같은 덧붙임은 무시하고 단어가 대부분 겹치면 같다고 본다."""
    ta, tb = (set(re.findall(r"[a-z0-9]+", TITLE_TAIL.sub("", x or "").lower())) for x in (a, b))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.75


def retitled(draft: dict, names: dict[str, str] | None) -> dict:
    """제품 페이지를 출처로 한 사실의 source_title 을 확인된 제품명(구매 링크를 만들 때 몰에서 읽은 이름)으로 바꾼 사본.
    구매 링크 이름과 출처 목록이 링크가 실제로 가는 제품과 항상 맞게 — 초안 파일은 건드리지 않는다."""
    names = {k: v for k, v in (names or {}).items() if v}
    if not names:
        return draft
    facts = []
    for f in draft.get("facts", []):
        m = PRODUCT_URL.match(f.get("url") or "")
        facts.append({**f, "source_title": f"{names[m.group(1)]} | {SITE_NAME}"} if m and m.group(1) in names else f)
    return {**draft, "facts": facts}


def buyable(items: list[dict], aff_urls: dict[str, str] | None = None) -> list[dict]:
    """구매 링크에 올릴 제품 — 링크를 만들 때 올리브영에서 사라진 것으로 확인된 제품(aff_urls 값이 빈 문자열)은 뺀다."""
    return [i for i in items if (aff_urls or {}).get(i["id"], None) != ""]


def global_url(product_id: str, cfg: dict | None = None) -> str:
    cfg = cfg or settings() or {}
    query = urllib.parse.urlencode({"prdtNo": product_id, "rwardCode": cfg.get("reward_code", ""), "utm_source": "influencers"})
    return f"https://global.oliveyoung.com/product/detail?{query}"


def us_products() -> dict[str, dict]:
    return (load_yaml("oliveyoung_us.yaml") or {}).get("products") or {}


def _https_host(url: str, hosts: tuple[str, ...]) -> bool:
    p = urllib.parse.urlparse(url or "")
    host = p.netloc.lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)


def us_target(product_id: str, cfg: dict | None = None) -> str:
    """미국 방문자가 갈 곳: US 몰 제품(또는 Rakuten 링크)이 있으면 그것, 없으면 US 몰 첫 화면 (수수료 없음)."""
    cfg = cfg or settings() or {}
    url = str((us_products().get(product_id) or {}).get("url") or "")
    return url if _https_host(url, US_HOSTS) else cfg.get("us_fallback", "https://us.oliveyoung.com/")


def us_price(product_id: str) -> str | None:
    """US 가격 문구 — 가격과 기준일이 모두 있을 때만 (없으면 가격을 쓰지 않는다)."""
    entry = us_products().get(product_id) or {}
    try:
        price = float(entry.get("price"))
        as_of = date.fromisoformat(str(entry.get("as_of")))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return f"US price ${price:,.2f} at Olive Young US (as of {as_of:%b %Y})"


def link_key(draft_id: str, product_id: str) -> str:
    return f"{LINK_KEY}:{draft_id}:{product_id}"


def needs_human(draft: dict, approved_count: int) -> bool:
    """제휴 글 첫 N편(settings human_first)은 사람이 본다 — 표시 위치·문구와 구매 링크 확인 뒤 자동 승인."""
    cfg = settings()
    return bool(cfg and applies(draft) and approved_count < cfg["human_first"])


# ---------------- 구매 링크 점검 (주 1회, 주간 리포트) ----------------
# 글로벌몰 제품 화면이 쓰는 제품 데이터(POST detail-data, 제품마다 1회)로 판매 상태를 본다.
# 빈 응답 = 없는 제품(화면은 not-found 로 보냄), sellStatCode '10' = 판매 중 (화면 스크립트 기준),
# tempOutOfStockYn 'Y' 또는 옵션 전부 품절 = 일시 품절. 알리기만 하고 게시된 글의 링크는 고치지 않는다.
DETAIL_DATA = "https://global.oliveyoung.com/product/detail-data"
STATUS_LABEL = {"ok": "판매 중", "soldout": "품절", "gone": "사라짐", "error": "확인 실패"}


def check_file():
    return content_dir() / "site" / "affiliate_check.json"


def tracked() -> dict[str, dict]:
    """게시된 제휴 글의 구매 링크 (content/site/tracked_links.json 의 kind affiliate)."""
    path = content_dir() / "site" / "tracked_links.json"
    raw = load_json(path) if path.exists() else {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("kind") == "affiliate" and v.get("product")}


def tracked_products(links: dict[str, dict] | None = None) -> list[dict]:
    """제품별로 묶기 — 같은 제품이 여러 글에 있으면 글 제목을 모은다."""
    out: dict[str, dict] = {}
    for v in (tracked() if links is None else links).values():
        item = out.setdefault(v["product"], {"id": v["product"], "name": v.get("product_name") or v["product"], "posts": []})
        if v.get("title") and v["title"] not in item["posts"]:
            item["posts"].append(v["title"])
    return list(out.values())


def product_status(product_id: str) -> dict:
    """제품 하나의 판매 상태 {status, name}. 네트워크 오류는 error (다음 주에 다시 본다)."""
    from pipeline.web import USER_AGENT
    req = urllib.request.Request(DETAIL_DATA, data=json.dumps({"prdtNo": product_id}).encode(), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "error", "note": str(e)[:120]}
    if not body:
        return {"status": "gone"}
    try:
        product = (json.loads(body) or {}).get("product") or {}
    except ValueError:
        return {"status": "error", "note": "응답이 JSON 이 아님"}
    if not product:
        return {"status": "gone"}
    name = product.get("prdtNameEn") or product.get("prdtName") or ""
    options = product.get("optionList") or []
    selling = str(product.get("sellStatCode")) == "10" and product.get("tempOutOfStockYn") != "Y"
    if selling and options:
        selling = any(str(o.get("sellStatCode")) == "10" and o.get("tempOutOfStockYn") != "Y" for o in options)
    return {"status": "ok" if selling else "soldout", "name": name}


def check(fetch=product_status, pause: float = 1.0) -> dict:
    """게시된 제휴 글의 제품을 모두 확인해 저장한다 (content/site/affiliate_check.json)."""
    items = []
    for i, item in enumerate(tracked_products()):
        if i and pause:
            time.sleep(pause)
        items.append({**item, **fetch(item["id"])})
    result = {"checked_at": now_iso(), "items": items}
    save_json(check_file(), result)
    bad = [x for x in items if x["status"] != "ok"]
    log.info("구매 링크 점검: 제품 %d개, 문제 %d개", len(items), len(bad))
    return result


def check_lines(result: dict) -> list[str]:
    items = result.get("items") or []
    if not items:
        return []
    counts = Counter(x["status"] for x in items)
    lines = ["🛍 구매 링크 점검: 제품 " + str(len(items)) + "개 — " +
             " · ".join(f"{STATUS_LABEL[k]} {counts[k]}" for k in STATUS_LABEL if counts[k])]
    for x in items:
        if x["status"] in ("soldout", "gone"):
            lines.append(f"  - {STATUS_LABEL[x['status']]}: {x['name']} ({x['id']}) — 글: {', '.join(x['posts'][:2]) or '-'}")
    if counts["soldout"] or counts["gone"]:
        lines.append("  (게시된 글의 링크는 그대로 둡니다 — 바꾸려면 따로 요청)")
    return lines


def click_lines(rows: list[dict], links: dict[str, dict]) -> list[str]:
    """추적기 집계(analytics.fetch 의 행)에서 구매 링크 클릭 — 봇 제외, 미국(US 몰로 감)·그 외(글로벌몰, 수수료 대상) 나눠서."""
    by_code = {v["code"]: v for v in links.values() if v.get("code")}
    total, us, products = 0, 0, Counter()
    for r in rows:
        meta = by_code.get(r.get("code"))
        if not meta or r.get("is_bot"):
            continue
        n = int(r.get("clicks") or 0)
        total += n
        us += n if r.get("country") == "US" else 0
        products[meta.get("product_name") or meta["product"]] += n
    posts = len({v.get("draft_id") for v in links.values()})
    line = f"🛍 올리브영 구매 링크: 제휴 글 {posts}편 · 클릭 {total}"
    if total:
        line += f" (글로벌몰 {total - us} · 미국→US 몰 {us})"
    lines = [line]
    if products:
        lines.append("  상위: " + ", ".join(f"{name} {n}" for name, n in products.most_common(3)))
    lines.append("  주문·수수료는 올리브영 인플루언서 화면에서 확인")
    return lines


def report_lines(days: int = 7) -> list[str]:
    """주간 리포트용: 구매 링크 클릭 + 링크 점검 (점검은 이때 실행). 설정이 꺼져 있으면 없음."""
    if not settings():
        return []
    links = tracked()
    if not links:
        return ["🛍 올리브영 제휴: 게시된 제휴 글 아직 없음"]
    lines = []
    from pipeline import tracker
    if tracker.configured():
        from pipeline import analytics
        end = date.today() - timedelta(days=1)
        try:
            lines += click_lines(analytics.fetch(end - timedelta(days=days - 1), end), links)
        except tracker.TrackerError as e:
            log.warning("구매 링크 클릭 집계 실패: %s", e)
    lines += check_lines(check())
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="올리브영 제휴 링크")
    parser.add_argument("cmd", choices=["check", "report"])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        result = check()
        print("\n".join(check_lines(result)) or "게시된 제휴 글의 구매 링크 없음")
        return 0
    print("\n".join(report_lines(args.days)) or "제휴 설정이 꺼져 있음")
    return 0


if __name__ == "__main__":
    run_cli("affiliates", main)
