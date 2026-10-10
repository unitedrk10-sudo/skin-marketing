"""3. 사람 검수 처리 — 텔레그램 답장을 해석해 파일 상태를 옮긴다 (기획서 6-1, 6-2).

상태 이동은 이 스크립트만 한다. 허용 이동: drafts→approved, drafts→rejected, rendered→ready_to_publish, rendered→rejected,
approved→rejected (승인 철회 — 영상 렌더링·블로그 게시 전에 거둬들일 때).

    python -m pipeline.03_review list [--stage drafts|rendered|approved]   # 검수 요청 메시지 출력 + 번호표 저장
    python -m pipeline.03_review apply "2 폐기" --stage approved            # 승인 철회 (먼저 list --stage approved)
    python -m pipeline.03_review apply "1,3,4 승인" [--stage drafts]
    python -m pipeline.03_review apply "2 수정: 가격 출처 다시"
    python -m pipeline.03_review apply "5 폐기"
    python -m pipeline.03_review apply "전체 승인" [--confirm]           # ⚠️/⛔ 건은 --confirm 있어야 승인
    python -m pipeline.03_review apply "게시 OK" --stage rendered        # 또는 "1,2 게시 OK"
    python -m pipeline.03_review apply "2 병원확인"                       # 스폰서 글: 광고주 병원이 최종본을 확인함

스폰서 글은 병원 확인(현재 내용 기준)이 없으면 --confirm 으로도 승인되지 않는다 (광고 주체 = 병원).

번호는 마지막 `list` 가 만든 번호표(<stage>/_batch.json) 기준이다.
종료 코드: 0 처리 완료, 2 답장을 해석하지 못함(사람에게 다시 묻기), 1 실행 오류.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pipeline.common import (
    blog_text,
    draft_hash,
    move_draft,
    content_dir,
    draft_dirs,
    PRACTICE_KIND,
    get_logger,
    is_medical_fact,
    load_draft,
    load_json,
    load_review,
    log_dir,
    now_iso,
    run_cli,
    save_json,
    script_text,
    shortform_on,
)

log = get_logger("03_review")

ALLOWED_MOVES = {("drafts", "approved"), ("drafts", "rejected"), ("rendered", "ready_to_publish"), ("rendered", "rejected"),
                 ("approved", "rejected"),
                 ("approved", "drafts"), ("rendered", "drafts")}  # 되돌리기: 블로그에 아직 안 올라간 승인 글을 고치려고 검수로 (2026-10-10)
GRADE_ICON = {"pass": "✅", "caution": "⚠️", "pending": "⏳", "block": "⛔", None: "❔"}
NUMS = r"(\d+(?:\s*[,，]\s*\d+|\s*~\s*\d+|\s*-\s*\d+|\s+\d+)*)"


class ReplyError(ValueError):
    pass


@dataclass
class Command:
    action: str  # approve | revise | reject | publish_ok | sponsor_ok | reopen
    numbers: list[int] = field(default_factory=list)  # 빈 목록 = 전체
    note: str = ""


def _numbers(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,，\s]+", text.strip()):
        rng = re.fullmatch(r"(\d+)[~-](\d+)", part)
        if rng:
            out += list(range(int(rng.group(1)), int(rng.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def parse_reply(reply: str) -> list[Command]:
    """권장 형식의 답장을 명령 목록으로. 자연어 해석은 Hermes 스킬(hermes/skills/skin-marketing/SKILL.md)이 이 형식으로 바꿔서 넘긴다."""
    commands: list[Command] = []
    # 수정 요청은 줄 끝까지가 메모이므로 줄 단위로 나눈다
    for raw in re.split(r"[\n;]+", reply):
        line = raw.strip()
        if not line:
            continue
        if m := re.fullmatch(r"(?:전체|모두|all)\s*(?:승인|approve)", line, re.I):
            commands.append(Command("approve"))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:승인|approve|ok)", line, re.I):
            commands.append(Command("approve", _numbers(m.group(1))))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:병원\s*확인|clinic\s*ok|sponsor\s*ok)", line, re.I):
            commands.append(Command("sponsor_ok", _numbers(m.group(1))))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:되돌리기|되돌려|reopen)(?:\s*(?:수정)?\s*[:：]\s*(.+))?", line, re.I):
            commands.append(Command("reopen", _numbers(m.group(1)), (m.group(2) or "").strip()))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:폐기|reject|drop)", line, re.I):
            commands.append(Command("reject", _numbers(m.group(1))))
        elif m := re.fullmatch(NUMS + r"\s*(?:번)?\s*(?:[가-힣]+\s*)?(?:수정|revise|fix)\s*[:：]\s*(.+)", line, re.I):
            commands.append(Command("revise", _numbers(m.group(1)), m.group(2).strip()))
        elif m := re.fullmatch(r"(?:" + NUMS + r"\s*(?:번)?\s*)?게시\s*ok|(?:" + NUMS + r"\s*)?publish\s*ok", line, re.I):
            commands.append(Command("publish_ok", _numbers(m.group(1) or m.group(2) or "")))
        else:
            raise ReplyError(f"해석할 수 없는 답장: {line!r}")
    if not commands:
        raise ReplyError("빈 답장")
    return commands


# ---------------- 번호표 ----------------

def batch_path(stage: str) -> Path:
    return content_dir(stage) / "_batch.json"


def build_batch(stage: str) -> list[str]:
    ids = [p.name for p in draft_dirs(stage)]
    save_json(batch_path(stage), {"stage": stage, "created_at": now_iso(), "ids": ids})
    return ids


def load_batch(stage: str) -> list[str]:
    path = batch_path(stage)
    if not path.exists():
        raise ReplyError(f"번호표가 없습니다. 먼저 `03_review list --stage {stage}` 로 검수 요청을 보내세요.")
    return load_json(path)["ids"]


ISSUE_ICON = {"block": "⛔", "caution": "⚠️", "pending": "⏳"}
MAX_ISSUES = 3


def _issues(review: dict | None) -> list[dict]:
    order = {"block": 0, "pending": 1, "caution": 2}
    found = [f for f in (review or {}).get("findings", []) if f["severity"] in order]
    return sorted(found, key=lambda f: order[f["severity"]])


def review_doc(path: Path) -> Path:
    """휴대폰에서 읽는 검수용 파일 (텔레그램 첨부): 걸린 항목 → 대본 → 블로그 → 사실별 원문 인용·출처."""
    draft, review = load_draft(path), load_review(path)
    ko = _korean(path)
    out = [f"# [{code(draft['id'])}] {draft['id']}", "",
           f"자동 검수: {GRADE_ICON[(review or {}).get('grade')]} {(review or {}).get('grade') or '미실시'}"]
    if ko:
        out.append("(한국어 번역은 검수용 참고 — 게시되는 글은 아래 '원문(영어)' 그대로)")
    issues = _issues(review)
    if issues:
        out += ["", "## 검수에서 걸린 항목"]
        msgs = ko["findings"] if ko else [f["message"] for f in issues]
        out += [f"- {ISSUE_ICON[f['severity']]} {m}" + (f"\n  > {f['quote']}" if f["quote"] else "") for f, m in zip(issues, msgs)]
    if (review or {}).get("always_human"):
        out += ["", "👤 사람 확인 필요: " + ", ".join(review["always_human"])]
    if ko:
        out += ["", "## 블로그 (한국어 번역)", "", ko["blog"], "", "## 원문(영어) — 블로그", "", blog_text(draft)]
    else:
        out += ["", "## 블로그", "", blog_text(draft)]
    if shortform_on():
        out += ["", "## 숏폼 대본", "", script_text(draft)]
    out += ["", "## 사실과 원문 인용"]
    for f in draft.get("facts", []):
        out.append(f"- [{f['id']}] {f['text']}\n  > {f.get('quote', '')}\n  {f['url']}")
    doc = path / f"{draft['id']}.md"
    doc.write_text("\n".join(out) + "\n", encoding="utf-8")
    return doc


def code(draft_id: str) -> str:
    """사람이 부르기 쉬운 고정 글번호: 2026-W41-07-hanbok… → W41-07 (주차-주제 번호, 검수 단계가 바뀌어도 같다)."""
    m = re.match(r"\d{4}-(W\d{2})-(\d{2})", draft_id)
    return f"{m.group(1)}-{m.group(2)}" if m else draft_id[:14]


def _korean(path: Path) -> dict | None:
    """만들어 둔 검수용 한국어 번역 (워커가 메시지 보내기 전에 만든다). 없으면 None → 영어."""
    try:
        return importlib.import_module("pipeline.translate").cached(path)
    except Exception:  # noqa: BLE001 — 번역 문제로 검수 메시지가 막히면 안 된다
        return None


def draft_message(path: Path, number: int, total: int) -> str:
    """텔레그램에 초안 하나를 통째로 보내는 메시지 — 걸린 항목 → 대본 → 블로그 전문 → 출처 (길면 Hermes 가 나눠 보낸다)."""
    draft, review = load_draft(path), load_review(path)
    sf, blog = draft.get("shortform") or {}, draft.get("blog") or {}
    out = [f"[초안 {number}/{total} · 글번호 {code(draft['id'])}] {GRADE_ICON[(review or {}).get('grade')]} {title(draft)}",
           f"답장 예: `{number} 승인` · `{number} 수정: …` (또는 `{code(draft['id'])} 승인`)"]
    if draft.get("sponsor"):
        out.append(f"💼 {draft['sponsor']['name_ko']} 광고 · 병원확인 {'✔' if sponsor_confirmed(path) else '대기'}")
    if (review or {}).get("always_human"):
        out.append("👤 " + ", ".join(review["always_human"]))
    fixes = [r["note"] for r in draft.get("revisions", []) if r.get("kind") == "fix"]
    if fixes:
        out.append(f"🛠 {fixes[-1]} — 아래는 고친 뒤에도 남은 지적")
    ko = _korean(path)  # 검수용 한국어 번역 (없거나 실패하면 영어 원문)
    if ko:
        out.append("🇰🇷 검수용 한국어 번역 — 게시되는 글은 영어 원문 그대로 (첨부 파일 끝에 원문)")
    issues = _issues(review)
    msgs = ko["findings"] if ko else [f["message"] for f in issues]
    out += ["", "🔎 남은 지적" + ("" if issues else ": 없음")] + [f"{ISSUE_ICON[f['severity']]} {m}" for f, m in zip(issues, msgs)]
    medical = medical_sentences(draft)
    out += ["", f"🩺 의료 정보 확인 ({len(medical)}문장) — 문장과 출처가 맞는지 봐 주세요"]
    for i, (text, urls) in enumerate(medical):
        out.append(f"• {ko['medical'][i] if ko else text}")
        out += [f"   ↳ {u}" for u in urls]
    practice = medical_sentences(draft, lambda f: f.get("kind") == PRACTICE_KIND)
    if practice:  # 블로그에는 병원 링크 없이 "[clinic websites]" 로만 나간다
        out += ["", f"🏥 병원 사이트 공통 정보 ({len(practice)}문장) — 블로그엔 병원명·링크 없이 게시, 근거 병원 확인용"]
        for i, (text, urls) in enumerate(practice):
            out.append(f"• {ko['practice'][i] if ko else text}")
            out += [f"   ↳ {u}" for u in urls]
    if shortform_on():
        out += ["", "🎬 숏폼 대본"]
        for i, ln in enumerate(sf.get("lines", []), 1):
            refs = f"  [{','.join(ln['fact_ids'])}]" if ln.get("fact_ids") else ""
            out.append(f"{i}. {ko['script'][i - 1] if ko and ko['script'] else ln.get('voice', '')}{refs}")
        out.append(f"화면 표기: {sf.get('on_screen_disclosure', '')}")
    if ko:  # 숏폼이 없으니 블로그 전문(한국어)을 바로 읽게 한다
        out += ["", f"📝 블로그 전문 (한국어 번역) — 영어 제목: {blog.get('title', '')}", "", ko["blog"]]
    else:
        sections = re.findall(r"(?m)^##\s+(.+)$", blog.get("markdown", ""))
        out += ["", f"📝 블로그: {blog.get('title', '')}" + (f" ({' · '.join(s.strip() for s in sections)})" if sections else ""),
                f"   여행·일반 문장은 생략했습니다. 전문: `{number}번 블로그 보여줘`"]
    return "\n".join(out)


def title(draft: dict) -> str:
    """검수 메시지 제목 — 숏폼이 꺼져 있으면 블로그 제목 (영상 제목·길이 없음)."""
    sf, blog = draft.get("shortform") or {}, draft.get("blog") or {}
    if shortform_on() and sf.get("title"):
        return sf["title"] + (f" ({sf['estimated_seconds']}s)" if sf.get("estimated_seconds") else "")
    return blog.get("title") or draft["topic"]["title"]


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\n+")
MD_NOISE = re.compile(r"^[\s>*#-]+|\*\*|__|\s*\[F\d+\]")


def medical_sentences(draft: dict, which=is_medical_fact) -> list[tuple[str, list[str]]]:
    """블로그·대본에서 의료 사실(which 로 바꿀 수 있다)을 인용한 문장과 그 출처 주소 — 사람이 출처와 대조할 목록."""
    facts = {f["id"]: f for f in draft.get("facts", [])}

    def urls(ids: list[str]) -> list[str]:
        return list(dict.fromkeys(u for i in ids if i in facts and which(facts[i])
                                  for u in facts[i].get("urls") or [facts[i]["url"]]))

    out: list[tuple[str, list[str]]] = []
    for sentence in SENTENCE_END.split((draft.get("blog") or {}).get("markdown", "")):
        found = urls(re.findall(r"\[(F\d+)\]", sentence))
        if found:
            out.append((re.sub(r"\s+", " ", MD_NOISE.sub("", sentence)).strip(), found))
    for ln in (draft.get("shortform") or {}).get("lines", []) if shortform_on() else []:
        found = urls(ln.get("fact_ids", []))
        if found:
            out.append((f"(대본) {ln.get('voice', '')}", found))
    return out


def draft_messages(stage: str = "drafts") -> list[str]:
    """마지막 list_message 의 번호표 순서대로 초안별 메시지."""
    ids = load_batch(stage)
    return [draft_message(content_dir(stage) / d, i, len(ids)) for i, d in enumerate(ids, 1)
            if (content_dir(stage) / d / "draft.json").exists()]


def _draft_summary(path: Path, draft: dict, review: dict | None) -> list[str]:
    """번호 줄 아래에 붙는 요약: 블로그 구성 · 걸린 항목 · 대본 전문."""
    blog = draft.get("blog") or {}
    sections = re.findall(r"(?m)^##\s+(.+)$", blog.get("markdown", ""))
    out = [f"   📝 블로그: {blog.get('title', '')}" + (f" — {blog['meta_description']}" if blog.get("meta_description") else "")]
    if sections:
        out.append("      " + " · ".join(s.strip() for s in sections[:8]))
    issues = _issues(review)
    for f in issues[:MAX_ISSUES]:
        out.append(f"   {ISSUE_ICON[f['severity']]} {f['message'][:140]}")
    if len(issues) > MAX_ISSUES:
        out.append(f"   … 외 {len(issues) - MAX_ISSUES}건 (첨부 파일)")
    if shortform_on():
        out.append("   🎬 대본:")
        out += [f"      · {ln.get('voice', '')}" for ln in (draft.get("shortform") or {}).get("lines", [])]
    return out


def list_message(stage: str, compact: bool = False) -> str:
    """검수 요청 메시지. compact=True: 초안 전문을 따로 보낸 뒤의 요약 (번호·등급·대표 지적·답장 예시만)."""
    ids = build_batch(stage)
    if not ids:
        return {"drafts": "검수 대기 없음", "approved": "승인된 초안 없음"}.get(stage, "게시 전 확인 대기 없음")
    lines = []
    if stage == "drafts":
        lines.append(f"[초안 검수 {len(ids)}건 (블로그)] 답장 예: `1,3 승인` · `2 수정: 가격 출처 다시` · `4 폐기` · `전체 승인` (번호 대신 글번호 W41-07 도 됨)")
        lines.append("↑ 초안 전문은 위 메시지에 하나씩 보냈습니다." if compact else
                     "초안별 전문(블로그·사실별 원문 인용)은 첨부 파일로 보냅니다.")
    elif stage == "approved":
        lines.append(f"[승인됨 {len(ids)}건 — 영상·블로그 게시 전] 철회하려면: `2 폐기` (영상 렌더링 전까지만)")
    else:
        lines.append(f"[게시 전 확인 {len(ids)}건] 답장 예: `게시 OK` · `1,2 게시 OK` · `3 폐기`")
    attachments = []
    for i, draft_id in enumerate(ids, 1):
        path = content_dir(stage) / draft_id
        draft, review = load_draft(path), load_review(path)
        sf = draft.get("shortform") or {}
        grade = (review or {}).get("grade")
        head = f"{i}. [{code(draft_id)}] {title(draft)}"
        line = f"{head} {GRADE_ICON[grade]}"
        if draft.get("sponsor"):
            line += f" 💼{draft['sponsor']['name_ko']} 광고 · 병원확인 {'✔' if sponsor_confirmed(path) else '대기'}"
            blocked = (draft.get("platforms") or {}).get("blocked") or {}
            if blocked:
                line += " · " + ", ".join(blocked) + " 게시 불가"
        if review and review.get("always_human"):
            line += f" 👤{', '.join(review['always_human'])}"
        elif not review:
            line += " 자동검수 전"
        if stage == "drafts" and not compact:
            lines += ["", line] + _draft_summary(path, draft, review)
            attachments.append(review_doc(path))
            continue
        if review:
            issues = _issues(review)
            if issues:
                ko = _korean(path)
                first = ko["findings"][0] if ko and ko["findings"] else issues[0]["message"]
                line += f" {first}" + (f" 외 {len(issues) - 1}건" if len(issues) > 1 else "")
        lines.append(line)
        preview = path / "carousel" / "preview.jpg"
        if stage == "rendered" and preview.exists():  # 사진 넘기기형: 슬라이드 전체를 한 장으로 미리 보기
            attachments.append(preview)
    # Hermes 가 MEDIA: 줄을 텔레그램 첨부 파일로 보낸다 (경로에 공백이 있어 백틱으로 감싼다)
    lines += [""] + [f"MEDIA:`{doc}`" for doc in attachments] if attachments else []
    return "\n".join(lines)


# ---------------- 이동·기록 ----------------

def sponsor_confirmed(path: Path) -> bool:
    """스폰서 글이 아니면 True. 스폰서 글은 현재 내용으로 병원 확인이 기록돼 있어야 True (재생성되면 무효)."""
    if not load_draft(path).get("sponsor"):
        return True
    record = path / "sponsor_approval.json"
    return record.exists() and load_json(record).get("draft_hash") == draft_hash(path)


def move(draft_id: str, src: str, dst: str) -> Path:
    return move_draft(draft_id, src, dst, ALLOWED_MOVES)


def record_agreement(draft_id: str, auto_grade: str | None, decision: str) -> None:
    """신뢰 단계 판단용: 자동 등급과 사람 판단 일치 여부 (기획서 6-0)."""
    agree = (auto_grade == "pass") == (decision == "approve")
    path = log_dir() / "review_agreement.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "draft_id", "auto_grade", "human_decision", "agree"])
        w.writerow([date.today().isoformat(), draft_id, auto_grade or "", decision, int(agree)])


def _targets(cmd: Command, batch: list[str]) -> list[str]:
    if not cmd.numbers:
        return list(batch)
    bad = [n for n in cmd.numbers if not 1 <= n <= len(batch)]
    if bad:
        raise ReplyError(f"없는 번호: {bad} (1~{len(batch)})")
    return [batch[n - 1] for n in cmd.numbers]


def apply(reply: str, stage: str = "drafts", confirm: bool = False) -> list[str]:
    """답장을 처리하고 텔레그램으로 돌려줄 결과 줄들을 반환한다."""
    batch = load_batch(stage)
    codes = {code(d).upper(): str(i) for i, d in enumerate(batch, 1)}
    reply = re.sub(r"(?i)\bW\d{2}-\d{2}\b", lambda m: codes.get(m.group(0).upper(), m.group(0)), reply)
    commands = parse_reply(reply)
    # 전부 검증한 뒤에 실행한다 (한 줄이 틀려 일부만 처리되는 일이 없도록)
    stage_of = {"approve": {"drafts"}, "revise": {"drafts"}, "reject": {"drafts", "rendered", "approved"}, "publish_ok": {"rendered"},
                "sponsor_ok": {"drafts"}, "reopen": {"approved", "rendered"}}
    hint = {"approve": "렌더링 영상은 `게시 OK` 로 답해주세요",
            "revise": "렌더링 영상 수정(재렌더링)은 아직 미구현입니다",
            "publish_ok": "`게시 OK` 는 렌더링 영상 확인(--stage rendered) 단계에서만 씁니다"}
    plan = []
    for cmd in commands:
        if stage not in stage_of[cmd.action]:
            raise ReplyError(f"{stage} 단계에서 쓸 수 없는 명령: {cmd.action} — {hint.get(cmd.action, '')}")
        plan += [(cmd, draft_id) for draft_id in _targets(cmd, batch)]

    out: list[str] = []
    for cmd, draft_id in plan:
        path = content_dir(stage) / draft_id
        if not path.exists():
            out.append(f"- {draft_id}: 이미 처리됨")
            continue
        grade = (load_review(path) or {}).get("grade")
        if cmd.action == "sponsor_ok":
            if not load_draft(path).get("sponsor"):
                out.append(f"- {draft_id}: 스폰서 글이 아닙니다 (병원 확인 불필요)")
                continue
            save_json(path / "sponsor_approval.json", {"at": now_iso(), "draft_hash": draft_hash(path)})
            out.append(f"- {draft_id}: 병원 확인 기록 — 이제 승인할 수 있습니다")
            continue
        if cmd.action == "approve" and not sponsor_confirmed(path):
            out.append(f"- {draft_id}: 💼 병원 확인 전 — 병원에 최종본을 보내 확인받은 뒤 `N 병원확인` 을 먼저 보내주세요")
            continue
        if cmd.action == "approve":
            if grade == "pending" and not confirm:  # 번호로 골라도 Claude Code 검수 전에는 보류
                out.append(f"- {draft_id}: ⏳ Claude Code 검수 대기 — 결과 반영 후 다시 승인하거나 `--confirm`")
                continue
            if grade == "block" and not confirm:  # 번호로 골라도 차단(⛔)은 한 번 더 확인
                out.append(f"- {draft_id}: ⛔ 자동검수 차단 — 걸린 내용을 확인했고 그래도 승인하려면 '재확인 승인'(--confirm), "
                           "고치려면 `N 수정: …`")
                continue
            if not cmd.numbers and grade != "pass" and not confirm:
                out.append(f"- {draft_id}: {GRADE_ICON[grade]} 자동검수 {grade or '미실시'} — 번호로 따로 승인하거나 `전체 승인` 재확인 필요")
                continue
            move(draft_id, "drafts", "approved")
            record_agreement(draft_id, grade, "approve")
            out.append(f"- {draft_id}: 승인 → approved")
        elif cmd.action == "reject":
            move(draft_id, stage, "rejected")
            if stage == "drafts":
                record_agreement(draft_id, grade, "reject")
            out.append(f"- {draft_id}: 폐기 → rejected")
        elif cmd.action == "revise":
            # 재생성은 오래 걸리므로 워커가 처리한다 (재생성 → 자동 검수 → Claude 검수 → 검수 요청 재전송)
            record_agreement(draft_id, grade, "revise")
            importlib.import_module("pipeline.worker").enqueue("revise", draft_id=draft_id, note=cmd.note)
            out.append(f"- {draft_id}: 수정 요청 접수 — 재생성·검수 후 다시 보내드립니다")
        elif cmd.action == "reopen":
            live = importlib.import_module("pipeline.social").live_slugs()
            if draft_id in live:
                out.append(f"- {draft_id}: 이미 블로그에 게시된 글이라 되돌릴 수 없습니다")
                continue
            move(draft_id, stage, "drafts")  # 게시 일정 자리는 그대로 (다시 승인하면 같은 날)
            if cmd.note:
                importlib.import_module("pipeline.worker").enqueue("revise", draft_id=draft_id, note=cmd.note)
                out.append(f"- {draft_id}: 검수로 되돌림 + 수정 요청 접수 — 재생성·검수 후 다시 보내드립니다 (게시일 유지)")
            else:
                out.append(f"- {draft_id}: 검수로 되돌림 (게시일 유지) — `N 수정: …` 으로 고친 뒤 다시 승인해 주세요")
        elif cmd.action == "publish_ok":
            move(draft_id, "rendered", "ready_to_publish")
            out.append(f"- {draft_id}: 게시 OK → ready_to_publish")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="텔레그램 검수 답장 처리")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--stage", choices=["drafts", "rendered", "approved"], default="drafts")
    ap = sub.add_parser("apply")
    ap.add_argument("reply")
    ap.add_argument("--stage", choices=["drafts", "rendered", "approved"], default="drafts")
    ap.add_argument("--confirm", action="store_true", help="`전체 승인` 시 ⚠️/⛔ 건도 승인")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(list_message(args.stage))
        return 0
    try:
        lines = apply(args.reply, args.stage, args.confirm)
    except ReplyError as e:
        print(f"❓ {e}")
        return 2
    print("\n".join(lines) or "처리할 항목 없음")
    return 0


if __name__ == "__main__":
    run_cli("03_review", main)
