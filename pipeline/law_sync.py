"""config/laws.yaml 의 법령을 조회해 스냅샷을 저장하고, 이전 스냅샷과 비교해 변경을 알린다.

Hermes 크론(no-agent 모드)으로 주 1회 실행하는 것을 전제로 한다.
- 결과: legal/snapshots/<법령명>.json, legal/digest.md, legal/changes.md(변경 있을 때만)
- 종료 코드: 0 정상(변경 유무와 무관), 1 조회 실패

    python -m pipeline.law_sync
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import yaml

from pipeline.law_api import Article, LawApiClient, LawApiError, LawDocument

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "laws.yaml"
LEGAL_DIR = ROOT / "legal"
SNAPSHOT_DIR = LEGAL_DIR / "snapshots"
REQUEST_INTERVAL = 2  # 초
RETRY_PAUSE = 60  # 초


def select_articles(doc: LawDocument, articles: list[str] | None, keywords: list[str] | None) -> list[Article]:
    wanted = set(articles or [])
    kws = keywords or []
    return [
        a
        for a in doc.articles
        if a.number in wanted or any(k in a.title or k in a.text for k in kws)
    ]


def snapshot(entry: dict, doc: LawDocument, selected: list[Article]) -> dict:
    return {
        "name": doc.name,
        "law_id": doc.law_id,
        "effective_date": doc.effective_date,
        "promulgation_date": doc.promulgation_date,
        "stage": entry.get("stage", []),
        "why": entry.get("why", ""),
        "articles": {
            a.number: {**asdict(a), "hash": hashlib.sha256(a.text.encode()).hexdigest()[:16]} for a in selected
        },
    }


def diff(old: dict | None, new: dict) -> list[str]:
    if old is None:
        return [f"신규 추적: {new['name']} (시행 {new['effective_date']})"]
    changes = []
    if old.get("effective_date") != new["effective_date"]:
        changes.append(f"시행일 변경: {old.get('effective_date')} → {new['effective_date']}")
    old_arts, new_arts = old.get("articles", {}), new["articles"]
    for num in sorted(set(old_arts) | set(new_arts)):
        if num not in new_arts:
            changes.append(f"제{num}조 삭제 또는 추적 범위 이탈")
        elif num not in old_arts:
            changes.append(f"제{num}조 추가: {new_arts[num]['title']}")
        elif old_arts[num]["hash"] != new_arts[num]["hash"]:
            changes.append(f"제{num}조 내용 변경: {new_arts[num]['title']}")
    return [f"{new['name']} — {c}" for c in changes]


def render_digest(snaps: list[dict]) -> str:
    out = [f"# 추적 법령 요약 (조회일 {date.today().isoformat()})", ""]
    out.append("> 국가법령정보 Open API 자동 수집본. 법률 자문이 아니며, 해석은 전문가 확인 필요.\n")
    for s in snaps:
        stage = ", ".join(str(x) for x in s["stage"])
        out.append(f"## {s['name']}\n\n- 시행일 {s['effective_date']} / 공포일 {s['promulgation_date']} / 단계: {stage}")
        out.append(f"- 추적 이유: {s['why']}\n")
        for num, a in s["articles"].items():
            out.append(f"### 제{num}조 {a['title']}\n\n```\n{a['text']}\n```\n")
    return "\n".join(out)


def _snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name.replace('/', '_').replace(' ', '_')}.json"


def _fetch_all(client: LawApiClient, entries: list[dict]) -> tuple[dict[str, LawDocument], dict[str, str]]:
    docs, failed = {}, {}
    for i, entry in enumerate(entries):
        if i:
            time.sleep(REQUEST_INTERVAL)  # 과도한 호출 제한 회피
        try:
            summary = client.find(entry["name"])
            docs[entry["name"]] = client.document(summary.mst)
        except LawApiError as e:
            failed[entry["name"]] = str(e)
    return docs, failed


def run(client: LawApiClient, config: dict) -> tuple[list[dict], list[str], list[str]]:
    snaps, changes, errors = [], [], []
    docs, failed = _fetch_all(client, config["laws"])
    if failed:  # 연속 호출 시 서버가 연결을 끊는 경우가 있어, 쉬었다가 실패분만 한 번 더
        time.sleep(RETRY_PAUSE)
        retried, failed = _fetch_all(client, [e for e in config["laws"] if e["name"] in failed])
        docs.update(retried)
    for entry in config["laws"]:
        doc = docs.get(entry["name"])
        if doc is None:
            errors.append(f"{entry['name']}: {failed[entry['name']]}")
            path = _snapshot_path(entry["name"])
            if path.exists():  # 조회 실패 시 요약본에는 직전 스냅샷을 유지
                snaps.append(json.loads(path.read_text(encoding="utf-8")))
            continue
        selected = select_articles(doc, entry.get("articles"), entry.get("keywords"))
        new = snapshot(entry, doc, selected)
        path = _snapshot_path(entry["name"])
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        changes += diff(old, new)
        path.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        snaps.append(new)
    return snaps, changes, errors


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    try:
        client = LawApiClient()
    except LawApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    snaps, changes, errors = run(client, config)
    if snaps:
        (LEGAL_DIR / "digest.md").write_text(render_digest(snaps), encoding="utf-8")
    if changes:
        body = "\n".join(f"- {c}" for c in changes)
        (LEGAL_DIR / "changes.md").write_text(f"# 법령 변경 감지 ({date.today().isoformat()})\n\n{body}\n", encoding="utf-8")
        print(body)
    else:
        (LEGAL_DIR / "changes.md").unlink(missing_ok=True)  # 이전 실행의 알림이 남지 않도록
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
