"""Session, login, first-run setup, logout and password management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from littlebot.dashboard.auth import (
    MIN_PASSWORD_LENGTH, SESSION_COOKIE, AuthError, Session, SetupAlreadyDone,
)
from littlebot.dashboard.deps import DashState, client_ip, get_dash, get_session
from littlebot.dashboard.models import ChangePasswordBody, LoginBody, SetupBody

router = APIRouter(prefix="/api", tags=["auth"])


def _set_cookie(response: Response, request: Request, token: str, max_age: float) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(max_age), path="/", httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )


def _clear_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")


def _rate_limited(retry_after: float) -> HTTPException:
    return HTTPException(status_code=429, detail="Too many attempts. Please wait a minute and try again.",
                         headers={"Retry-After": str(int(retry_after + 0.999))})


def _session_payload(dash: DashState, sess: Session) -> dict[str, Any]:
    return {
        "authenticated": True,
        "setup_required": False,
        "csrf_token": sess.csrf_token,
        "expires_at": sess.expires,
        "robot_name": dash.ctx.config.robot_name,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Deliberately minimal: no robot data, no version."""
    return {"ok": True}


@router.get("/session")
async def session_info(request: Request, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    sess: Session | None = getattr(request.state, "session", None)
    if sess is not None:
        return _session_payload(dash, sess)
    return {"authenticated": False, "setup_required": dash.auth.setup_required,
            "min_password_length": MIN_PASSWORD_LENGTH}


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response,
                dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    auth = dash.auth
    if not auth.setup_required:
        raise HTTPException(status_code=409, detail="Setup has already been completed. Please log in.")
    retry = auth.setup_limiter.hit(client_ip(request))
    if retry is not None:
        raise _rate_limited(retry)
    try:
        await auth.complete_setup(body.setup_code, body.password)
    except SetupAlreadyDone as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    token, sess = auth.create_session(client_ip(request), request.headers.get("user-agent", ""))
    _set_cookie(response, request, token, auth.session_seconds)
    return _session_payload(dash, sess)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response,
                dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    auth = dash.auth
    ip = client_ip(request)
    retry = auth.login_limiter.hit(ip)
    if retry is not None:
        raise _rate_limited(retry)
    if auth.setup_required:
        raise HTTPException(status_code=409, detail="The dashboard has not been set up yet.")
    if not await auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token, sess = auth.create_session(ip, request.headers.get("user-agent", ""))
    _set_cookie(response, request, token, auth.session_seconds)
    return _session_payload(dash, sess)


@router.post("/logout")
async def logout(request: Request, response: Response, sess: Session = Depends(get_session),
                 dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.auth.revoke(sess.token_hash)
    _clear_cookie(response, request)
    return {"ok": True}


@router.post("/auth/logout-all")
async def logout_all(request: Request, response: Response, dash: DashState = Depends(get_dash),
                     _sess: Session = Depends(get_session)) -> dict[str, Any]:
    count = dash.auth.revoke_all()
    _clear_cookie(response, request)
    return {"ok": True, "sessions_ended": count}


@router.post("/auth/password")
async def change_password(body: ChangePasswordBody, request: Request, sess: Session = Depends(get_session),
                          dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    retry = dash.auth.password_limiter.hit(client_ip(request))
    if retry is not None:
        raise _rate_limited(retry)
    try:
        await dash.auth.change_password(body.current_password, body.new_password, keep_session=sess.token_hash)
    except AuthError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {"ok": True, "detail": "Password changed. Other devices have been logged out."}


@router.get("/auth/sessions")
async def sessions(dash: DashState = Depends(get_dash), sess: Session = Depends(get_session)) -> dict[str, Any]:
    return {"active_sessions": dash.auth.active_sessions(), "current": sess.public(),
            "session_hours": dash.auth.session_seconds / 3600.0}
