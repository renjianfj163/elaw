"""本机工作台的访问边界。

只接受本机地址。启动器发一次性令牌，换成 HttpOnly、SameSite=Strict 的 cookie。
没有这枚 cookie 的请求一律拒绝。写操作还要带本机 Origin。
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse

COOKIE = "elaw_session"
_IDLE_SECONDS = 15 * 60
_lock = threading.Lock()
_launch_tokens: set[str] = set()
_sessions: set[str] = set()
_last_seen = time.monotonic()
_idle_started = False
_before_exit: list = []


def guard_enabled() -> bool:
    from app.public_trial import enabled

    if enabled():
        return False
    return os.environ.get("ELAW_GUARD", "1") != "0"


def current_port() -> int:
    try:
        port = int(os.environ.get("ELAW_PORT", "8000"))
    except ValueError:
        return 8000
    if port < 1 or port > 65535:
        return 8000
    return port


def touch() -> None:
    global _last_seen
    with _lock:
        _last_seen = time.monotonic()


def before_exit(callback) -> None:
    _before_exit.append(callback)


def issue_launch_token() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _launch_tokens.add(token)
    touch()
    return token


def consume_launch_token(token: str) -> str | None:
    if not token:
        return None
    with _lock:
        if token not in _launch_tokens:
            return None
        _launch_tokens.remove(token)
        session = secrets.token_urlsafe(32)
        _sessions.add(session)
    touch()
    return session


def session_ok(value: str) -> bool:
    if not value:
        return False
    with _lock:
        return value in _sessions


def reset_for_tests() -> None:
    global _last_seen
    with _lock:
        _launch_tokens.clear()
        _sessions.clear()
        _last_seen = time.monotonic()


def host_allowed(host: str, port: int) -> bool:
    return host.strip().lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}


def origin_allowed(origin: str, port: int) -> bool:
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def _terminate() -> None:
    for callback in list(_before_exit):
        try:
            callback()
        except Exception:
            pass
    os._exit(0)


def request_shutdown() -> bool:
    if not guard_enabled():
        return False
    delay = float(os.environ.get("ELAW_SHUTDOWN_DELAY", "0.3"))

    def later() -> None:
        time.sleep(max(0.0, delay))
        _terminate()

    threading.Thread(target=later, daemon=True).start()
    return True


def start_idle_watch() -> None:
    global _idle_started
    with _lock:
        if _idle_started:
            return
        _idle_started = True
    idle = float(os.environ.get("ELAW_IDLE_SECONDS", str(_IDLE_SECONDS)))

    def loop() -> None:
        while True:
            time.sleep(5)
            if not guard_enabled():
                continue
            with _lock:
                quiet = time.monotonic() - _last_seen
            if quiet >= idle:
                _terminate()

    threading.Thread(target=loop, daemon=True).start()


router = APIRouter()


@router.post("/api/heartbeat")
def heartbeat():
    touch()
    return {"ok": True}


@router.post("/api/shutdown")
def shutdown():
    return {"ok": request_shutdown()}


async def guard_middleware(request, call_next):
    if not guard_enabled():
        return await call_next(request)
    port = current_port()
    if not host_allowed(request.headers.get("host", ""), port):
        return PlainTextResponse("拒绝访问", status_code=400)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not origin_allowed(request.headers.get("origin", ""), port):
            return PlainTextResponse("拒绝访问", status_code=403)
    if request.method == "GET" and request.url.path == "/auth/launch":
        session = consume_launch_token(request.query_params.get("token", ""))
        if session is None:
            return PlainTextResponse("请从本机启动器打开工作台", status_code=401)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE,
            session,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=12 * 60 * 60,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    if not session_ok(request.cookies.get(COOKIE, "")):
        return PlainTextResponse("请从本机启动器打开工作台", status_code=401)
    touch()
    return await call_next(request)


def install_guard(app) -> None:
    app.middleware("http")(guard_middleware)
    if guard_enabled():
        start_idle_watch()
