"""Android (Waydroid) on LittleBot's screen (littlebot/android): set up?, open (Android or one of its apps), close,
the parent's switch. Android is shut down by itself once it isn't on the screen; nothing secret passes here (the
Google sign-in stays inside Android)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/android", tags=["android"])

UNAVAILABLE = "Android is unavailable right now."


class AndroidOpenBody(Body):
    app: str | None = Field(default=None, max_length=60)  # a name on android.apps ("aosu"); None: Android itself


class AndroidSettingsBody(Body):
    enabled: bool


def _svc(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "android", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


def _view(dash: DashState) -> dict[str, Any]:
    svc = getattr(dash.ctx, "android", None)
    if svc is None:
        return {"available": False, "enabled": bool(dash.ctx.settings.get("android.enabled", True)),
                "detail": "not running", "active": False, "state": None, "set_up": False, "apps": {}}
    return svc.status()


@router.get("")
async def android_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = getattr(dash.ctx, "android", None)
    if svc is not None:
        await svc.refresh()
    return _view(dash)


@router.post("/open")
async def android_open(body: AndroidOpenBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).open(source="dashboard", app=body.app or None)
    log.info("android: open from dashboard: %s", "ok" if res.get("ok") else res.get("error"))
    return {**res, "status": _view(dash)}


@router.post("/close")
async def android_close(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    closed = bool(await _svc(dash).close("closed on the dashboard"))
    return {"ok": True, "closed": closed, **_view(dash)}


@router.put("/settings")
async def android_settings(body: AndroidSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.ctx.settings.set("android.enabled", bool(body.enabled), source="dashboard")
    log.info("android switched %s from dashboard", "on" if body.enabled else "off")
    return {"ok": True, **_view(dash)}
