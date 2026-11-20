"""운영자가 한국어로 쓴 Reddit 답변을 Google 번역(Cloud Translation API v2)으로 영어로 옮긴다 (2026-11-02, docs/reddit-ops.md).

LLM 이 다시 쓰는 것이 아니라 기계 번역이다 — 운영자 문장을 옮기기만 하고 내용을 보태지 않는다. 그래도 커뮤니티에 따라 기계 번역도
AI 글로 볼 수 있어, reddit.py 는 AI 글이 허용된 곳이나 번역 허용 기록(`번역허용`)이 있는 곳에서만 쓰고, 번역했다는 줄을 공개 문구에 붙인다.
키: 환경변수 GOOGLE_TRANSLATE_API_KEY (요청 헤더로만 보낸다 — 주소·로그·저장소에 남기지 않는다).
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.request

URL = "https://translation.googleapis.com/language/translate/v2"
HANGUL = re.compile(r"[가-힣]")
LETTER = re.compile(r"[A-Za-z가-힣]")
_translator = None


class TranslateError(RuntimeError):
    pass


def set_translator(fn=None) -> None:
    """테스트용: 번역 함수를 바꾼다 (None 이면 실제 API)."""
    global _translator
    _translator = fn


def is_korean(text: str) -> bool:
    letters = LETTER.findall(text)
    return bool(letters) and len(HANGUL.findall(text)) / len(letters) > 0.3


def to_english(text: str) -> str:
    if _translator:
        return _translator(text)
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not key:
        raise TranslateError("GOOGLE_TRANSLATE_API_KEY 가 없습니다")
    body = json.dumps({"q": text, "source": "ko", "target": "en", "format": "text"}).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "X-Goog-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001 — 키가 오류 메시지에 섞이지 않게 종류만
        raise TranslateError(f"Google 번역 실패 ({type(e).__name__})") from None
    try:
        return html.unescape(data["data"]["translations"][0]["translatedText"]).strip()
    except (KeyError, IndexError, TypeError):
        raise TranslateError("Google 번역 응답 형식이 다릅니다") from None
