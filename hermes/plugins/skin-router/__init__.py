"""텔레그램 메시지를 대화 에이전트 대신 pipeline.telegram_router 로 넘긴다 (2026-11-01).

게이트웨이를 막지 않도록 큐 + 스레드 하나로 순서대로 처리한다. 허용되지 않은 사용자의 메시지는 건드리지 않는다.
메시지가 온 방(telegram:<chat_id>[:<thread_id>])을 SKIN_TG_ORIGIN 으로 넘겨 답장이 그 방으로 가게 한다 (2026-11-02, Reddit 전용 방).
방 정보를 못 찾으면 비워 두고, 그러면 라우터는 예전처럼 기본 방으로 답한다.
/new 같은 Hermes 내장 명령만 그대로 통과. 문제가 생겨도 에이전트로 넘기지 않는다 (글을 지어내 게시한 사고 방지).
"""

import logging
import os
import queue
import subprocess
import threading
import time

INPUT_DELAY = 4  # 초: 마지막 메시지 뒤 이만큼 기다렸다 처리
MAX_WAIT = 20
REPO = r"{{REPO}}"
PYTHON = r"{{PYTHON}}"
HERMES = r"{{HERMES}}"
CLAUDE = r"{{CLAUDE}}"
PASS_COMMANDS = {"/new", "/reset", "/status", "/restart", "/stop"}
TIMEOUT = 1800

logger = logging.getLogger("skin-router")
_queue = queue.Queue()
_started = False
_lock = threading.Lock()


def _env():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HERMES_BIN=HERMES, CLAUDE_BIN=CLAUDE)
    env["PATH"] = os.pathsep.join([os.path.dirname(CLAUDE), os.path.dirname(PYTHON), env.get("PATH", "")])
    return env


def _origin(source):
    """메시지가 온 방. Hermes 버전마다 속성 이름이 다를 수 있어 여러 이름을 본다."""
    chat = None
    for name in ("chat_id", "channel_id", "conversation_id"):
        chat = chat or getattr(source, name, None)
    chat = chat or getattr(getattr(source, "chat", None), "id", None)
    thread = None
    for name in ("thread_id", "message_thread_id", "topic_id"):
        thread = thread or getattr(source, name, None)
    if chat is None or not str(chat).lstrip("-").isdigit():
        return ""
    return f"telegram:{chat}" + (f":{thread}" if thread and str(thread).isdigit() else "")


def _source_keys(source):
    """방을 못 찾았을 때 진단용: source 의 속성 이름만 (값은 넘기지 않는다)."""
    try:
        names = sorted(vars(source)) if hasattr(source, "__dict__") else sorted(n for n in dir(source) if not n.startswith("_"))
    except Exception:  # noqa: BLE001
        names = []
    return ",".join(names)[:300]


def _handle(origin, text, keys=""):
    env = _env()
    env["SKIN_TG_ORIGIN"] = origin
    env["SKIN_TG_SOURCE_KEYS"] = "" if origin else keys
    proc = subprocess.run([PYTHON, "-m", "pipeline.telegram_router", "handle"], input=text.encode("utf-8"), cwd=REPO,
                          env=env, capture_output=True, timeout=TIMEOUT, check=False)
    if proc.returncode != 0:
        logger.warning("telegram_router 실패 (%s)", proc.returncode)


def _loop():
    later = None  # 다른 방에서 온 메시지는 섞지 않고 다음 차례로
    while True:
        origin, text, keys = later or _queue.get()
        later = None
        deadline = time.monotonic() + MAX_WAIT
        while time.monotonic() < deadline:  # 같은 방에서 연달아 보낸 메시지를 모아 순서대로 한 번에
            try:
                nxt = _queue.get(timeout=INPUT_DELAY)
            except queue.Empty:
                break
            if nxt[0] != origin:
                later = nxt
                break
            text += "\n" + nxt[1]
        try:
            _handle(origin, text, keys)
        except Exception as e:  # noqa: BLE001 — 스레드가 죽으면 이후 메시지가 전부 멈춘다
            logger.warning("telegram_router 호출 실패: %s", type(e).__name__)


def _ensure_thread():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_loop, name="skin-router", daemon=True).start()
            _started = True


def on_dispatch(event=None, gateway=None, **_):
    source = event.source
    if getattr(source.platform, "value", "") != "telegram" or not gateway._is_user_authorized(source):
        return None  # 다른 플랫폼·미허용 사용자는 기존 흐름 (페어링 등)
    text = (event.text or "").strip()
    if text.startswith("/"):
        if text.split()[0].split("@")[0].lower() in PASS_COMMANDS:
            return None
        text = "도움말"
    if text:
        _ensure_thread()
        try:
            origin = _origin(source)
        except Exception:  # noqa: BLE001 — 방을 몰라도 메시지는 처리한다 (기본 방으로 답)
            origin = ""
        _queue.put((origin, text, "" if origin else _source_keys(source)))
    return {"action": "skip", "reason": "skin-router"}


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_dispatch)
