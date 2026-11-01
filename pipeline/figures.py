"""블로그 글 안의 설명 그림 (2026-10-22) — 숏폼 도식과 같은 그림을 정지 이미지로 블로그 섹션 아래에 넣는다.

아직 게시되지 않은 승인 글만 (이미 올라간 글은 고치지 않는다 — 사람 결정 2026-10-21). 워커가 게시 예정 글마다 한 번 만든다.
Claude(stage figure_plan)가 승인된 본문에서 그림이 필요한 섹션(작동 원리 우선)과 도식 종류·글자를 고르고 →
코드가 사실 목록 기준으로 검사(explainer.check — 걸리면 그 그림은 뺀다) → Remotion still(Figure) 로 PNG →
<글 폴더>/figures/ + figures.json → 사람 승인(2026-11-01) → site.py 가 해당 섹션 첫 문단 뒤에 넣는다.
승인 전이거나 승인 뒤 본문·그림이 바뀐 글은 그림 없이 게시된다. 초안 검수 때 첨부된 그림은 초안 승인으로 같이 승인된다.

    python -m pipeline.figures make <draft_id>      # 한 글
    python -m pipeline.figures upcoming             # 게시 예정 글 중 아직 없는 것 1개
    python -m pipeline.figures list                 # 승인 대기 그림 (첨부 포함)
    python -m pipeline.figures apply "1,3 승인"     # 또는 "2 빼기" / "전체 승인"
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import llm
from pipeline.common import content_dir, facts_text, get_logger, load_draft, load_json, now_iso, prompt, run_cli, save_json

log = get_logger("figures")

KST = timezone(timedelta(hours=9))
FIG_KINDS = {"skin_layers", "molecule", "timeline", "fade_patch"}
MAX_FIGS = 2
STILL_FRAME = 108  # 4초(120프레임) 중 끝쪽 — 입자·섬유 효과가 보이는 순간


class FigureError(RuntimeError):
    pass


def headings(md: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^##\s+(.+)$", md, re.M) if m.strip().lower() not in ("sources", "faq")]


def plan(path: Path) -> list[dict]:
    draft = load_draft(path)
    shorts = importlib.import_module("pipeline.shorts")
    heads = headings(draft["blog"].get("markdown", ""))
    data, _ = llm.generate_json("figure_plan", prompt("figure_plan", guide=shorts.guide_text([draft]),
                                                      headings="\n".join(f"- {h}" for h in heads),
                                                      facts=facts_text(shorts._facts(draft))))
    figs = [f for f in (data or {}).get("figures") or [] if isinstance(f, dict)][:MAX_FIGS]
    explainer = importlib.import_module("pipeline.explainer")
    out = []
    for f in figs:
        if f.get("kind") not in FIG_KINDS or f.get("after_heading") not in heads:
            log.warning("그림 계획 버림 (종류·섹션 불일치): %s", f.get("after_heading"))
            continue
        checked, problems = explainer.check(draft, [{**f, "line": 0}])
        if problems or checked[0].get("kind") != f["kind"]:
            log.warning("그림 계획 버림 (사실 검사): %s", problems)
            continue
        out.append({**f, "source": explainer.sources(draft, f)})
    return out


def render(beat: dict, out: Path) -> Path:
    explainer = importlib.import_module("pipeline.explainer")
    exe = explainer.npx()
    props = out.with_suffix(".props.json")
    props.write_text(json.dumps({"beat": beat}, ensure_ascii=False), encoding="utf-8")
    cmd = [exe, "remotion", "still", "src/index.ts", "Figure", str(out), f"--props={props}", f"--frame={STILL_FRAME}", "--log=error"]
    if chrome := explainer.browser():
        cmd.append(f"--browser-executable={chrome}")
    proc = subprocess.run(cmd, cwd=explainer.VIDEO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, check=False)
    props.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists():
        raise FigureError(f"그림 렌더 실패: {(proc.stderr or proc.stdout)[-400:]}")
    return out


def make(path: Path) -> list[dict]:
    figs = plan(path)
    folder = path / "figures"
    folder.mkdir(exist_ok=True)
    saved = []
    for n, f in enumerate(figs, 1):
        png = render({k: v for k, v in f.items() if k not in ("after_heading", "alt", "fact_ids")}, folder / f"fig{n}.png")
        saved.append({"file": png.name, "after_heading": f["after_heading"], "alt": f.get("alt", ""),
                      "caption": " — ".join(x for x in (f.get("label"), f.get("note")) if x), "source": f.get("source", ""),
                      "fact_ids": f.get("fact_ids") or []})
    save_json(path / "figures.json", {"made_at": now_iso(), "figures": saved, "blog_hash": blog_hash(path)})
    return saved


def blog_hash(path: Path) -> str:
    import hashlib
    return hashlib.sha1(load_draft(path)["blog"].get("markdown", "").encode()).hexdigest()[:12]


def stale(path: Path) -> bool:
    """그림이 없거나, 본문이 바뀌어(수정 요청 등) 다시 만들어야 하나."""
    f = path / "figures.json"
    return not f.exists() or load_json(f).get("blog_hash") not in (None, blog_hash(path))


def wants_figures(draft: dict) -> bool:
    """시술 정보 글만 (관광 글·스폰서 글은 그림 없이)."""
    return not draft.get("sponsor") and (draft.get("content_type") or (draft.get("topic") or {}).get("axis")) in ("procedure", "skincare")


def in_review() -> list[Path]:
    """검수 대기 초안 중 그림이 필요한 것 (2026-10-26: 다음 시술 글부터 검수 메시지에 그림을 같이 보낸다)."""
    from pipeline.common import draft_dirs
    return [p for p in draft_dirs("drafts") if (p / "draft.json").exists() and wants_figures(load_draft(p)) and stale(p)]


def ensure_drafts(limit: int = 2) -> list[str]:
    done = []
    for path in in_review()[:limit]:
        try:
            figs = make(path)
            done.append(f"🖼 검수용 그림 {len(figs)}개: {path.name}")
        except llm.ClaudeAuthExpired:
            raise
        except (FigureError, llm.LLMError) as e:
            save_json(path / "figures.json", {"made_at": now_iso(), "figures": [], "error": str(e)[:300], "blog_hash": blog_hash(path)})
            done.append(f"⚠️ 검수용 그림 실패 ({path.name}): {e}")
    return done


def attachments(path: Path) -> list[Path]:
    f = path / "figures.json"
    if not f.exists():
        return []
    return [path / "figures" / x["file"] for x in load_json(f).get("figures") or [] if (path / "figures" / x["file"]).exists()]


def upcoming(now: datetime | None = None) -> list[Path]:
    """게시 예정(아직 안 올라간) 승인 글 중 그림을 아직 안 만든 것."""
    now = now or datetime.now(KST)
    sched_file = content_dir() / "site" / "schedule.json"
    sched = load_json(sched_file) if sched_file.exists() else {}
    live = importlib.import_module("pipeline.social").live_slugs()  # 이미 배포된 글은 고치지 않는다
    shorts = importlib.import_module("pipeline.shorts")
    out = []
    for draft_id, slot in sorted(sched.items(), key=lambda x: x[1]["date"]):
        at = datetime.fromisoformat(slot.get("at") or slot["date"] + "T07:00:00+09:00")
        path = shorts.post_path(draft_id)
        if not path or at <= now or not stale(path):
            continue
        if draft_id in live:
            continue
        out.append(path)
    return out


def ensure_upcoming(limit: int = 1) -> list[str]:
    done = []
    for path in upcoming()[:limit]:
        try:
            figs = make(path)
            done.append(f"🖼 블로그 그림 {len(figs)}개: {load_draft(path)['blog'].get('title', '')}")
        except llm.ClaudeAuthExpired:
            raise
        except (FigureError, llm.LLMError) as e:
            save_json(path / "figures.json", {"made_at": now_iso(), "figures": [], "error": str(e)[:300]})
            done.append(f"⚠️ 블로그 그림 실패 ({path.name}): {e}")
    return done


# ---------------- 사람 승인 (2026-11-01) ----------------

def _listed(path: Path) -> list[dict]:
    f = path / "figures.json"
    if not f.exists():
        return []
    return [x for x in load_json(f).get("figures") or [] if (path / "figures" / x["file"]).exists()]


def figures_hash(path: Path) -> str:
    """승인 대상 지문: 본문 + 그림 파일 + 설명 글자. 하나라도 바뀌면 승인은 무효."""
    import hashlib
    h = hashlib.sha1(blog_hash(path).encode())
    for fig in _listed(path):
        h.update(json.dumps(fig, ensure_ascii=False, sort_keys=True).encode())
        h.update((path / "figures" / fig["file"]).read_bytes())
    return h.hexdigest()[:16]


def _review(path: Path) -> dict:
    return load_json(path / "figures.json").get("review") or {}


def _set_review(path: Path, **fields) -> None:
    data = load_json(path / "figures.json")
    data["review"] = {**(data.get("review") or {}), **fields}
    save_json(path / "figures.json", data)


def approved(path: Path) -> list[dict]:
    """블로그에 넣어도 되는 그림 — 사람이 승인했고 그 뒤로 바뀌지 않은 것만."""
    figs = _listed(path)
    if not figs:
        return []
    r = _review(path)
    return figs if r.get("status") == "approved" and r.get("hash") == figures_hash(path) else []


def approve(path: Path, by: str) -> int:
    figs = _listed(path)
    if figs:
        _set_review(path, status="approved", hash=figures_hash(path), by=by, at=now_iso())
    return len(figs)


def pending(now: datetime | None = None) -> list[Path]:
    """게시 전 승인 글 중 그림이 있는데 아직 승인도 제외도 안 된 것 (게시 순서)."""
    now = now or datetime.now(KST)
    sched_file = content_dir() / "site" / "schedule.json"
    sched = load_json(sched_file) if sched_file.exists() else {}
    live = importlib.import_module("pipeline.social").live_slugs()
    shorts = importlib.import_module("pipeline.shorts")
    out = []
    for draft_id, slot in sorted(sched.items(), key=lambda x: x[1]["date"]):
        at = datetime.fromisoformat(slot.get("at") or slot["date"] + "T07:00:00+09:00")
        path = shorts.post_path(draft_id)
        if not path or at <= now or draft_id in live or not _listed(path) or approved(path):
            continue
        r = _review(path)
        if r.get("status") == "rejected" and r.get("hash") == figures_hash(path):
            continue
        out.append(path)
    return out


def list_message(paths: list[Path] | None = None) -> str:
    paths = pending() if paths is None else paths
    if not paths:
        return ""
    sched = load_json(content_dir() / "site" / "schedule.json")
    lines = ["[블로그 그림 승인] 답장: '그림 1,3 승인' / '그림 2 빼기' / '그림 전체 승인' — 승인 전에는 그림 없이 게시됩니다"]
    media = []
    for n, path in enumerate(paths, 1):
        draft = load_draft(path)
        day = (sched.get(path.name) or {}).get("date", "")
        lines += ["", f"{n}. {day} {draft['blog'].get('title', '')}"]
        for fig in _listed(path):
            lines.append(f"· [{fig.get('after_heading', '')}] {fig.get('caption', '')} ({fig.get('source', '')})")
            media.append(path / "figures" / fig["file"])
    return "\n".join(lines + [""] + [f"MEDIA:`{m}`" for m in media])


def ask_message() -> str:
    """새로 승인이 필요한 그림이 생겼을 때만 (같은 그림을 두 번 묻지 않는다)."""
    paths = pending()
    fresh = [p for p in paths if _review(p).get("asked") != figures_hash(p)]
    if not fresh:
        return ""
    message = list_message(paths)
    for path in fresh:
        _set_review(path, asked=figures_hash(path))
    return message


def apply(text: str) -> list[str]:
    """'1,3 승인' / '2 빼기' / '전체 승인' — 번호는 list_message 순서."""
    m = re.match(r"\s*(?:그림\s*)?(전체|[\d,\s]+?)\s*(승인|빼기|제외|반려)\s*$", text)
    if not m:
        raise FigureError("형식: '1,3 승인' / '2 빼기' / '전체 승인'")
    paths = pending()
    nums = range(1, len(paths) + 1) if m.group(1) == "전체" else [int(x) for x in re.findall(r"\d+", m.group(1))]
    if bad := [n for n in nums if not 1 <= n <= len(paths)]:
        raise FigureError(f"없는 번호: {bad} (승인 대기 {len(paths)}개)")
    out = []
    for n in nums:
        path = paths[n - 1]
        title = load_draft(path)["blog"].get("title", "")
        if m.group(2) == "승인":
            out.append(f"✅ 그림 {approve(path, 'telegram')}개 승인: {title}")
        else:
            _set_review(path, status="rejected", hash=figures_hash(path), at=now_iso())
            out.append(f"🚫 그림 없이 게시: {title}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="블로그 설명 그림")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("draft_id")
    sub.add_parser("upcoming")
    sub.add_parser("list")
    a = sub.add_parser("apply")
    a.add_argument("reply")
    args = ap.parse_args(argv)
    if args.cmd == "make":
        path = importlib.import_module("pipeline.shorts").post_path(args.draft_id)
        if not path:
            raise FigureError(f"글 없음: {args.draft_id}")
        print(json.dumps(make(path), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(list_message() or "승인 대기 그림 없음")
    elif args.cmd == "apply":
        print("\n".join(apply(args.reply)))
    else:
        print("\n".join(ensure_upcoming()) or "만들 그림 없음")
    return 0


if __name__ == "__main__":
    run_cli("figures", main)
