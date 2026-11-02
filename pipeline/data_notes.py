"""공공 데이터 글 (2026-11-02, docs/data-content-strategy.md) — 블로그와 별개, /data/notes/<id>/.

숫자는 코드가 데이터 세트에서 계산하고 AI 는 설명만 쓴다. 본문의 숫자는 코드가 목록과 대조하고(없으면 차단),
Gemini 가 해석을 검수하며, 사람이 승인한 글만 공개한다 (신뢰 단계 자동 승인 없음). 병원명·병원별 수치는 넣지 않는다.

    python -m pipeline.data_notes draft where
    python -m pipeline.data_notes list
    python -m pipeline.data_notes approve where
    python -m pipeline.data_notes reject where
"""

from __future__ import annotations

import argparse
import importlib
import re
from collections import Counter

from pipeline import dataset, llm
from pipeline.common import content_dir, get_logger, load_json, now_iso, prompt, run_cli, save_json

log = get_logger("data_notes")

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
TOP_CHART = 12
LIMITS = """- The count covers institutions that list a dermatology department, including many general clinics. It does not count dermatologists or specialist clinics.
- It says nothing about quality, safety, price or which institutions treat foreign visitors.
- The source does not explain why the numbers differ, so no cause can be stated."""
SURFACE, INK, MUTED, GRID, BAR = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#2a78d6"


class NoteError(RuntimeError):
    pass


def notes_dir():
    return content_dir() / "site" / "data_notes"


def note_file(note_id: str):
    return notes_dir() / note_id / "note.json"


def _district(name: str, province: str) -> str:
    return name if name.startswith(province) else f"{name}, {province}"  # "Busan Haeundae-gu, Busan" 처럼 겹치지 않게


def build_where(data: dict) -> dict:
    """1번 글: 지역 분포. 숫자 목록·표·그래프 값을 코드가 만든다."""
    names, total = dataset.regions_en(), data["totals"]["total"]
    by_province: Counter = Counter()
    for r in data["regions"]:
        by_province[r["sido"]] += r["total"]
    share = lambda n: f"{100 * n / total:.1f}%"
    districts = [{"label": _district(names.get(r["sggu"], r["sggu"]), names[r["sido"]]), "value": r["total"]} for r in data["regions"]]
    provinces = by_province.most_common()
    lines = [f"As of date: {data['as_of']}", "Source: Korea's Health Insurance Review & Assessment Service (HIRA)",
             f"Institutions listing a dermatology department nationwide: {total:,}"]
    lines += [f"{names[k]}: {v:,} institutions, {share(v)} of the national total" for k, v in provinces[:3]]
    lines += [f"{d['label']}: {d['value']:,} institutions, {share(d['value'])} of the national total" for d in districts[:3]]
    lines.append(f"Number of districts in the data: {len(districts)}")
    return {"topic": "Where Korea's institutions with a dermatology department are concentrated", "numbers": "\n".join(lines),
            "limits": LIMITS, "chart": {"title": "Institutions with a dermatology department, top districts", "rows": districts[:TOP_CHART]},
            "table": [["District", "Institutions", "Share of national total"]] + [[d["label"], f"{d['value']:,}", share(d["value"])] for d in districts[:TOP_CHART]]}


BUILDERS = {"where": build_where}


def number_problems(text: str, numbers: str) -> list[str]:
    """목록에 없는 숫자. 글머리 번호처럼 숫자 하나로 끝나는 표기도 전부 대조한다."""
    allowed = {n.replace(",", "") for n in NUMBER.findall(numbers)}
    return [f"목록에 없는 숫자: {n}" for n in dict.fromkeys(NUMBER.findall(text)) if n.replace(",", "") not in allowed]


def rule_problems(text: str) -> list[str]:
    social = importlib.import_module("pipeline.social")
    return social.text_problems(text, set(social.NUMBER.findall(text)))  # 숫자는 number_problems 가 따로 본다


def chart(spec: dict, out) -> bool:
    """가로 막대 하나. matplotlib 이 없으면 그래프 없이."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib 없음 — 그래프 없이")
        return False
    rows = spec["rows"][::-1]
    plt.rcParams.update({"font.family": ["Malgun Gothic", "DejaVu Sans"], "text.color": INK, "ytick.color": MUTED})
    fig, ax = plt.subplots(figsize=(9, 6), dpi=150, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.barh([r["label"] for r in rows], [r["value"] for r in rows], 0.68, color=BAR, edgecolor=SURFACE, linewidth=2)
    peak = max(r["value"] for r in rows)
    for y, r in enumerate(rows):
        ax.text(r["value"] + peak * 0.012, y, f"{r['value']:,}", va="center", fontsize=10)
    ax.set_xlim(0, peak * 1.12)
    ax.xaxis.set_visible(False)
    ax.spines[["top", "right", "bottom", "left"]].set_visible(False)
    ax.tick_params(length=0, labelsize=10)
    ax.set_title(spec["title"], loc="left", fontsize=13, fontweight="bold", pad=14)
    fig.text(0.01, 0.01, "Not a count of dermatologists. Not a ranking of quality. Source: HIRA. skinboundkorea.com/data/", fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return True


def draft(note_id: str) -> dict:
    if note_id not in BUILDERS:
        raise NoteError(f"없는 글: {note_id} (가능: {', '.join(BUILDERS)})")
    data = dataset.load()
    if not data:
        raise NoteError("데이터가 없습니다: python -m pipeline.dataset refresh")
    spec = BUILDERS[note_id](data)
    problems: list[str] = []
    written, ko = {}, ""
    for _ in range(2):  # 걸리면 지적을 주고 한 번 더
        fix = "\n\nA reviewer rejected your previous note. Fix these problems:\n" + "\n".join(f"- {p}" for p in problems) if problems else ""
        written, _r = llm.generate_json("data_note_write", prompt("data_note_write", topic=spec["topic"], numbers=spec["numbers"], limits=spec["limits"]) + fix)
        text = "\n".join(str(written.get(k, "")) for k in ("title", "description", "markdown"))
        problems = number_problems(text, spec["numbers"]) + rule_problems(text)
        if not written.get("markdown"):
            problems.append("본문 없음")
        if not problems:
            review, _r = llm.generate_json("data_note_review", prompt("data_note_review", numbers=spec["numbers"], limits=spec["limits"], note=text))
            ko = review.get("ko", "")
            if not review.get("ok"):
                problems = [i.get("problem", "") if isinstance(i, dict) else str(i) for i in review.get("issues", [])] or ["검수 통과 못 함"]
        if not problems:
            break
    note = {"id": note_id, "status": "blocked" if problems else "review", "problems": problems, "as_of": data["as_of"], "created_at": now_iso(),
            "title": written.get("title", ""), "description": written.get("description", ""), "markdown": written.get("markdown", ""), "ko": ko,
            "table": spec["table"], "numbers": spec["numbers"]}
    note_file(note_id).parent.mkdir(parents=True, exist_ok=True)
    note["has_chart"] = chart(spec["chart"], note_file(note_id).with_name("chart.png"))
    save_json(note_file(note_id), note)
    return note


def load(note_id: str) -> dict:
    if not note_file(note_id).exists():
        raise NoteError(f"{note_id} 글이 없습니다")
    return load_json(note_file(note_id))


def published() -> list[dict]:
    """승인된 글만, 최근 순."""
    folder = notes_dir()
    notes = [load_json(f) for f in folder.glob("*/note.json")] if folder.exists() else []
    return sorted((n for n in notes if n["status"] == "approved"), key=lambda n: n.get("approved_at", ""), reverse=True)


def review_message(note: dict) -> str:
    if note["status"] == "blocked":
        return f"[데이터 글 {note['id']}] ⛔ 검사에 걸려 승인 요청을 보내지 않았습니다\n" + "\n".join(f"- {p}" for p in note["problems"])
    lines = [f"[데이터 글 검수] {note['id']} (기준 {note['as_of']})", f"승인: `데이터글 승인 {note['id']}` · 폐기: `데이터글 폐기 {note['id']}`", "",
             f"제목: {note['title']}", "", note.get("ko", ""), "", "📊 코드가 계산한 숫자", note["numbers"]]
    if note.get("has_chart"):
        lines += ["", f"MEDIA:`{note_file(note['id']).with_name('chart.png')}`"]
    return "\n".join(lines)


def list_message() -> str:
    folder = notes_dir()
    notes = [load_json(f) for f in sorted(folder.glob("*/note.json"))] if folder.exists() else []
    current = (dataset.load() or {}).get("as_of")
    if not notes:
        return "데이터 글이 없습니다. 만들려면: `데이터글 초안 where`"
    return "\n".join(f"- {n['id']}: {n['status']} · 기준 {n['as_of']}" + (" · ⚠️ 새 데이터 있음" if current and n["as_of"] != current else "") + f" · {n['title']}" for n in notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="공공 데이터 글")
    parser.add_argument("cmd", choices=["draft", "list", "approve", "reject"])
    parser.add_argument("note_id", nargs="?", default="")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            print(list_message())
        elif args.cmd == "draft":
            print(review_message(draft(args.note_id)))
        else:
            note = load(args.note_id)
            if args.cmd == "approve" and note["status"] not in ("review", "approved"):
                raise NoteError(f"{args.note_id}: 승인할 수 없는 상태입니다 ({note['status']})")
            note.update(status="approved" if args.cmd == "approve" else "rejected", approved_at=now_iso())
            save_json(note_file(args.note_id), note)
            print(f"✅ {args.note_id}: {note['status']}" + (" — 다음 배포 때 공개됩니다" if args.cmd == "approve" else ""))
    except NoteError as e:
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("data_notes", main)
