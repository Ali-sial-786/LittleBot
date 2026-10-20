"""FastAPI application factory for the parent dashboard.

Security layers applied here (details in docs/modules/dashboard.md):

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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from littlebot.core.context import RobotContext
from littlebot.dashboard.auth import CSRF_HEADER, SESSION_COOKIE, AuthManager
from littlebot.dashboard.deps import DashState

log = logging.getLogger("littlebot.dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"

PUBLIC_API_PATHS = frozenset({"/api/health", "/api/session", "/api/login", "/api/setup"})
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
            first = errors[0]
            detail = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
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
    return app


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
