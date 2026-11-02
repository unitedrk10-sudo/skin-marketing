"""공공 데이터 글 (2026-11-02, docs/data-content-strategy.md) — 블로그와 별개, /data/notes/<id>/.

숫자는 코드가 데이터 세트에서 계산하고 AI 는 설명만 쓴다. 본문의 숫자는 코드가 목록과 대조하고(없으면 차단),
Gemini 가 해석을 검수하며, 사람이 승인한 글만 공개한다 (신뢰 단계 자동 승인 없음). 병원명·병원별 수치는 넣지 않는다.

    python -m pipeline.data_notes draft where
    python -m pipeline.data_notes draft changes   # 직전 기준 대비 분기 변화 → changes-<기준일>
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
SPELLED_PERIOD = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)[- ](?:day|week|month|quarter|year)s?\b", re.I)
TOP_CHART = 12
LIMITS = """- The count covers institutions that list a dermatology department, including many general clinics. It does not count dermatologists or specialist clinics.
- It says nothing about quality, safety, price or which institutions treat foreign visitors.
- The source does not explain why the numbers differ, so no cause can be stated."""
CHANGE_LIMITS = """- HIRA publishes the counts. Skinbound Korea made this comparison from two retrievals, so never say HIRA compared, reported or found a change.
- The figures compare two dates only, so they do not show a long-term trend.
- A change is a net difference. It does not separate openings, closures, relocations or changes in listed departments.
- Districts present on only one date are not compared, because they may have been renamed or reorganized."""
TOP_CHANGES = 6
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


def _counts(rows: list[dict], key) -> dict:
    out: Counter = Counter()
    for r in rows:
        out[key(r)] += r["total"]
    return dict(out)


def _english(names: dict, korean: str) -> str:
    if korean not in names:  # 한국어 이름이 영어 글에 그대로 나가지 않게
        raise NoteError(f"영문 지역명 없음: {korean} (python -m pipeline.dataset regions)")
    return names[korean]


def _direction(diff: int) -> str:
    return f"increased by {diff:,}" if diff > 0 else f"decreased by {-diff:,}" if diff < 0 else "unchanged"


def _compare(before: dict, after: dict, label) -> list[dict]:
    """두 시점에 다 있는 곳만, 변화 큰 순. 한쪽에만 있는 곳은 이름 변경·개편일 수 있어 증감으로 치지 않는다."""
    rows = [{"label": label(k), "before": before[k], "after": after[k], "value": after[k] - before[k]} for k in before.keys() & after.keys()]
    return sorted(rows, key=lambda r: (-abs(r["value"]), r["label"]))


def _leaders(rows: list[dict], n: int) -> tuple[list[dict], dict | None]:
    """변화 큰 순 상위 n. 같은 값은 임의로 자르지 않는다: 몇 개 더면 함께, 너무 많으면 이름 없이 개수만."""
    if len(rows) <= n:
        return rows, None
    cut = abs(rows[n - 1]["value"])
    level = [r for r in rows if abs(r["value"]) >= cut]
    if len(level) <= n + 2:
        return level, None
    above = [r for r in rows if abs(r["value"]) > cut]
    return above, {"count": len(level) - len(above), "value": rows[n - 1]["value"]}


def build_changes(data: dict, previous: dict) -> dict:
    """분기 변화 글: 직전 기준과 비교. 숫자 목록·표·그래프 값을 코드가 만든다."""
    names, then, now = dataset.regions_en(), previous["as_of"], data["as_of"]
    pct = lambda r: f"{100 * r['value'] / r['before']:+.1f}%"
    region = lambda rows: _counts(rows, lambda r: (r["sido"], r["sggu"]))
    before, after = region(previous["regions"]), region(data["regions"])
    districts = _compare(before, after, lambda k: _district(_english(names, k[1]), _english(names, k[0])))
    provinces = _compare(_counts(previous["regions"], lambda r: r["sido"]), _counts(data["regions"], lambda r: r["sido"]), lambda k: _english(names, k))
    ups = [d for d in districts if d["value"] > 0]
    downs = [d for d in districts if d["value"] < 0]
    old_total, new_total = previous["totals"]["total"], data["totals"]["total"]
    diff = new_total - old_total
    line = lambda r: f"{r['label']}: {_direction(r['value'])}, from {r['before']:,} to {r['after']:,} ({pct(r)})"
    lines = [f"Latest date: {now}", f"Earlier date: {then}", "Source: Korea's Health Insurance Review & Assessment Service (HIRA)",
             f"Institutions listing a dermatology department nationwide: {_direction(diff)}, from {old_total:,} to {new_total:,} ({diff / old_total * 100:+.1f}%)"]
    for heading, rows in (("Provinces with the largest change", [p for p in provinces if p["value"]]), ("Districts with the largest increase", ups), ("Districts with the largest decrease", downs)):
        shown, tied = _leaders(rows, 3)
        lines += [f"{heading}:"] + [line(r) for r in shown]
        if tied:
            lines.append(f"Tied at the next level: {tied['count']}, each {_direction(tied['value'])}. The source names them; this note leaves the names out for length")
    lines += [f"Districts compared: {len(districts)}", f"Districts that increased: {len(ups)}", f"Districts that decreased: {len(downs)}",
              f"Districts unchanged: {len(districts) - len(ups) - len(downs)}", f"Districts present on only one date, not compared: {len(before.keys() ^ after.keys())}"]
    picked = _leaders(ups, TOP_CHANGES)[0] + _leaders(downs, TOP_CHANGES)[0]
    unit = "district" if picked else "province"  # 동률이 너무 많아 지역을 못 고르면 빠짐없는 시·도 표로
    picked = sorted(picked or [p for p in provinces if p["value"]], key=lambda d: -d["value"])
    return {"topic": f"What changed in Korea's institutions with a dermatology department between {then} and {now}", "numbers": "\n".join(lines),
            "limits": LIMITS + "\n" + CHANGE_LIMITS, "compare_to": then,
            "chart": {"title": f"Net change by {unit}, {then} to {now}", "signed": True, "rows": picked},
            "table": [[unit.capitalize(), then, now, "Change", "Change %"]] + [[d["label"], f"{d['before']:,}", f"{d['after']:,}", f"{d['value']:+,}", pct(d)] for d in picked]}


BUILDERS = {"where": build_where}


def number_problems(text: str, numbers: str) -> list[str]:
    """목록에 없는 숫자. 글머리 번호처럼 숫자 하나로 끝나는 표기도 전부 대조한다."""
    allowed = {n.replace(",", "") for n in NUMBER.findall(numbers)}
    return [f"목록에 없는 숫자: {n}" for n in dict.fromkeys(NUMBER.findall(text)) if n.replace(",", "") not in allowed] + [
        f"목록에 없는 기간 표현: {m.group(0)}" for m in SPELLED_PERIOD.finditer(text)]


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
    if not rows:
        return False
    signed = spec.get("signed")
    plt.rcParams.update({"font.family": ["Malgun Gothic", "DejaVu Sans"], "text.color": INK, "ytick.color": MUTED})
    fig, ax = plt.subplots(figsize=(9, 6), dpi=150, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.barh([r["label"] for r in rows], [r["value"] for r in rows], 0.68, color=BAR, edgecolor=SURFACE, linewidth=2)
    peak = max(abs(r["value"]) for r in rows)
    for y, r in enumerate(rows):
        negative = r["value"] < 0
        ax.text(r["value"] + (-1 if negative else 1) * peak * 0.012, y, f"{r['value']:+,}" if signed else f"{r['value']:,}", va="center", ha="right" if negative else "left", fontsize=10)
    if signed:  # 값 글자가 지역 이름과 겹치지 않게 양쪽에 같은 여백
        ax.set_xlim(min(0, min(r["value"] for r in rows)) - peak * 0.14, max(0, max(r["value"] for r in rows)) + peak * 0.14)
        ax.axvline(0, color=MUTED, linewidth=1)
    else:
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
    if note_id != "changes" and note_id not in BUILDERS:
        raise NoteError(f"없는 글: {note_id} (가능: {', '.join(BUILDERS)}, changes)")
    data = dataset.load()
    if not data:
        raise NoteError("데이터가 없습니다: python -m pipeline.dataset refresh")
    if note_id == "changes":  # 분기마다 새 글, 지난 분기 글은 그대로 둔다
        previous = dataset.previous_snapshot(data["as_of"])
        if not previous:
            raise NoteError("비교할 이전 데이터가 없습니다. 다음 분기 갱신 뒤에 만들 수 있습니다")
        spec, note_id = build_changes(data, previous), f"changes-{data['as_of']}"
    else:
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
            "table": spec["table"], "numbers": spec["numbers"], "compare_to": spec.get("compare_to")}
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
    compared = f", {note['compare_to']} 대비 변화" if note.get("compare_to") else ""
    lines = [f"[데이터 글 검수] 전국 데이터 (기준 {note['as_of']}{compared})", "승인: `데이터글 승인` · 폐기: `데이터글 폐기`", "",
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
    return "\n".join(f"- {n['id']}: {n['status']} · 기준 {n['as_of']}" + (" · ⚠️ 새 데이터 있음" if current and n["as_of"] != current and not n.get("compare_to") else "") + f" · {n['title']}" for n in notes)


def _only_waiting() -> str:
    folder = notes_dir()
    waiting = [n["id"] for n in map(load_json, folder.glob("*/note.json")) if n["status"] == "review"] if folder.exists() else []
    if len(waiting) != 1:
        raise NoteError(f"검수 대기 데이터 글이 {len(waiting)}개입니다" + (f": {', '.join(waiting)}" if waiting else ""))
    return waiting[0]


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
            note = load(args.note_id or _only_waiting())
            if args.cmd == "approve" and note["status"] not in ("review", "approved"):
                raise NoteError(f"{args.note_id}: 승인할 수 없는 상태입니다 ({note['status']})")
            note.update(status="approved" if args.cmd == "approve" else "rejected", approved_at=now_iso())
            save_json(note_file(note["id"]), note)
            print(f"✅ {note['title']}: {'승인, 다음 배포 때 공개됩니다' if args.cmd == 'approve' else '폐기'}")
    except NoteError as e:
        print(f"⚠️ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("data_notes", main)
