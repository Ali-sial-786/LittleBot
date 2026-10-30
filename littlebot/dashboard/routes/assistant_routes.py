"""The assistant (littlebot/assistant) from the phone: which voice is on and why, type to LittleBot, have it say
something (intercom), send a note to its screen, and what its skills hold (lists, reminders, memories ...).

Keys are never returned: only whether each one is set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, clean_text
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

UNAVAILABLE = "The assistant isn't running."


class ChatBody(Body):
    text: str = Field(..., min_length=1, max_length=2000)
    speak: bool = False  # also say the answer out loud

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return clean_text(v, max_len=2000)


class SayBody(Body):
    text: str = Field(..., min_length=1, max_length=400)

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return clean_text(v, max_len=400)


class NoteBody(Body):
    text: str = Field(..., min_length=1, max_length=400)
    title: str = Field(default="Message", max_length=60)
    say: bool = False  # read it out too

    @field_validator("text", "title")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v, max_len=400)


def _assistant(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "assistant", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


def _skills(dash: DashState) -> list[dict[str, Any]]:
    hub = getattr(dash.ctx, "skills", None)
    if hub is None:
        return []
    out: list[dict[str, Any]] = []
    for skill in getattr(hub, "skills", []):
        try:
            entry = dict(skill.status())
            entry["now"] = str(skill.context_note() or "") if skill.available() else ""
        except Exception:  # noqa: BLE001 - one broken skill never breaks the page
            entry = {"name": getattr(skill, "name", "?"), "available": False}
        out.append(entry)
    return out


@router.get("")
async def assistant_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = getattr(dash.ctx, "assistant", None)
    status = svc.status() if svc is not None else {"available": False, "active": False}
    live = getattr(dash.ctx, "live", None)
    return scrub({"assistant": status, "live_active": bool(live is not None and live.status().get("active")),
                  "skills": _skills(dash),
                  "privacy": {"cloud_enabled": dash.ctx.privacy.cloud_enabled,
                              "microphone_enabled": dash.ctx.privacy.microphone_enabled,
                              "offline_mode": dash.ctx.config.is_offline}})


@router.post("/chat")
async def chat(body: ChatBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _assistant(dash)
    res = await svc.handle_text(body.text, speak=body.speak, source="dashboard")
    if res.get("error") in ("offline", "cloud_off"):
        raise HTTPException(status_code=403, detail="Cloud communication is off (Privacy page)."
                            if res["error"] == "cloud_off" else "LittleBot is in offline mode.")
    return scrub(res)


@router.post("/say")
async def say(body: SayBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Intercom: LittleBot says ``text`` now (in its assistant voice when it can)."""
    svc = _assistant(dash)
    log.info("dashboard: intercom message (%d characters)", len(body.text))
    from littlebot.skills.base import announce  # noqa: PLC0415

    announce(dash.ctx, body.text, kind="intercom")
    return {"ok": True, "queued": True, "active": bool(svc.status().get("active"))}


@router.post("/note")
async def note(body: NoteBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Send a note to LittleBot's screen (and optionally read it out)."""
    screens = getattr(dash.ctx, "screens", None)
    if screens is None:
        raise HTTPException(status_code=503, detail="LittleBot's screen cards are unavailable.")
    res = await screens.show({"card": "note", "title": body.title or "Message", "text": body.text}, source="dashboard")
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail="LittleBot's screen is busy (a game, a video or an activity), "
                                                    "asleep, or cards are switched off.")
    if body.say:
        from littlebot.skills.base import announce  # noqa: PLC0415

        announce(dash.ctx, body.text, kind="intercom")
    return {"ok": True}


class TickBody(Body):
    list: str = Field(..., min_length=1, max_length=60)
    id: int = Field(..., ge=0)


class IdBody(Body):
    id: int = Field(..., ge=0)


def _skill(dash: DashState, name: str) -> Any:
    hub = getattr(dash.ctx, "skills", None)
    skill = hub.get(name) if hub is not None and hasattr(hub, "get") else None
    if skill is None:
        raise HTTPException(status_code=503, detail=f"The {name} skill isn't running.")
    return skill


@router.get("/data")
async def data(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """What LittleBot keeps for the family: memories, lists and upcoming reminders (on the robot only)."""
    hub = getattr(dash.ctx, "skills", None)
    get = (lambda n: hub.get(n)) if hub is not None and hasattr(hub, "get") else (lambda n: None)
    out: dict[str, Any] = {"memories": [], "lists": [], "reminders": []}
    memory, lists, reminders = get("memory"), get("lists"), get("reminders")
    try:
        if memory is not None:
            out["memories"] = memory.entries()
        if lists is not None:
            out["lists"] = [{"name": name, "items": [{"id": int(i["id"]), "item": str(i["item"])}
                                                     for i in lists.items(name)]} for name, _ in lists.names()]
        if reminders is not None:
            out["reminders"] = [it.as_dict() for it in reminders.upcoming()]
    except Exception:  # noqa: BLE001 - never break the page
        log.exception("assistant data failed")
    return scrub(out)


@router.post("/lists/tick")
async def list_tick(body: TickBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    gone = _skill(dash, "lists").tick_off(body.list, body.id)
    return {"ok": gone is not None}


@router.post("/memories/forget")
async def memory_forget(body: IdBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    _skill(dash, "memory").delete(body.id)
    return {"ok": True}


@router.post("/reminders/cancel")
async def reminder_cancel(body: IdBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    rem = _skill(dash, "reminders")
    item = rem.item(body.id)
    if item is None:
        raise HTTPException(status_code=404, detail="That reminder is already gone.")
    rem.cancel([item])
    return {"ok": True}


@router.post("/glance")
async def glance(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    screens = getattr(dash.ctx, "screens", None)
    if screens is None:
        raise HTTPException(status_code=503, detail="LittleBot's screen cards are unavailable.")
    res = await screens.show({"card": "glance"}, source="dashboard")
    return {"ok": bool(res.get("ok")), "error": res.get("error")}


@router.post("/session/start")
async def session_start(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _assistant(dash)
    res = await svc.start_session(parent_present=True, source="dashboard")
    if not res.get("ok"):
        code = 409 if res.get("error") in ("already_running", "busy") else 403
        raise HTTPException(status_code=code, detail=res.get("detail") or "The assistant can't start right now.")
    return scrub(res)


@router.post("/session/stop")
async def session_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _assistant(dash)
    return scrub(await svc.stop_session("stopped from the dashboard"))


@router.post("/history/clear")
async def history_clear(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _assistant(dash)
    svc.history.clear()
    return {"ok": True}
