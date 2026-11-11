"""WhatsApp calls on LittleBot's screen (littlebot/calls): what's on (linked? a call? memory), open (WhatsApp, a
call to someone on the list, or to answer), close, the parent's switches (calls allowed; the chats hidden), the list
of people to call, and signing out on the robot (the saved profile folder is deleted). Nothing of WhatsApp's chats
passes here: the status holds states and the names on the parent's own list only."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.calls import contacts as C
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/calls", tags=["calls"])

UNAVAILABLE = "WhatsApp calls are unavailable right now."


class CallsOpenBody(Body):
    contact: str | None = Field(default=None, max_length=C.MAX_NAME)  # a said name on the list; None: just open
    kind: Literal["video", "voice"] = "video"
    answer: bool = False


class CallsSettingsBody(Body):
    enabled: bool | None = None
    hide_chats: bool | None = None


class CallsContact(Body):
    name: str = Field(max_length=C.MAX_NAME)
    whatsapp: str = Field(default="", max_length=C.MAX_NAME)


class CallsContactsBody(Body):
    contacts: list[CallsContact] = Field(max_length=C.MAX_CONTACTS)


def _svc(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "calls", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


def _contacts(dash: DashState) -> dict[str, str]:
    raw = dash.ctx.settings.get("calls.contacts", None)
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _view(dash: DashState) -> dict[str, Any]:
    svc = getattr(dash.ctx, "calls", None)
    if svc is None:
        settings = dash.ctx.settings
        return {"available": False, "enabled": bool(settings.get("calls.enabled", True)),
                "hide_chats": bool(settings.get("calls.hide_chats", True)), "detail": "not running",
                "active": False, "state": None, "call": "none", "linked": None, "profile": False,
                "contacts": [{"name": k, "whatsapp": v} for k, v in _contacts(dash).items()]}
    return svc.status()


@router.get("")
async def calls_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/open")
async def calls_open(body: CallsOpenBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).open(source="dashboard", contact=body.contact or None, kind=body.kind,
                                answer=bool(body.answer))
    log.info("calls: open from dashboard: %s", "ok" if res.get("ok") else res.get("error"))
    return {**res, "status": _view(dash)}


@router.post("/close")
async def calls_close(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).hang_up("closed on the dashboard")
    return {"ok": True, "closed": bool(res.get("closed")), **_view(dash)}


@router.put("/settings")
async def calls_settings(body: CallsSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if body.enabled is None and body.hide_chats is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    settings = dash.ctx.settings
    if body.enabled is not None:
        settings.set("calls.enabled", bool(body.enabled), source="dashboard")
        log.info("calls switched %s from dashboard", "on" if body.enabled else "off")
    if body.hide_chats is not None:
        settings.set("calls.hide_chats", bool(body.hide_chats), source="dashboard")
        log.info("calls: hiding WhatsApp's chats switched %s from dashboard", "on" if body.hide_chats else "off")
    return {"ok": True, **_view(dash)}


@router.put("/contacts")
async def calls_contacts(body: CallsContactsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    raw = [{"name": c.name, "whatsapp": c.whatsapp} for c in body.contacts]
    svc = getattr(dash.ctx, "calls", None)
    try:
        if svc is not None:
            svc.set_contacts(raw, source="dashboard")
        else:
            dash.ctx.settings.set("calls.contacts", C.clean(raw), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, **_view(dash)}


@router.post("/signout")
async def calls_signout(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    removed = bool(await _svc(dash).forget())
    log.info("calls: signed out of WhatsApp on the robot from dashboard (%s)",
             "the profile was deleted" if removed else "there was none")
    return {"ok": True, "removed": removed, **_view(dash)}
