"""LLM 호출 단일 진입점. 다른 모듈은 이 파일의 generate / generate_json 만 쓴다.

- 모델·공급자는 config/models.yaml 의 stages 에서 읽는다 (코드에 모델명 하드코딩 금지).
- 키는 환경변수(GEMINI_API_KEY, ANTHROPIC_API_KEY)에서만 읽고 로그에 남기지 않는다.
- provider: claude_cli 는 같은 머신의 Claude Code CLI(claude -p)를 부른다 — 초안 작성 (Pro 로그인, 키 불필요).
- 테스트에서는 set_backend() 로 실제 API 대신 가짜 응답 함수를 넣는다.

CLI:
    python -m pipeline.llm check          # 키·모델 설정 점검 + 단계별 1회 호출 테스트
    python -m pipeline.llm models         # Gemini 사용 가능 모델 목록
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from pipeline.common import get_logger, load_yaml, parse_json

log = get_logger("llm")

KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
# API 를 부르지 않고 검수 요청 파일로 내보내 Claude Code 세션이 처리하는 단계 (02c_external_review.py)
EXTERNAL = "claude_code"
# 같은 머신의 Claude Code CLI(claude -p, Pro 로그인)로 바로 호출하는 단계 — 초안 작성 (API 키 불필요)
CLAUDE_CLI = "claude_cli"
RETRIES = 3
# 응답이 오지 않는 호출을 끊는다 (없으면 워커가 Hermes 1시간 제한까지 멈추고 잠금 때문에 다음 실행도 건너뜀).
# pro + 검색 연동은 2분 안팎 걸린다. 초과하면 재시도.
REQUEST_TIMEOUT_SEC = 300


class LLMError(RuntimeError):
    pass


class RateLimited(LLMError):
    """사용량 한도 — 같은 요청을 나중에 다시 하면 된다 (워커는 요청을 버리지 않는다)."""


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
    if cfg.get("provider") not in (*KEY_ENV, EXTERNAL, CLAUDE_CLI):
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
        # 함수 호출은 쓰지 않는다 — 켜 두면 매 호출 stderr 에 AFC 경고가 찍혀 Hermes 실패 알림 첫 줄을 가린다
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
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


# ---- Claude Code CLI (claude -p) ----

LIMIT_WORDS = re.compile(r"usage limit|session limit|weekly limit|rate limit|limit reached|limit will reset|resets \d|"
                         r"hit your .{0,20}limit|overloaded|too many requests", re.I)


def _claude_cli(stage: str, cfg: dict, system: str | None, prompt: str) -> LLMResult:
    """프롬프트는 stdin 으로. 도구는 cfg.tools (기본: 없음). 저장소 밖 빈 폴더에서 실행해 CLAUDE.md·MCP 를 읽지 않는다."""
    cmd = [os.environ.get("CLAUDE_BIN", "claude"), "-p", "--output-format", "json", "--strict-mcp-config",
           "--no-session-persistence", "--tools", ",".join(cfg.get("tools") or [])]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if system:
        cmd += ["--append-system-prompt", system]
    timeout = cfg.get("timeout_sec", 1800)
    with tempfile.TemporaryDirectory(prefix="skin-claude-") as workdir:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", cwd=workdir, timeout=timeout)
        except FileNotFoundError as e:
            raise LLMError(f"claude 명령을 찾을 수 없습니다 (CLAUDE_BIN): {e}") from e
        except subprocess.TimeoutExpired as e:  # 재시도하면 Hermes 1시간 제한을 넘긴다 → 다음 워커 실행에 맡김
            raise RateLimited(f"claude -p {timeout}s 초과 — 다음 실행 때 재시도") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {"is_error": True, "result": (proc.stdout or proc.stderr or "").strip()[-500:]}
    text = str(out.get("result") or "")
    if out.get("is_error") or proc.returncode != 0:
        detail = text or (proc.stderr or "").strip()[-500:] or f"exit {proc.returncode}"
        if LIMIT_WORDS.search(detail):
            raise RateLimited(f"Claude 사용량 한도: {detail[:200]}")
        raise LLMError(f"claude -p 실패 (stage={stage}): {detail[:300]}")
    # Claude Code 는 보조 작업에 다른 모델(Haiku)도 쓴다 — 출력을 가장 많이 만든 모델이 실제 작성 모델
    usage = out.get("modelUsage") or {}
    model = max(usage, key=lambda m: (usage[m] or {}).get("outputTokens", 0)) if usage else (cfg.get("model") or "claude")
    return LLMResult(text=text, model=model, stage=stage)


PROVIDERS = {"gemini": _gemini, "anthropic": _anthropic, CLAUDE_CLI: _claude_cli}


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
    cli = [s for s, c in stages.items() if c.get("provider") == CLAUDE_CLI]
    if cli:
        print(f"Claude Code CLI (claude -p): {', '.join(cli)}")
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
