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

import re
import urllib.parse
from datetime import date

from pipeline.common import load_yaml

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
