"""숏폼 화면 소스 실험 (2026-10-19) — 같은 글의 검증된 대본·음성·자막에 화면 소스만 바꿔 비교한다.

  A: 무료 영상 자동 (Pixabay + Pexels, Claude 가 고름) — 기준선, 바로 완성
  B: Google Flow 직접 — 장면 프롬프트를 텔레그램으로 보내면 사람이 Flow(Veo 3.1 Lite)로 만들어 clips 폴더에 넣는다
  C: Google Flow + Aside 루틴 — B 와 같은 프롬프트, 사람이 Flow 전용 계정에서 Aside 를 돌려 clips 폴더에 넣는다
     (Flow 웹 자동 조작은 우리 시스템에 넣지 않는다 — 약관·계정 위험은 사람이 판단하고 직접 운영)

일정 config/experiments.yaml. 워커가 그날 실험을 시작하고(send_hour 이후 한 번), clips 폴더가 차면 자동으로 편집해
완성 영상을 텔레그램으로 보낸다. 점수는 '실험 점수 4 4 3 예' → score. 게시는 하지 않는다 (숏폼 중단 그대로).

    python -m pipeline.experiment today            # 오늘 실험 시작 (안내 메시지)
    python -m pipeline.experiment build <id>       # clips 폴더 영상으로 지금 편집
    python -m pipeline.experiment score <id> 4 4 3 y
    python -m pipeline.experiment summary
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import llm
from pipeline.common import (
    content_dir,
    get_logger,
    load_draft,
    load_json,
    load_yaml,
    now_iso,
    prompt,
    run_cli,
    save_json,
)

log = get_logger("experiment")

KST = timezone(timedelta(hours=9))
PUBLIC_STATES = ("approved", "rendered", "ready_to_publish", "published")
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
METHOD_LABEL = {"A": "무료 영상 자동", "B": "Flow 직접 생성", "C": "Flow + Aside",
                "E": "코드로 그리는 설명 애니메이션", "BE": "Flow 배경 + 설명 애니메이션",
                "M": "섞기 — 원리·분위기는 Flow, 숫자·목록은 우리 도식"}  # E·BE 2026-10-21 (pipeline/explainer.py)
MIN_SPEED = 0.6  # 생성 영상(8초)을 장면 길이에 맞춰 이 배속까지만 느리게, 더 길면 반복
CLIP_SEC = 8.0     # Flow(Veo) 한 클립 길이
HEAD_SEC = 0.3     # 클립 첫 부분은 건너뛴다 (생성 영상 시작 프레임이 어색할 때가 많다)
TRANSITION = 0.4   # 컷 사이 겹쳐 넘기기(초) — 2026-10-22 "연결이 자연스럽지 않다"
MAX_SHOT = CLIP_SEC - HEAD_SEC - TRANSITION - 0.2  # 한 장면이 맡을 수 있는 최대 길이 — 넘으면 장면을 나눈다 (느리게 틀지 않음)
SHOT_MAX = 3.2   # 한 컷 최대 길이(초) — 넘으면 같은 영상의 다른 구간으로 나눈다
ZOOMS = (1.0, 1.18)  # 같은 영상에서 이어지는 컷은 확대 비율을 번갈아 (점프컷 완화, 고정 확대라 떨림 없음)


class ExperimentError(RuntimeError):
    pass


def config() -> dict:
    return load_yaml("experiments.yaml")


def root() -> Path:
    return content_dir() / "experiments"


def find_post(code: str) -> Path:
    """글번호(W41-10) → 글 폴더."""
    for state in PUBLIC_STATES:
        for p in sorted(content_dir(state).glob(f"*-{code}-*")):
            if (p / "draft.json").exists():
                return p
    raise ExperimentError(f"글을 찾지 못함: {code}")


def state(eid: str) -> dict:
    f = root() / eid / "experiment.json"
    return load_json(f) if f.exists() else {}


def _save(eid: str, data: dict) -> None:
    save_json(root() / eid / "experiment.json", data)


# ---------------- 장면 프롬프트 (B·C) ----------------

def plan_scenes(spans: list[float]) -> list[dict]:
    """음성 길이로 장면을 미리 나눈다 (2026-10-22). 한 줄 = 한 장면, 줄이 MAX_SHOT 보다 길면 같은 줄을 여러 장면으로
    (part 1/2 …) — 생성 영상 8초를 느리게 늘리거나 반복하지 않게, 필요한 만큼 클립을 만든다."""
    plan = []
    for n, span in enumerate(spans, 1):
        parts = max(1, math.ceil(span / MAX_SHOT))
        for k in range(1, parts + 1):
            plan.append({"lines": [n], "part": k, "parts": parts, "seconds": round(span / parts, 2)})
    return plan


def scene_prompts(draft: dict, plan: list[dict] | int, style: str, guide: str = "") -> list[dict]:
    """장면 계획(plan_scenes)대로 Flow 프롬프트를 쓴다. 숫자를 넘기면 예전처럼 Claude 가 n 장면으로 나눈다."""
    lines = [ln for ln in (draft.get("shortform") or {}).get("lines", []) if (ln.get("voice") or "").strip()]
    script = "\n".join(f"{i}. {ln['voice']}" for i, ln in enumerate(lines, 1))
    fixed = isinstance(plan, list)
    n = len(plan) if fixed else int(plan)
    layout = ("\n".join(f"Scene {i}: line {p['lines'][0]}"
                         + (f" (part {p['part']} of {p['parts']} — continue the same subject: next moment or a new angle)"
                            if p["parts"] > 1 else "") + f", {p['seconds']:.1f} s on screen"
                         for i, p in enumerate(plan, 1)) if fixed else "")
    data, _ = llm.generate_json("scene_prompts", prompt("scene_prompts", title=draft["blog"].get("title", ""),
                                                        script=script, n=str(n), plan=layout or "(none — group lines yourself)",
                                                        look=guide.strip() or "(default look — see style below)"))
    scenes = (data or {}).get("scenes") or []
    if len(scenes) != n:
        raise ExperimentError(f"장면 {n}개가 아니라 {len(scenes)}개")
    out = []
    for i, s in enumerate(scenes, 1):
        base = plan[i - 1] if fixed else {"lines": s.get("lines", [])}
        out.append({"n": i, **base, "prompt": f"{s['prompt'].strip()} {style.strip()}"})
    return out


# ---------------- 화면 구성 ----------------

def stock_segments(lines, timeline, total, work):
    """A: 숏폼 렌더링과 같은 무료 영상 고르기 (Pixabay + Pexels)."""
    return importlib.import_module("pipeline.04_render_video").plan_segments(lines, timeline, total, work)


def clip_files(eid: str) -> list[Path]:
    folder = root() / eid / "clips"
    return sorted(p for p in folder.glob("*") if p.suffix.lower() in VIDEO_EXT) if folder.exists() else []


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}


def order_clips(clips: list[Path], scenes: list[dict]) -> list[Path]:
    """장면 순서로 정렬. 이름이 숫자로 시작하면 그 순서, 아니면 Flow 가 붙인 파일 이름(프롬프트 요약)과 장면 프롬프트의 단어 겹침으로 짝짓는다."""
    if not scenes or len(clips) != len(scenes) or all(re.match(r"\d+", c.stem) for c in clips):
        return sorted(clips, key=lambda c: (int(m.group()) if (m := re.match(r"\d+", c.stem)) else 10**6, c.name))
    pairs = sorted(((len(_words(c.stem.replace("_", " ")) & _words(s["prompt"])), -i, c, i)
                    for i, s in enumerate(scenes) for c in clips), key=lambda x: (x[0], x[1]), reverse=True)
    picked: dict[int, Path] = {}
    used: set[Path] = set()
    for _, _, c, i in pairs:
        if i not in picked and c not in used:
            picked[i] = c
            used.add(c)
    return [picked[i] for i in range(len(scenes))]


def fit_clips(clips: list[Path], scenes: list[dict]) -> tuple[list[Path | None], list[dict], list[Path]]:
    """받은 클립을 장면에 맞춘다 (2026-10-22). 이름이 숫자로 시작하면 order_clips 그대로.
    아니면 파일 이름 ↔ 프롬프트(공통 스타일 문구 제외) 단어가 2개 이상 겹치는 클립을 장면마다 하나씩 고르고
    (같은 장면 여러 버전이면 먼저 만든 것). 클립이 없는 장면은 None — 화면은 clip_segments 가 남는 클립·앞 장면으로 채운다."""
    if not scenes or all(re.match(r"\d+", c.stem) for c in clips):
        return order_clips(clips, scenes), scenes, []
    style = _words(config().get("style", ""))
    pairs = sorted(((len(_words(c.stem.replace("_", " ")) & (_words(s["prompt"]) - style)), -i, c.name, c, i)
                    for i, s in enumerate(scenes) for c in clips), key=lambda x: (-x[0], x[4], x[2]))
    picked: dict[int, Path] = {}
    used: set[Path] = set()
    for score, _, _, c, i in pairs:
        if score >= 2 and i not in picked and c not in used:
            picked[i] = c
            used.add(c)
    if not picked:
        raise ExperimentError("클립 파일 이름으로 장면을 찾지 못했습니다 — 앞에 1_, 2_… 를 붙여 주세요")
    log.info("장면 %d개 중 클립 %d개 — 빠진 장면: %s", len(scenes), len(picked),
             [s.get("n") for i, s in enumerate(scenes) if i not in picked])
    return [picked.get(i) for i in range(len(scenes))], scenes, [c for c in clips if c not in used]  # 남는 클립(같은 장면 다른 버전) = 긴 장면에 빌려 줄 화면


def clip_segments(clips: list[Path], scenes: list[dict] | None = None, spare: list[Path] | tuple = (),
                  fill_gaps: bool = False):
    """B·C: 장면 영상을 그 장면이 맡은 대본 줄에 이어서 쓴다 (2026-10-22 — 같은 화면 반복 제거).
    - 한 장면 = 한 줄이면 자르지 않고 한 컷으로 (8초 클립에서 줄 길이만큼).
    - 한 장면이 여러 줄이면 줄마다 컷을 나누되(확대 번갈아) 클립을 앞에서부터 이어서 쓴다 — 같은 구간을 두 번 쓰지 않는다.
    - 클립이 모자라면 MIN_SPEED 까지 느리게, 그래도 모자라면 남는 클립(spare)을 빌리고, 그것도 없을 때만 처음부터 다시.
    장면 정보가 없으면 예전처럼 같은 길이로 나눈다."""
    render = importlib.import_module("pipeline.04_render_video")

    def even(lines, timeline, total, work):
        span = total / len(clips)
        segments, meta = [], []
        for c in clips:
            length = max(render.duration(c) - 0.3, 0.5)
            speed = max(MIN_SPEED, min(1.0, length / span))
            segments.append((c, span, speed))
            meta.append({"file": c.name, "seconds": round(length, 2), "speed": round(speed, 2)})
        return segments, meta

    def per_line(lines, timeline, total, work):
        starts = [t["start"] for t in timeline] + [total]
        pool = list(spare)
        segments, meta = [], []
        prev, prev_used = None, 0.0
        for c, scene in zip(clips, scenes):
            nums = [n for n in scene.get("lines", []) if 1 <= n <= len(timeline)]
            if not nums:
                continue
            span = starts[max(nums)] - starts[min(nums) - 1]
            if scene.get("parts", 1) > 1:
                span /= scene["parts"]
            if fill_gaps:  # 장면이 맡지 않은 줄(우리 도식이 그리는 줄)은 화면 없이(None) 시간만 채운다
                begin = starts[min(nums) - 1] + (scene.get("part", 1) - 1) * span
                here = sum(s[1] for s in segments)
                if begin - here > 0.05:
                    segments.append((None, begin - here))
                    meta.append({"scene": None, "file": "", "seconds": round(begin - here, 2)})
            need = span + TRANSITION
            if c is None:  # 클립이 없는 장면 → 남는 클립 → 앞 장면 클립의 남은 부분 → (최후) 앞 장면 처음부터
                c, offset = (pool.pop(0), HEAD_SEC) if pool else (prev, prev_used)
                if c is None:
                    continue
                if offset + need > render.duration(c):
                    offset = HEAD_SEC
                    log.warning("장면 %s: 클립이 없어 앞 장면 화면을 다시 씁니다", scene.get("n"))
            else:
                offset = HEAD_SEC
            if offset + need > render.duration(c) + 0.05:
                log.warning("장면 %s: 클립(%.1f초)이 필요한 길이(%.1f초)보다 짧습니다", scene.get("n"), render.duration(c), need)
            segments.append((c, span, 1.0, offset, 1.0))
            meta.append({"scene": scene.get("n"), "file": c.name, "seconds": round(span, 2), "from": round(offset, 2)})
            prev, prev_used = c, offset + span
        covered = sum(s[1] for s in segments)
        if segments and covered < total:
            last = segments[-1]
            segments[-1] = (last[0], last[1] + total - covered, *last[2:])
        return segments, meta

    usable = scenes and len(scenes) == len(clips) and all(s.get("lines") for s in scenes)  # clips 는 장면 순서, 없는 장면은 None
    return per_line if usable else even


def build(eid: str) -> str:
    """완성 영상 만들기 → 텔레그램용 메시지 (MEDIA 첨부)."""
    data = state(eid)
    if not data:
        raise ExperimentError(f"실험 없음: {eid}")
    draft = load_draft(find_post(data["post"]))
    out = root() / eid
    if data["method"] == "A":
        fn = stock_segments
    elif data["method"] == "M":
        explainer = importlib.import_module("pipeline.explainer")
        clips = clip_files(eid)
        if not clips:
            raise ExperimentError(f"{out / 'clips'} 에 영상이 없습니다")
        fn = explainer.segments_fn(draft, eid, clip_segments(*fit_clips(clips, data.get("scenes") or []), fill_gaps=True),
                                   overlay="mix")
    elif data["method"] in ("E", "BE"):
        explainer = importlib.import_module("pipeline.explainer")
        background = None
        if data["method"] == "BE":  # 배경 = B 실험(data["bg"])의 Flow 클립, 줄마다 쪼갠 그대로
            bg = state(data.get("bg", ""))
            clips = clip_files(bg.get("id", "")) if bg else []
            if not clips:
                raise ExperimentError(f"배경으로 쓸 B 실험 클립이 없습니다: {data.get('bg')}")
            scenes = bg.get("scenes") or []
            background = clip_segments(*fit_clips(clips, scenes))
        fn = explainer.segments_fn(draft, eid, background, overlay=data.get("overlay", "card"))
    else:
        clips = clip_files(eid)
        if not clips:
            raise ExperimentError(f"{out / 'clips'} 에 영상이 없습니다")
        scenes = data.get("scenes") or []
        clips, scenes, spare = fit_clips(clips, scenes)
        fn = clip_segments(clips, scenes, spare)
    render = importlib.import_module("pipeline.04_render_video")
    meta = render.make_video(draft, out, fn, TRANSITION if data["method"] in ("B", "C") else 0.0,
                             captions=data["method"] not in ("E", "BE", "M"))  # E·BE 는 자막·브랜드를 도식과 같은 디자인으로 Remotion 이 그린다
    data.update(status="done", built_at=now_iso(), render=meta)
    _save(eid, data)
    return (f"[실험 완성 {eid}] {METHOD_LABEL[data['method']]} — {draft['blog'].get('title', '')} ({meta['seconds']:.0f}초)\n"
            f"점수 답장: `실험 점수 {eid} 자연스러움 내용일치 일관성 올릴만함` (예: `실험 점수 {eid} 4 4 3 예`)\n"
            f"MEDIA:`{out / 'video.mp4'}`")


def start(entry: dict) -> str:
    eid = exp_id(entry)
    cfg = config()
    draft_path = find_post(entry["post"])
    draft = load_draft(draft_path)
    data = {"id": eid, "date": entry["date"], "method": entry["method"], "post": entry["post"],
            "title": draft["blog"].get("title", ""), "started_at": now_iso(), "status": "started",
            **{k: entry[k] for k in ("bg", "look", "overlay", "tag") if entry.get(k)}}
    (root() / eid).mkdir(parents=True, exist_ok=True)
    _save(eid, data)
    if entry["method"] in ("A", "E", "BE"):  # 사람 손이 필요 없는 방식은 바로 완성
        return build(eid)
    if entry.get("scenes"):  # 예전 방식 — 장면 수를 정해 두고 Claude 가 줄을 나눔
        plan = int(entry["scenes"])
    else:  # 2026-10-22 기본 — 음성 길이를 먼저 재서 장면을 나눈다 (8초 클립을 느리게·반복하지 않게)
        render = importlib.import_module("pipeline.04_render_video")
        plan = plan_scenes(render.line_spans([s for _, s in render.voice_lines(draft, root() / eid / "voice_lines")]))
        if entry["method"] == "M":  # 도식 계획 먼저 → 숫자·목록 줄은 우리 도식이 맡고 Flow 장면은 나머지 줄만
            explainer = importlib.import_module("pipeline.explainer")
            beats, problems = explainer.check(draft, explainer.plan(draft))
            save_json(root() / eid / "explainer.json", {"beats": beats, "problems": problems})
            drawn = {b["line"] for b in beats if b.get("kind") in explainer.DRAWN_KINDS}
            plan = [p for p in plan if p["lines"][0] not in drawn]
    look = (cfg.get("looks") or {}).get(entry.get("look") or ("flatmotion" if entry["method"] == "M" else ""), {})
    scenes = scene_prompts(draft, plan, look.get("style") or cfg.get("style", ""), look.get("guide", ""))
    (root() / eid / "clips").mkdir(exist_ok=True)
    (root() / eid / "prompts.txt").write_text("\n\n".join(f"{s['n']}. {s['prompt']}" for s in scenes) + "\n", encoding="utf-8")
    data.update(status="waiting_clips", scenes=scenes)
    _save(eid, data)
    how = ("Google Flow(labs.google/flow) → 모델 **Veo 3.1 Lite**, 세로 9:16 → 아래 프롬프트를 하나씩 붙여 넣어 생성 → 내려받기"
           if entry["method"] == "B" else
           "Flow 전용 구글 계정·별도 프로필의 Aside 에서 루틴 실행: 아래 프롬프트를 Flow(Veo 3.1 Lite, 9:16)로 생성·내려받기")
    lines = [f"[오늘의 실험 {eid}] {METHOD_LABEL[entry['method']]} — {data['title']}", how,
             f"내려받은 영상을 이름 그대로 이 PC 폴더에 넣어 주세요 (순서는 프롬프트와 파일 이름으로 맞춤, 헷갈리면 앞에 1_, 2_…): {root() / eid / 'clips'}",
             "다 넣으면 자동으로 편집해 보내 드립니다 (바로 하려면 '실험 영상 넣었어').",
             f"장면 {len(scenes)}개 — 음성 길이 기준 장면마다 " + ", ".join(f"{s['n']}번 {s['seconds']:.1f}초" for s in scenes if s.get("seconds"))]
    if send_prompts(scenes):
        lines.append(f"프롬프트 {len(scenes)}개는 복사하기 쉽게 한 개씩 따로 보냈습니다 (1번부터 순서대로).")
    else:
        lines += [""] + [f"{s['n']}. {s['prompt']}" for s in scenes]
    return "\n".join(lines)


def send_prompts(scenes: list[dict]) -> bool:
    """장면 프롬프트를 텔레그램에 한 개씩 (2026-10-20 — Flow 에 하나씩 복사해 붙여 넣기 쉽게). 실패하면 한 번 더, 그래도 실패면 False."""
    worker = importlib.import_module("pipeline.worker")
    for s in scenes:
        text = f"#{s['n']} / {len(scenes)}\n{s['prompt']}"  # 번호는 첫 줄에만 — 둘째 줄부터 복사해 Flow 에 붙여 넣기 (2026-10-22)
        if not (worker.hermes_send(text) or worker.hermes_send(text)):
            log.warning("장면 %s 프롬프트 개별 전송 실패 — 묶음 메시지에 넣음", s["n"])
            return False
    return True


def exp_id(entry: dict) -> str:  # 같은 날 여러 실험 (2026-10-19 하루 몰아서) — 글번호까지, 같은 날 같은 글이면 tag 로 구분
    return f"{entry['date']}-{entry['method']}-{entry['post']}" + (f"-{entry['tag']}" if entry.get("tag") else "")


def today_entries(now: datetime) -> list[dict]:
    return [{**e, "date": str(e["date"])} for e in config().get("schedule") or [] if str(e["date"]) == now.date().isoformat()]


def daily_message(now: datetime | None = None) -> str:
    """send_hour 이후 — 오늘 일정 중 아직 시작하지 않은 실험을 모두 시작. 없으면 빈 문자열."""
    now = now or datetime.now(KST)
    if now.hour < int(config().get("send_hour", 10)):
        return ""
    out = []
    for entry in today_entries(now):
        if state(exp_id(entry)):
            continue
        try:
            out.append(start(entry))
        except (ExperimentError, llm.LLMError) as e:
            _save(exp_id(entry), {"id": exp_id(entry), **entry, "status": "failed", "error": str(e)[:300]})
            out.append(f"⚠️ 실험({exp_id(entry)}) 시작 실패: {e}")
        break  # 워커 한 번에 하나 — A 하나가 30~40분이라 여러 개를 한 번에 하면 Hermes 1시간 제한에 걸려 메시지가 사라진다
    return "\n\n".join(out)


def poll_message() -> str:
    """B·C: clips 폴더가 장면 수만큼 차면 편집해서 보낸다."""
    out = []
    default = int(config().get("scenes", 5))
    for f in sorted(root().glob("*/experiment.json")) if root().exists() else []:
        data = load_json(f)
        want = len(data.get("scenes") or []) or default
        if data.get("status") == "waiting_clips" and len(clip_files(data["id"])) >= want:
            try:
                out.append(build(data["id"]))
            except Exception as e:  # noqa: BLE001 — 한 실험 실패가 워커를 멈추지 않게
                data.update(status="build_failed", error=str(e)[:300])
                save_json(f, data)
                out.append(f"⚠️ 실험 {data['id']} 편집 실패: {e}")
            break  # 한 번에 하나 (daily_message 와 같은 이유)
    return "\n\n".join(out)


def latest_done() -> str | None:
    rows = [load_json(f) for f in sorted(root().glob("*/experiment.json"))] if root().exists() else []
    done = [d for d in rows if d.get("status") == "done"]
    return max(done, key=lambda d: d.get("built_at", ""))["id"] if done else None


def score(eid: str, natural: int, match: int, consistency: int, ok: str) -> str:
    if eid in ("최근", "latest", "오늘"):
        eid = latest_done() or ""
    data = state(eid)
    if not data:
        raise ExperimentError(f"실험 없음: {eid}")
    vals = [natural, match, consistency]
    if any(not 1 <= v <= 5 for v in vals):
        raise ExperimentError("점수는 1~5")
    data["score"] = {"natural": natural, "match": match, "consistency": consistency,
                     "ok": ok.strip().lower() in ("y", "yes", "예", "네", "o", "ok"), "at": now_iso()}
    _save(eid, data)
    return f"✅ {eid} 점수 기록: 자연스러움 {natural} · 내용 일치 {match} · 일관성 {consistency} · 올릴 만함 {data['score']['ok']}"


def summary() -> str:
    rows = [load_json(f) for f in sorted(root().glob("*/experiment.json"))] if root().exists() else []
    if not rows:
        return "실험 기록 없음"
    lines = ["[화면 소스 실험 정리]"]
    by = {}
    for d in rows:
        s = d.get("score")
        mark = (f"{s['natural']}/{s['match']}/{s['consistency']} {'올릴만함' if s['ok'] else '-'}" if s else d.get("status", ""))
        wait = ""
        if d.get("built_at") and d.get("method") != "A":
            minutes = (datetime.fromisoformat(d["built_at"]) - datetime.fromisoformat(d["started_at"])).total_seconds() / 60
            wait = f" · 프롬프트→완성 {math.ceil(minutes)}분"
        lines.append(f"- {d['id']} {METHOD_LABEL.get(d['method'], d['method'])} [{d['post']}]: {mark}{wait}")
        if s:
            by.setdefault(d["method"], []).append(s["natural"] + s["match"] + s["consistency"])
    if by:
        lines.append("평균(3항목 합, 15점 만점): " + " · ".join(f"{m} {sum(v) / len(v):.1f}" for m, v in sorted(by.items())))
    return "\n".join(lines)


def latest_waiting() -> str | None:
    rows = [load_json(f) for f in sorted(root().glob("*/experiment.json"))] if root().exists() else []
    waiting = [d["id"] for d in rows if d.get("status") == "waiting_clips"]
    return waiting[-1] if waiting else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="숏폼 화면 소스 실험")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("today")
    t.add_argument("--force", action="store_true", help="시각과 관계없이 지금 시작")
    b = sub.add_parser("build")
    b.add_argument("id", nargs="?", help="생략하면 영상을 기다리는 가장 최근 실험")
    s = sub.add_parser("score")
    s.add_argument("id")
    s.add_argument("natural", type=int)
    s.add_argument("match", type=int)
    s.add_argument("consistency", type=int)
    s.add_argument("ok")
    sub.add_parser("summary")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "today":
            now = datetime.now(KST)
            print(daily_message(now.replace(hour=23) if args.force else now) or "오늘 시작할 실험 없음 (이미 시작했거나 일정 없음)")
        elif args.cmd == "build":
            eid = args.id or latest_waiting()
            if not eid:
                raise ExperimentError("영상을 기다리는 실험이 없습니다")
            print(build(eid))
        elif args.cmd == "score":
            print(score(args.id, args.natural, args.match, args.consistency, args.ok))
        else:
            print(summary())
    except ExperimentError as e:
        print(f"❓ {e}")
        return 2
    return 0


if __name__ == "__main__":
    run_cli("experiment", main)
