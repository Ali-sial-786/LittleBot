"""The Nanit baby camera on LittleBot's screen (littlebot/nanit): set up?, the bridge, the cameras, show one, close,
the parent's switch and the sound switch. Never the bridge's sign-in: no response holds a token or the session
file (the service never reads it)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/nanit", tags=["nanit"])

UNAVAILABLE = "The baby camera view is unavailable right now."
SETUP_COMMAND = "bash ~/LittleBot/scripts/setup_nanit.sh"  # littlebot.nanit.service.SETUP_COMMAND


class NanitShowBody(Body):
    camera: str | None = Field(default=None, max_length=40)  # the camera's name; none = the first


class NanitSettingsBody(Body):
    enabled: bool | None = None
    sound: bool | None = None


def _svc(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "nanit", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


async def _view(dash: DashState, *, fresh: bool = False) -> dict[str, Any]:
    svc = getattr(dash.ctx, "nanit", None)
    if svc is None:
        s = dash.ctx.settings
        return {"available": False, "enabled": bool(s.get("nanit.enabled", True)),
                "sound": bool(s.get("nanit.sound", True)), "detail": "not running", "set_up": False,
                "setup_command": SETUP_COMMAND, "bridge": None, "bridge_running": False, "cameras": [],
                "active": False, "camera": None, "state": None}
    try:
        await svc.refresh(force=fresh)
    except Exception:  # noqa: BLE001 - the last known state is shown
        log.debug("nanit: asking for the bridge's state failed", exc_info=True)
    return svc.status()


@router.get("")
async def nanit_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return await _view(dash)


@router.post("/show")
async def nanit_show(body: NanitShowBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).show(body.camera or None, source="dashboard")
    return {**res, "status": await _view(dash)}


@router.post("/close")
async def nanit_close(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    closed = bool(await _svc(dash).close("parent"))
    return {"ok": True, "closed": closed, **(await _view(dash))}


@router.put("/settings")
async def nanit_settings(body: NanitSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if body.enabled is None and body.sound is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    s = dash.ctx.settings
    if body.enabled is not None:
        s.set("nanit.enabled", bool(body.enabled), source="dashboard")
        log.info("baby camera switched %s from dashboard", "on" if body.enabled else "off")
    if body.sound is not None:
        s.set("nanit.sound", bool(body.sound), source="dashboard")
        log.info("baby camera sound switched %s from dashboard", "on" if body.sound else "off")
    return {"ok": True, **(await _view(dash))}
