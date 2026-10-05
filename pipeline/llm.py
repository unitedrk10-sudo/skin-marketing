"""LLM 호출 단일 진입점. 다른 모듈은 이 파일의 generate / generate_json 만 쓴다.

- 모델·공급자는 config/models.yaml 의 stages 에서 읽는다 (코드에 모델명 하드코딩 금지).
- 키는 환경변수(GEMINI_API_KEY, ANTHROPIC_API_KEY)에서만 읽고 로그에 남기지 않는다.
- 테스트에서는 set_backend() 로 실제 API 대신 가짜 응답 함수를 넣는다.

CLI:
    python -m pipeline.llm check          # 키·모델 설정 점검 + 단계별 1회 호출 테스트
    python -m pipeline.llm models         # Gemini 사용 가능 모델 목록
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from pipeline.common import get_logger, load_yaml, parse_json

log = get_logger("llm")

KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
# API 를 부르지 않고 검수 요청 파일로 내보내 Claude Code 세션이 처리하는 단계 (02c_external_review.py)
EXTERNAL = "claude_code"
RETRIES = 3
# 응답이 오지 않는 호출을 끊는다 (없으면 워커가 Hermes 1시간 제한까지 멈추고 잠금 때문에 다음 실행도 건너뜀).
# pro + 검색 연동은 2분 안팎 걸린다. 초과하면 재시도.
REQUEST_TIMEOUT_SEC = 300


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    stage: str
    grounding_urls: list[dict] = field(default_factory=list)  # Gemini 검색 연동 시 [{url,title}]


Backend = Callable[[str, dict, str | None, str], LLMResult]
_backend: Backend | None = None


def set_backend(fn: Backend | None) -> None:
    """테스트용: fn(stage, stage_cfg, system, prompt) -> LLMResult"""
    global _backend
    _backend = fn


def stage_config(stage: str) -> dict:
    stages = load_yaml("models.yaml").get("stages", {})
    if stage not in stages:
        raise LLMError(f"config/models.yaml 에 stage '{stage}' 없음")
    cfg = dict(stages[stage])
    if cfg.get("provider") not in (*KEY_ENV, EXTERNAL):
        raise LLMError(f"stage '{stage}': 지원하지 않는 provider {cfg.get('provider')!r}")
    return cfg


def is_external(stage: str) -> bool:
    return stage_config(stage)["provider"] == EXTERNAL


def _api_key(provider: str) -> str:
    key = os.environ.get(KEY_ENV[provider], "")
    if not key:
        raise LLMError(f"{KEY_ENV[provider]} 환경변수가 없습니다.")
    return key


# ---- Gemini (google-genai) ----

def _gemini(stage: str, cfg: dict, system: str | None, prompt: str) -> LLMResult:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_api_key("gemini"),
                          http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SEC * 1000))  # ms
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=cfg.get("temperature"),
        tools=[types.Tool(google_search=types.GoogleSearch())] if cfg.get("search") else None,
    )
    resp = client.models.generate_content(model=cfg["model"], contents=prompt, config=config)
    text = resp.text or ""
    if not text:
        reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
        raise LLMError(f"Gemini 빈 응답 ({reason})")
    urls = []
    for cand in resp.candidates or []:
        meta = cand.grounding_metadata
        for chunk in (meta.grounding_chunks if meta and meta.grounding_chunks else []):
            if chunk.web and chunk.web.uri:
                urls.append({"url": chunk.web.uri, "title": chunk.web.title or ""})
    return LLMResult(text=text, model=cfg["model"], stage=stage, grounding_urls=urls)


# ---- Claude (anthropic) ----

def _anthropic(stage: str, cfg: dict, system: str | None, prompt: str) -> LLMResult:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key("anthropic"), timeout=REQUEST_TIMEOUT_SEC)
    kwargs: dict = {
        "model": cfg["model"],
        "max_tokens": cfg.get("max_tokens", 16000),
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if cfg.get("effort"):
        kwargs["output_config"] = {"effort": cfg["effort"]}
    if cfg.get("fallbacks", True) and not cfg["model"].startswith("claude-haiku"):
        # 안전 분류기 거절 시 서버 측에서 권장 모델로 재시도
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
        resp = client.beta.messages.create(**kwargs)
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise LLMError(f"Claude 거절 (stage={stage})")
    if resp.stop_reason == "max_tokens":
        raise LLMError(f"Claude 응답이 max_tokens 에서 잘림 (stage={stage})")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return LLMResult(text=text, model=resp.model, stage=stage)


PROVIDERS = {"gemini": _gemini, "anthropic": _anthropic}


def generate(stage: str, prompt: str, system: str | None = None) -> LLMResult:
    cfg = stage_config(stage)
    if cfg["provider"] == EXTERNAL:
        raise LLMError(f"stage '{stage}' 는 Claude Code 외부 검수 단계라 API 로 호출하지 않습니다")
    call = _backend or PROVIDERS[cfg["provider"]]
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            started = time.monotonic()
            result = call(stage, cfg, system, prompt)
            log.info("stage=%s model=%s %.1fs", stage, result.model, time.monotonic() - started)
            return result
        except LLMError:
            raise  # 설정·키 오류, 거절 등은 재시도해도 같다
        except Exception as e:  # noqa: BLE001 — 네트워크·5xx·429
            last = e
            log.warning("stage=%s 호출 실패 (%d/%d): %s", stage, attempt + 1, RETRIES, type(e).__name__)
            if attempt + 1 < RETRIES:
                time.sleep(2 ** (attempt + 1))
    raise LLMError(f"stage={stage} 호출 실패: {last}")


def generate_json(stage: str, prompt: str, system: str | None = None):
    """JSON 응답을 파싱해 (객체, LLMResult) 로 돌려준다. 파싱 실패 시 1회 재요청."""
    result = generate(stage, prompt, system)
    try:
        return parse_json(result.text), result
    except ValueError:
        log.warning("stage=%s JSON 파싱 실패, 재요청", stage)
        result = generate(stage, prompt + "\n\nIMPORTANT: respond with valid JSON only.", system)
        try:
            return parse_json(result.text), result
        except ValueError as e:
            raise LLMError(f"stage={stage} JSON 파싱 실패: {e}") from e


# ---- CLI ----

def _check() -> int:
    stages = load_yaml("models.yaml").get("stages", {})
    ok = True
    for provider, env in KEY_ENV.items():
        used = [s for s, c in stages.items() if c.get("provider") == provider]
        if used:
            present = bool(os.environ.get(env))
            ok &= present
            print(f"{env}: {'OK' if present else '없음'} (사용 단계: {', '.join(used)})")
    external = [s for s, c in stages.items() if c.get("provider") == EXTERNAL]
    if external:
        print(f"Claude Code 외부 검수: {', '.join(external)} (02c_external_review export/import)")
    if not ok:
        return 1
    for stage in (s for s in stages if s not in external):
        try:
            r = generate(stage, 'Reply with exactly: {"ok": true}')
            print(f"{stage}: OK ({r.model})")
        except LLMError as e:
            ok = False
            print(f"{stage}: 실패 — {e}")
    return 0 if ok else 1


def _models() -> int:
    from google import genai

    client = genai.Client(api_key=_api_key("gemini"))
    for m in client.models.list():
        if "generateContent" in (m.supported_actions or []):
            print(m.name.removeprefix("models/"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 설정 점검")
    parser.add_argument("cmd", choices=["check", "models"])
    args = parser.parse_args(argv)
    try:
        return _check() if args.cmd == "check" else _models()
    except LLMError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
