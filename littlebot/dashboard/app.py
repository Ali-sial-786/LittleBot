"""FastAPI application factory for the parent dashboard.

Security layers applied here (details in docs/modules/dashboard.md):

* an outermost ASGI guard (:class:`RequestGuard`) that runs before routing and
  before any body byte is read: ``Host`` allowlist (400, DNS-rebinding defence),
  request-body caps (413: 64 KB, 16 KB under ``/player``; ``Content-Length`` is
  checked up front and chunked bodies are cut off while streaming) and the
  client address for the auth failure accounting;
* an HTTP middleware that, for every ``/api/*`` request except the public
  allowlist, resolves the session cookie (401 if missing / expired) and, for
  state-changing methods, checks the ``X-CSRF-Token`` header and the
  Origin / Referer host (403 on mismatch);
* security headers on every response (CSP, X-Frame-Options, nosniff ...);
* exception mapping: PermissionError -> 403, KeyError -> 404, ValueError -> 422,
  with sanitised validation errors (request values are never echoed back).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from littlebot.core.context import RobotContext
from littlebot.dashboard.auth import CSRF_HEADER, SESSION_COOKIE, AuthManager, current_client
from littlebot.dashboard.deps import DashState

log = logging.getLogger("littlebot.dashboard")

# Parent-facing names for request fields (toasts read "Reply length must be 60 or less").
FIELD_LABELS = {
    "max_words": "Reply length", "max_requests_per_hour": "Requests per hour",
    "max_requests_per_day": "Requests per day", "daily_budget_usd": "Daily budget",
    "session_limit_minutes": "Session limit", "daily_limit_minutes": "Daily limit",
    "url": "Link", "title": "Title", "nickname": "Nickname", "birth_month": "Birth month",
    "age_months": "Age", "favourite_animals": "Favourite animals", "favourite_songs": "Favourite songs",
    "interests": "Interests", "remembered": "Things LittleBot remembers", "password": "Password", "new_password": "New password",
    "current_password": "Current password", "setup_code": "Setup code", "volume": "Volume",
    "tts_rate": "Speaking speed", "text": "Text", "angle": "Angle", "name": "Name", "seconds": "Seconds",
}


def friendly_validation_message(field: str, err_type: str, message: str, ctx: dict) -> str:
    """Turn a pydantic error into a short sentence a parent understands."""
    label = FIELD_LABELS.get(field.split(".")[-1] if field else "", "")
    if not label and field:
        label = field.split(".")[-1].replace("_", " ").capitalize()
    lim = ctx.get("le", ctx.get("lt", ctx.get("ge", ctx.get("gt", ctx.get("max_length", ctx.get("min_length"))))))
    phrases = {
        "less_than_equal": f"must be {lim} or less", "less_than": f"must be less than {lim}",
        "greater_than_equal": f"must be at least {lim}", "greater_than": f"must be more than {lim}",
        "string_too_long": f"is too long (at most {lim} characters)",
        "string_too_short": "is too short" if lim in (None, 1) else f"is too short (at least {lim} characters)",
        "too_long": f"has too many entries (at most {lim})", "missing": "is required",
        "float_parsing": "must be a number", "int_parsing": "must be a whole number",
        "bool_parsing": "must be on or off", "string_type": "must be text",
        "literal_error": "is not one of the allowed choices", "string_pattern_mismatch": "has an unexpected format",
    }
    if err_type == "value_error":  # our own validators already speak plain language
        text = message.strip()
        return text[:1].upper() + text[1:] if text else "Invalid value."
    phrase = phrases.get(err_type, message)
    return f"{label} {phrase}" if label else phrase[:1].upper() + phrase[1:]

STATIC_DIR = Path(__file__).resolve().parent / "static"

PUBLIC_API_PATHS = frozenset({"/api/health", "/api/session", "/api/login", "/api/setup"})
# Public paths on which a missing password + missing setup code (reset-password while running)
# makes the server issue a fresh code.
SETUP_CODE_PATHS = frozenset({"/api/session", "/api/setup"})
# Largest accepted request body. No dashboard request comes close (the biggest is a few KB);
# /player uses the media module's own, smaller cap (littlebot.media.player_routes.MAX_BODY_BYTES).
MAX_BODY_BYTES = 64 * 1024
DEFAULT_PLAYER_MAX_BODY_BYTES = 16 * 1024
UNKNOWN_HOST_MESSAGE = ("Unknown host name. Open the dashboard at the address LittleBot shows in its "
                        "console (or add this name to dashboard.allowed_hosts).\n")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Paths served by other modules' routers that set their own security headers.
EXTERNAL_PREFIXES = ("/player",)

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: https://i.ytimg.com",  # YouTube thumbnails for approved videos
    "connect-src 'self'",
    "font-src 'self'",
    "media-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
PAGE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _error(status: int, detail: str, code: str | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": detail}
    if code:
        body["code"] = code
    return JSONResponse(body, status_code=status, headers=headers)


def same_origin(request: Request) -> bool:
    """Origin (or Referer) must name the host the request was sent to, when present."""
    host = (request.headers.get("host") or "").lower()
    origin = request.headers.get("origin")
    if origin is not None:
        if origin == "null":
            return False
        return urlsplit(origin).netloc.lower() == host
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() == host
    return True  # non-browser client: the CSRF token check still applies


def _exc_message(exc: BaseException, fallback: str) -> str:
    if exc.args and isinstance(exc.args[0], str) and exc.args[0].strip():
        return exc.args[0][:300]
    return fallback


def create_app(ctx: RobotContext, robot: Any = None, *, auth: AuthManager | None = None) -> FastAPI:
    """Build the dashboard ASGI app. ``robot`` defaults to ``ctx.extras["robot"]`` (looked up lazily)."""
    app = FastAPI(title="LittleBot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    dash = DashState(ctx, robot, auth)
    app.state.dash = dash
    app.state.auth = dash.auth
    app.state.ctx = ctx

    # ------------------------------------------------------------- middleware
    @app.middleware("http")
    async def _security(request: Request, call_next: Any) -> Response:
        path = request.url.path
        method = request.method.upper()
        if path == "/api" or path.startswith("/api/"):
            if method not in SAFE_METHODS and not same_origin(request):
                return _apply_headers(path, _error(403, "Cross-site request blocked.", "origin"))
            token = request.cookies.get(SESSION_COOKIE)
            session = dash.auth.get_session(token)
            if path in PUBLIC_API_PATHS:
                if session is None and path in SETUP_CODE_PATHS:
                    dash.ensure_setup_code()
                request.state.session = session
                if method not in SAFE_METHODS:
                    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if ctype != "application/json":
                        return _apply_headers(path, _error(415, "Expected a JSON request.", "content_type"))
            else:
                if session is None:
                    return _apply_headers(path, _error(401, "Please log in.", "auth_required"))
                if method not in SAFE_METHODS and not dash.auth.check_csrf(session, request.headers.get(CSRF_HEADER)):
                    return _apply_headers(path, _error(403, "Security check failed. Please reload the page.", "csrf"))
                request.state.session = session
        response = await call_next(request)
        return _apply_headers(path, response)

    # ------------------------------------------------------------- errors
    @app.exception_handler(PermissionError)
    async def _perm(_req: Request, exc: PermissionError) -> JSONResponse:
        return _error(403, _exc_message(exc, "That isn't allowed right now."), "forbidden")

    @app.exception_handler(KeyError)
    async def _key(_req: Request, exc: KeyError) -> JSONResponse:
        return _error(404, _exc_message(exc, "Not found."), "not_found")

    @app.exception_handler(ValueError)
    async def _value(_req: Request, exc: ValueError) -> JSONResponse:
        if isinstance(exc, ValidationError):  # never echo the offending input back
            errs = exc.errors(include_input=False, include_url=False)
            msg = str(errs[0].get("msg", "Invalid value.")) if errs else "Invalid value."
            return _error(422, msg.removeprefix("Value error, "), "invalid")
        return _error(422, _exc_message(exc, "Invalid value."), "invalid")

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors()[:10]:
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            msg = str(err.get("msg", "invalid value"))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            errors.append({"field": ".".join(loc), "message": msg})
        if errors:
            raw = exc.errors()[0]
            detail = friendly_validation_message(errors[0]["field"], str(raw.get("type", "")),
                                                 errors[0]["message"], raw.get("ctx") or {})
        else:
            detail = "Invalid request."
        return JSONResponse({"detail": detail, "code": "invalid", "errors": errors}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _error(exc.status_code, detail, headers=getattr(exc, "headers", None))

    # ------------------------------------------------------------- routes
    from littlebot.dashboard.routes import ROUTERS

    for router in ROUTERS:
        app.include_router(router)

    _mount_player(app, ctx)

    # ------------------------------------------------------------- frontend
    index = STATIC_DIR / "index.html"

    async def _index() -> FileResponse:
        return FileResponse(index, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    for page in ("/", "/setup", "/login"):
        app.add_api_route(page, _index, methods=["GET"], include_in_schema=False)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Added last = outermost: runs before the security middleware, routing and body parsing.
    app.add_middleware(RequestGuard, dash=dash, player_max_body=_player_body_limit())
    return app


class RequestGuard:
    """Pure ASGI guard in front of everything else (see the module docstring).

    * ``Host`` must be on the allowlist (:class:`littlebot.dashboard.deps.HostAllowlist`), else 400.
    * ``Content-Length`` above the cap -> 413 without reading the body; bodies without a length
      (chunked) are counted while the app reads them and replaced by a 413 once over the cap,
      so at most ``cap`` bytes are ever buffered. 413 responses close the connection.
    * The client address is published in :data:`littlebot.dashboard.auth.current_client`.
    """

    def __init__(self, app: Any, *, dash: DashState, max_body: int = MAX_BODY_BYTES,
                 player_max_body: int = DEFAULT_PLAYER_MAX_BODY_BYTES) -> None:
        self.app = app
        self.dash = dash
        self.max_body = int(max_body)
        self.player_max_body = int(player_max_body)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        hosts: list[str] = []
        length: bytes | None = None
        for key, value in scope.get("headers") or ():
            if key == b"host":
                hosts.append(value.decode("latin-1"))
            elif key == b"content-length":
                length = value
        host = hosts[0] if len(hosts) == 1 else (None if not hosts else "")  # several Host headers: refuse
        if not self.dash.hosts.allowed(host, scope.get("server")):
            self.dash.hosts.note_rejected(host)
            await _unknown_host(path)(scope, receive, send)
            return
        limit = self.player_max_body if path == "/player" or path.startswith("/player/") else self.max_body
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                await _apply_headers(path, _error(400, "Invalid Content-Length.", "bad_request"))(scope, receive, send)
                return
            if declared > limit:
                await _too_large(path)(scope, receive, send)
                return

        state = {"received": 0, "too_large": False, "started": False}

        async def limited_receive() -> Any:
            if state["too_large"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                state["received"] += len(message.get("body") or b"")
                if state["received"] > limit:
                    # Stop here: the app sees a disconnect (never the extra bytes) and its
                    # response is replaced by a 413 below.
                    state["too_large"] = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Any) -> None:
            if state["too_large"]:
                if message.get("type") == "http.response.start" and not state["started"]:
                    state["started"] = True
                    await _too_large(path)(scope, receive, send)
                return  # drop whatever the app tried to send instead
            if message.get("type") == "http.response.start":
                state["started"] = True
            await send(message)

        client = scope.get("client")
        token = current_client.set(str(client[0]) if client else None)
        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not state["too_large"]:
                raise
        finally:
            current_client.reset(token)
        if state["too_large"] and not state["started"]:
            state["started"] = True
            await _too_large(path)(scope, receive, send)


def _unknown_host(path: str) -> Response:
    return _apply_headers(path, PlainTextResponse(UNKNOWN_HOST_MESSAGE, status_code=400,
                                                  headers={"Cache-Control": "no-store"}))


def _too_large(path: str) -> Response:
    return _apply_headers(path, _error(413, "That request is too large.", "too_large", headers={"Connection": "close"}))


def _player_body_limit() -> int:
    try:
        from littlebot.media.player_routes import MAX_BODY_BYTES as player_max  # type: ignore[import-not-found]
    except ImportError:
        return DEFAULT_PLAYER_MAX_BODY_BYTES
    return int(player_max)


def _apply_headers(path: str, response: Response) -> Response:
    headers = response.headers
    for k, v in BASE_HEADERS.items():
        if k not in headers:
            headers[k] = v
    if not path.startswith(EXTERNAL_PREFIXES):
        for k, v in PAGE_HEADERS.items():
            headers[k] = v
    if path.startswith("/api"):
        headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/") and "Cache-Control" not in headers:
        headers["Cache-Control"] = "no-cache"
    return response


def _mount_player(app: FastAPI, ctx: RobotContext) -> None:
    """Mount the media module's kiosk player (it does its own token check, no parent auth)."""
    try:
        from littlebot.media.player_routes import create_player_router  # type: ignore[import-not-found]
    except ImportError:
        log.debug("media player routes not available")
        return
    if ctx.media is None:
        return
    try:
        app.include_router(create_player_router(ctx.media))
    except Exception:  # noqa: BLE001
        log.exception("could not mount the media player routes")
