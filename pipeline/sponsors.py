"""스폰서 트랙 — 광고주(병원) 목록, 광고 표시 문구, 스폰서 전용 생성 규칙.

광고 주체는 병원, 우리는 매체 + 제작 대행 (기획서 12-1-1). 병원명·계약은 config/sponsors.yaml (git 제외)에 둔다.

    python -m pipeline.sponsors check     # 목록 검증 + 계약 상태 출력
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from pipeline.common import CONFIG_DIR, read_prompt, render

REQUIRED = ("id", "name_en", "name_ko", "official_url", "contract")
# monthly | per_post = 정액 유료. pilot = 무상 프로모션(유입 데이터 확보용) — 광고 표시는 똑같이 붙는다.
CONTRACT_TYPES = ("monthly", "per_post", "pilot")
PILOT_MAX_DAYS = 183  # 무상 파일럿은 기간을 짧게 (최대 6개월) — 사실상 무기한 무상 광고가 되지 않게


class SponsorError(ValueError):
    pass


def sponsors_file() -> Path:
    return Path(os.environ.get("SKIN_SPONSORS_FILE", CONFIG_DIR / "sponsors.yaml"))


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def validate(s: dict) -> dict:
    missing = [k for k in REQUIRED if not s.get(k)]
    if missing:
        raise SponsorError(f"스폰서 {s.get('id', '?')}: 필수 항목 없음 {missing}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", s["id"]):
        raise SponsorError(f"스폰서 id 형식 오류: {s['id']!r} (영문 소문자·숫자·하이픈)")
    if urlparse(s["official_url"]).scheme != "https":
        raise SponsorError(f"스폰서 {s['id']}: official_url 은 https 주소여야 합니다")
    contract = s["contract"]
    if contract.get("type") not in CONTRACT_TYPES:
        raise SponsorError(f"스폰서 {s['id']}: contract.type 은 monthly | per_post | pilot (성과 연동 계약 금지)")
    start, end = _as_date(contract["start"]), _as_date(contract["end"])
    if end < start:
        raise SponsorError(f"스폰서 {s['id']}: 계약 종료일이 시작일보다 빠름")
    if contract["type"] == "pilot" and (end - start).days > PILOT_MAX_DAYS:
        raise SponsorError(f"스폰서 {s['id']}: 무상 파일럿은 최대 {PILOT_MAX_DAYS}일 (이후 정액 계약으로 전환)")
    zones = {"gangnam", "central", "east", "west", "north"}
    if s.get("zone") and s["zone"] not in zones:
        raise SponsorError(f"스폰서 {s['id']}: zone 은 {sorted(zones)} 중 하나 (config/attractions.yaml 권역)")
    return {**s, "contract": {**contract, "start": start.isoformat(), "end": end.isoformat()},
            "ad_review_required": bool(s.get("ad_review_required")), "review_no": str(s.get("review_no") or "")}


def load() -> dict[str, dict]:
    path = sponsors_file()
    if not path.exists():
        return {}
    items = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("sponsors") or []
    out = {}
    for s in items:
        v = validate(s)
        if v["id"] in out:
            raise SponsorError(f"스폰서 id 중복: {v['id']}")
        out[v["id"]] = v
    return out


def get(sponsor_id: str) -> dict:
    sponsors = load()
    if sponsor_id not in sponsors:
        raise SponsorError(f"등록되지 않은 스폰서: {sponsor_id} ({sponsors_file()} 확인)")
    return sponsors[sponsor_id]


def contract_active(sponsor: dict, today: date | None = None) -> bool:
    today = today or date.today()
    return _as_date(sponsor["contract"]["start"]) <= today <= _as_date(sponsor["contract"]["end"])


def problems(sponsor: dict, today: date | None = None) -> list[str]:
    """게시하면 안 되는 사유 (02b 에서 ⛔)."""
    out = []
    if not contract_active(sponsor, today):
        c = sponsor["contract"]
        out.append(f"스폰서 계약 기간 아님 ({c['start']} ~ {c['end']})")
    if sponsor["ad_review_required"] and not sponsor["review_no"]:
        out.append("의료광고 사전심의 대상인데 심의번호 없음 (sponsors.yaml review_no)")
    return out


def is_pilot(sponsor: dict) -> bool:
    return (sponsor.get("contract") or {}).get("type") == "pilot"


def label(sponsor: dict) -> str:
    """광고 배지·표시 문구의 앞부분. 무상 파일럿은 대가가 없으므로 "Sponsored" 대신 "Partner" (광고 표시는 동일)."""
    return "Partner" if is_pilot(sponsor) else "Sponsored"


def short_disclosure(sponsor: dict) -> str:
    if is_pilot(sponsor):
        return f"Partner content with {sponsor['name_en']} · Advertisement (unpaid pilot) · AI-generated content"
    return f"Sponsored by {sponsor['name_en']} · Advertisement · AI-generated content"


def blog_disclosure(sponsor: dict) -> str:
    review = f" Ad review no. {sponsor['review_no']}." if sponsor["review_no"] else ""
    head = (f"Partner content — this is an advertisement by {sponsor['name_en']} (unpaid pilot partnership, no fee paid)."
            if is_pilot(sponsor) else f"Sponsored content — this is an advertisement by {sponsor['name_en']}.")
    return (f"> **{head}**{review} "
            "Produced with AI assistance and based on publicly available sources and information provided by the clinic. "
            "It is not medical advice; consult a licensed doctor.")


def official_link_line(sponsor: dict) -> str:
    """스폰서 글 끝에 항상 붙는 병원 공식 사이트 링크 (블로그 빌드 시 추적 링크 + rel=sponsored 로 바뀜)."""
    return f"For details, see {sponsor['name_en']}'s official website: [{sponsor['official_url']}]({sponsor['official_url']})"


def rules_text(sponsor: dict) -> str:
    fee_line = ("We produce and publish it free of charge as a short partner pilot — it is still an advertisement."
                if is_pilot(sponsor) else "We produce and publish it for a flat fee.")
    return render(read_prompt("_sponsored_rules"), name_en=sponsor["name_en"], name_ko=sponsor["name_ko"],
                  official_url=sponsor["official_url"], fee_line=fee_line,
                  short_label=short_disclosure(sponsor).split(" · ")[0])


def platform_policy() -> dict:
    """채널별 스폰서 콘텐츠 게시 가능 여부 (config/channels.yaml sponsored_policy)."""
    from pipeline.common import load_yaml
    policy = load_yaml("channels.yaml").get("sponsored_policy") or {}
    return {
        "allowed": [c for c, v in policy.items() if v.get("allowed")],
        "blocked": {c: v.get("why", "") for c, v in policy.items() if not v.get("allowed")},
        "requires": {c: v.get("requires", []) for c, v in policy.items() if v.get("allowed")},
    }


def official_host(sponsor: dict) -> str:
    return urlparse(sponsor["official_url"]).netloc.lower().removeprefix("www.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="스폰서 목록 점검")
    parser.add_argument("cmd", choices=["check"])
    parser.parse_args(argv)
    try:
        sponsors = load()
    except (SponsorError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not sponsors:
        print(f"스폰서 없음 ({sponsors_file()})")
    for s in sponsors.values():
        issues = problems(s)
        state = "게시 가능" if not issues else " / ".join(issues)
        print(f"{s['id']}\t{s['name_en']}\t{s['contract']['type']} {s['contract']['start']}~{s['contract']['end']}\t{state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
