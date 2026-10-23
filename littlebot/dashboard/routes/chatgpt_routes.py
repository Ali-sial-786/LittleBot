"""Sign in with ChatGPT, live voice (and its wake word) and "ask what LittleBot sees". Tokens never leave the
robot: only the masked e-mail, the plan name and the one-time sign-in code (which the parent types on OpenAI's
page) are returned. Camera photos never reach the browser either - only the AI's answer text does."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.chatgpt import get_auth
from littlebot.chatgpt.auth import ChatGPTAuthError
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, LiveStartBody, LookBody

router = APIRouter(prefix="/api/chatgpt", tags=["chatgpt"])

# Reasons a photo may not be taken (the parent can change these); anything else is a failure (502).
_LOOK_BLOCKED = {"offline", "cloud_off", "camera_off", "ai_camera_off", "not_signed_in", "no_camera"}
_LOOK_UNAVAILABLE = {"enabled": False, "blocked": "unavailable",
                     "blocked_detail": "Looking through the camera isn't available on this robot yet.",
                     "photos_today": 0, "last_at": None}


class WakeBody(Body):
    # The parent's switch: saying "Hey LittleBot" / "Hey Bot" starts live voice.
    enabled: bool


def _wake_status(dash: DashState) -> dict[str, Any]:
    """The wake word's status (``ctx.wake``); a stand-in when this robot doesn't run it."""
    enabled = bool(dash.ctx.settings.get("wake.enabled", True))
    fallback = {"enabled": enabled, "available": False, "listening": False, "state": "unavailable", "paused": None,
                "detail": "The wake word isn't running on this robot.", "unavailable": "not_running",
                "unavailable_detail": None, "model_missing": False, "phrases": [], "triggers": 0, "last_heard": None}
    wake = getattr(dash.ctx, "wake", None)
    if wake is None:
        return fallback
    try:
        return wake.status()
    except Exception:  # never break the AI page's poll
        log.exception("wake word status failed")
        return fallback


def _camera_look(dash: DashState) -> Any:
    """The shared CameraLook (``ctx.extras["camera_look"]``), or None if this robot doesn't have it."""
    try:
        from littlebot.chatgpt.look import get_look
    except ImportError:
        return dash.ctx.extras.get("camera_look")
    return get_look(dash.ctx)


def _look_errors() -> dict[str, str]:
    try:
        from littlebot.chatgpt.look import ERRORS
    except ImportError:
        return {}
    return ERRORS


def _look_status(dash: DashState) -> dict[str, Any]:
    look = _camera_look(dash)
    if look is None:
        return dict(_LOOK_UNAVAILABLE)
    try:
        return look.status()
    except Exception:  # never break the AI page's poll
        log.exception("camera look status failed")
        return dict(_LOOK_UNAVAILABLE)


def _view(dash: DashState) -> dict[str, Any]:
    live = dash.ctx.live
    return {
        "account": get_auth(dash.ctx).status(),
        "live": live.status() if live is not None else {"available": False, "active": False, "can_start": False,
                                                        "blocked": "unavailable"},
        "privacy": {"cloud_enabled": dash.ctx.privacy.cloud_enabled,
                    "microphone_enabled": dash.ctx.privacy.microphone_enabled,
                    "camera_enabled": dash.ctx.privacy.camera_enabled,
                    "ai_camera_enabled": bool(getattr(dash.ctx.privacy, "ai_camera_enabled", False)),
                    "offline_mode": dash.ctx.config.is_offline},
        "look": _look_status(dash),
        "wake": _wake_status(dash),
    }


@router.get("")
async def chatgpt_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/sign-in")
async def sign_in(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if dash.ctx.config.is_offline:
        raise HTTPException(status_code=403, detail="LittleBot is in offline mode.")
    if not dash.ctx.privacy.cloud_enabled:
        raise HTTPException(status_code=403, detail="Turn on cloud communication in Privacy first.")
    try:
        await get_auth(dash.ctx).start_sign_in()
    except ChatGPTAuthError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    log.info("ChatGPT sign-in started from the dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/sign-in/cancel")
async def cancel_sign_in(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    get_auth(dash.ctx).cancel_sign_in()
    return {"ok": True, **_view(dash)}


@router.post("/sign-out")
async def sign_out(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    live = dash.ctx.live
    if live is not None:
        await live.stop_session("signed out")
    revoked = await get_auth(dash.ctx).sign_out()
    log.info("signed out of ChatGPT from the dashboard")
    return {"ok": True, "revoked": revoked, **_view(dash)}


@router.post("/live/start")
async def live_start(body: LiveStartBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    live = dash.need("live")
    result = await live.start_session(parent_present=body.parent_present)
    if not result.get("ok"):
        code = 409 if result.get("error") in ("already_running", "busy") else 403
        raise HTTPException(status_code=code, detail=result.get("detail") or "Live voice can't start right now.")
    return {"ok": True, **_view(dash)}


@router.post("/live/stop")
async def live_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    live = dash.need("live")
    await live.stop_session("stopped by parent")
    return {"ok": True, **_view(dash)}


@router.put("/wake")
async def set_wake(body: WakeBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Turn the wake word on or off (heard on the robot; nothing is sent until live voice starts)."""
    dash.ctx.settings.set("wake.enabled", body.enabled, source="dashboard")
    log.info("wake word turned %s from the dashboard", "on" if body.enabled else "off")
    return {"ok": True, **_view(dash)}


@router.post("/look")
async def look(body: LookBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Take one photo now, send it with the question to ChatGPT and return the answer text (never the photo)."""
    cam = _camera_look(dash)
    if cam is None:
        raise HTTPException(status_code=503, detail=_LOOK_UNAVAILABLE["blocked_detail"])
    result = await cam.look(body.question or "", asked_by="parent")  # empty: CameraLook asks "What do you see?"
    if not result.get("ok"):
        error = result.get("error")
        detail = result.get("detail") or _look_errors().get(str(error)) or "LittleBot couldn't look and answer right now."
        status = 403 if error in _LOOK_BLOCKED else 409 if error == "busy" else 502
        raise HTTPException(status_code=status, detail=detail)
    answer = str(result.get("answer") or "").strip()
    spoken = False
    audio = getattr(dash.ctx, "audio", None)
    if body.speak and answer and audio is not None:
        spoken = bool(await audio.say(answer))
    log.info("camera look from the dashboard answered (spoken=%s)", body.speak and bool(answer))
    out = {k: result.get(k) for k in ("ok", "answer", "error", "detail", "latency_ms")}
    return {"ok": True, "result": out, "spoken": spoken, "look": _look_status(dash)}
