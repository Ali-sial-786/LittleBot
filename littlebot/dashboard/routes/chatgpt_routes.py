"""Sign in with ChatGPT and live voice. Tokens never leave the robot: only the masked e-mail,
the plan name and the one-time sign-in code (which the parent types on OpenAI's page) are returned."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.chatgpt import get_auth
from littlebot.chatgpt.auth import ChatGPTAuthError
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import LiveStartBody

router = APIRouter(prefix="/api/chatgpt", tags=["chatgpt"])


def _view(dash: DashState) -> dict[str, Any]:
    live = dash.ctx.live
    return {
        "account": get_auth(dash.ctx).status(),
        "live": live.status() if live is not None else {"available": False, "active": False, "can_start": False,
                                                        "blocked": "unavailable"},
        "privacy": {"cloud_enabled": dash.ctx.privacy.cloud_enabled,
                    "microphone_enabled": dash.ctx.privacy.microphone_enabled,
                    "offline_mode": dash.ctx.config.is_offline},
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
