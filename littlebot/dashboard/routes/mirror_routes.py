"""Phone mirroring on LittleBot's screen (littlebot/mirror): what's on, start an iPhone or Android phone, stop,
pair and forget Android phones, the parent's switch. Nothing secret is kept: adb keeps its own keys."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body
from littlebot.mirror import adb as A

router = APIRouter(prefix="/api/mirror", tags=["mirror"])

UNAVAILABLE = "Phone mirroring is unavailable right now."


class MirrorStartBody(Body):
    phone: Literal["iphone", "android"]


class MirrorPairBody(Body):
    address: str = Field(min_length=9, max_length=21)  # "192.168.1.23:37099"
    code: str = Field(min_length=6, max_length=6)
    name: str = Field(default="", max_length=40)


class MirrorForgetBody(Body):
    key: str = Field(min_length=1, max_length=80)  # the phone's guid or address


class MirrorSettingsBody(Body):
    enabled: bool


def _svc(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "mirror", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


def _view(dash: DashState) -> dict[str, Any]:
    svc = getattr(dash.ctx, "mirror", None)
    if svc is None:
        return {"available": False, "enabled": bool(dash.ctx.settings.get("mirror.enabled", True)),
                "detail": "not running", "active": False, "kind": None, "state": None, "android_phones": []}
    return svc.status()


@router.get("")
async def mirror_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/start")
async def mirror_start(body: MirrorStartBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).start(body.phone, source="dashboard")
    return {**res, "status": _view(dash)}


@router.post("/stop")
async def mirror_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    stopped = bool(await _svc(dash).stop("parent"))
    return {"ok": True, "stopped": stopped, **_view(dash)}


@router.post("/android/pair")
async def mirror_pair(body: MirrorPairBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _svc(dash)
    if A.check_address(body.address) is None:
        raise HTTPException(status_code=422, detail="The pairing address looks like 192.168.1.23:37099.")
    if A.check_code(body.code) is None:
        raise HTTPException(status_code=422, detail="The pairing code is six digits.")
    res = await svc.pair(body.address, body.code, name=body.name)
    log.info("mirror: Android pairing from dashboard: %s", "paired" if res.get("ok") else res.get("error"))
    return {**res, "status": _view(dash)}


@router.post("/android/forget")
async def mirror_forget(body: MirrorForgetBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    forgot = bool(await _svc(dash).forget(body.key))
    return {"ok": True, "forgot": forgot, **_view(dash)}


@router.put("/settings")
async def mirror_settings(body: MirrorSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.ctx.settings.set("mirror.enabled", bool(body.enabled), source="dashboard")
    log.info("phone mirroring switched %s from dashboard", "on" if body.enabled else "off")
    return {"ok": True, **_view(dash)}
