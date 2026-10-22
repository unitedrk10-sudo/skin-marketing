"""숏폼 실험 E — 코드로 그리는 설명 애니메이션 (2026-10-21).

Claude(stage explainer_plan)가 대본 줄마다 정해진 도식 종류(피부 단면·분자 사슬·회차 타임라인·붉은기 빠짐 등) 하나를 고르고
글자를 채운다 → 코드가 사실 목록 기준으로 검사(수치·금지 표현·병원명·길이) → Remotion(video/)이 가운데 도식을 렌더링 →
04_render_video.make_video 가 음성·자막을 얹는다. 배경은 크림색(E) 또는 B 실험의 Flow 클립(BE).

    python -m pipeline.experiment build <id>   # method E / BE
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from pipeline import llm
from pipeline.common import ROOT, get_logger, load_yaml, prompt

log = get_logger("explainer")

VIDEO_DIR = ROOT / "video"
KINDS = {"skin_layers", "molecule", "timeline", "big_number", "fade_patch", "myth_fact", "checklist", "icon_scene"}
ICONS = {"suitcase", "calendar", "sun", "bed", "doctor_note", "drop", "logo"}
DRAWN_KINDS = {"timeline", "big_number", "checklist", "myth_fact"}  # 섞기(M): 숫자·목록은 정확히 — 우리 도식이 그린다
TEXT_FIELDS = ("label", "note", "interval", "value", "unit", "belief", "fact", "from_label", "to_label")
LIMITS = {"label": 32, "note": 48}
MAX_TEXT = 60


class ExplainerError(RuntimeError):
    pass


def _lines(draft: dict) -> list[dict]:
    return [ln for ln in (draft.get("shortform") or {}).get("lines", []) if (ln.get("voice") or "").strip()]


def plan(draft: dict) -> list[dict]:
    """줄마다 도식 하나 (Claude). 줄 번호 순서로 정리해 돌려준다."""
    lines = _lines(draft)
    script = "\n".join(f"{i}. {ln['voice']}" for i, ln in enumerate(lines, 1))
    facts = importlib.import_module("pipeline.social").social_facts(draft)
    from pipeline.common import facts_text
    data, _ = llm.generate_json("explainer_plan", prompt("explainer_plan", title=draft["blog"].get("title", ""),
                                                         script=script, facts=facts_text(facts),
                                                         guide=importlib.import_module("pipeline.shorts").guide_text([draft])))
    beats = {int(b.get("line", 0)): b for b in (data or {}).get("beats") or [] if isinstance(b, dict)}
    return [beats.get(i) or {"line": i, "kind": "icon_scene", "icon": "drop"} for i in range(1, len(lines) + 1)]


def check(draft: dict, beats: list[dict]) -> tuple[list[dict], list[str]]:
    """사실 목록 기준 검사. 걸린 도식은 글자 없는 아이콘으로 바꾸고 이유를 남긴다 (화면에 근거 없는 글자가 나가지 않게)."""
    social = importlib.import_module("pipeline.social")
    lines = _lines(draft)
    known = social.known_numbers_of(draft)
    fact_ids = {f["id"] for f in draft.get("facts", [])}
    clean, problems = [], []
    for b in beats:
        n = int(b.get("line", 0))
        line_numbers = set(social.NUMBER.findall(lines[n - 1]["voice"])) if 1 <= n <= len(lines) else set()
        issues = []
        if b.get("kind") not in KINDS:
            issues.append(f"모르는 종류 {b.get('kind')}")
        texts = [str(b.get(k) or "") for k in TEXT_FIELDS] + [str(i) for i in b.get("items") or []]
        if b.get("kind") == "timeline":
            texts.append(str(b.get("count") or ""))
        for k, limit in LIMITS.items():
            if len(str(b.get(k) or "")) > limit:
                issues.append(f"{k} 너무 김")
        for t in texts:
            if len(t) > MAX_TEXT:
                issues.append(f"글자 너무 김: {t[:30]}…")
            issues += social.text_problems(t, known | line_numbers)
        if [i for i in b.get("fact_ids") or [] if i not in fact_ids]:
            issues.append("없는 사실 번호")
        if issues:
            problems.append(f"{n}번 줄: " + "; ".join(issues))
            b = {"line": n, "kind": "icon_scene", "icon": "drop", "label": "", "note": ""}
        if b.get("kind") == "icon_scene" and b.get("icon") not in ICONS:
            b["icon"] = "drop"
        clean.append(b)
    return clean, problems


def sources(draft: dict, beat: dict) -> str:
    """의료 정보 도식 아래 출처 도메인 (블로그와 같은 원칙 — 사실마다 출처)."""
    urls = {f["id"]: f.get("url", "") for f in draft.get("facts", []) if f.get("kind") != "travel"}
    hosts = []
    for i in beat.get("fact_ids") or []:
        host = urlparse(urls.get(i, "")).netloc.removeprefix("www.")
        if host and host not in hosts:
            hosts.append(host)
    return ("Source: " + " · ".join(hosts[:2])) if hosts else ""


def props(draft: dict, beats: list[dict], timeline: list[dict], total: float, background: list[dict]) -> dict:
    starts = [t["start"] for t in timeline] + [total]
    out = []
    for b in beats:
        n = int(b["line"])
        if not 1 <= n <= len(timeline):
            continue
        out.append({**{k: v for k, v in b.items() if k not in ("line", "fact_ids")},
                    "start": starts[n - 1], "end": starts[n], "source": sources(draft, b)})
    site = load_yaml("site.yaml")
    sf = draft.get("shortform") or {}
    return {"seconds": total, "beats": out, "background": background,
            "lines": [{"start": t["start"], "end": t["end"], "voice": t["voice"]} for t in timeline],
            "handle": site.get("domain") or site.get("name", ""),
            "disclosure": sf.get("on_screen_disclosure") or "AI-generated content"}


def npx() -> str:
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        raise ExplainerError("Node.js(npx)가 없습니다 — https://nodejs.org 설치 후 video/ 에서 npm install")
    if not (VIDEO_DIR / "node_modules" / "remotion").exists():
        raise ExplainerError("video/node_modules 가 없습니다 — video 폴더에서 npm install")
    return exe


CHROME_PATHS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome")


def browser() -> str | None:
    """설치된 Chrome 을 쓴다 — Remotion 이 자기 브라우저를 내려받다 멈춘 적이 있다 (2026-10-21, 30분 시간 초과)."""
    import os
    for c in (os.environ.get("REMOTION_BROWSER", ""), *CHROME_PATHS):
        if c and Path(c).exists():
            return c
    return None


def render(data: dict, out: Path) -> Path:
    """Remotion 으로 가운데 도식(+배경) 영상을 만든다 (소리 없음)."""
    exe = npx()
    props_file = out.with_suffix(".props.json")
    props_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cmd = [exe, "remotion", "render", "src/index.ts", "Explainer", str(out), f"--props={props_file}", "--log=error"]
    if chrome := browser():
        cmd.append(f"--browser-executable={chrome}")
    proc = subprocess.run(cmd, cwd=VIDEO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=1800, check=False)
    if proc.returncode != 0 or not out.exists():
        raise ExplainerError(f"Remotion 렌더 실패: {(proc.stderr or proc.stdout)[-500:]}")
    return out


def public_clips(eid: str, segments: list[tuple]) -> list[dict]:
    """B 클립을 Remotion public/ 으로 복사하고 배경 목록으로 (Remotion 은 public 폴더 파일만 읽는다)."""
    folder = VIDEO_DIR / "public" / "exp" / eid
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for seg in segments:
        clip, dur = seg[0], seg[1]
        if clip is None:  # 화면 없는 구간 (섞기: 우리 도식이 그림)
            out.append({"file": "", "seconds": dur, "from": 0, "zoom": 1.0, "rate": 1.0})
            continue
        speed = seg[2] if len(seg) > 2 else 1.0
        start = seg[3] if len(seg) > 3 else 0.3
        zoom = seg[4] if len(seg) > 4 else 1.0
        target = folder / clip.name
        if not target.exists():
            shutil.copy2(clip, target)
        out.append({"file": f"exp/{eid}/{clip.name}", "seconds": dur, "from": start, "zoom": zoom, "rate": speed})
    return out


def segments_fn(draft: dict, eid: str, background_fn=None, report: dict | None = None, overlay: str = "card"):
    """overlay: card = 도식 카드(E·BE), caption = 배경 영상이 주인공이고 제목·설명만 아래에 (Flow 3D 원리 애니메이션)."""
    """make_video 에 넘길 화면 함수 — 도식 영상 하나를 전체 길이로."""
    def fn(lines, timeline, total, work):
        saved = work.parent / "explainer.json"  # render/ 는 완성 후 지워진다 — 실험 폴더에 둔다. 화면만 고쳐 다시 만들 때·BE 를 E 와 같은 도식으로 비교할 때 계획을 다시 쓰지 않는다
        if saved.exists():
            beats, problems = check(draft, json.loads(saved.read_text(encoding="utf-8"))["beats"])
        else:
            beats, problems = check(draft, plan(draft))
        background = []
        if background_fn:
            bg_segments, _ = background_fn(lines, timeline, total, work)
            background = public_clips(eid, bg_segments)
        data = {**props(draft, beats, timeline, total, background), "overlay": overlay}
        saved.write_text(json.dumps({"beats": beats, "problems": problems}, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        if report is not None:
            report.update(beats=len(beats), problems=problems)
        out = render(data, work / "explainer.mp4")
        return [(out, total, 1.0, 0.0, 1.0)], [{"file": out.name, "beats": len(beats), "problems": problems}]
    return fn
