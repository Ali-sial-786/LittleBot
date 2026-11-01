"""Calendars and phones, per person (littlebot/skills/agenda.py "Whose calendar", push.py, docs/CALENDAR.md): connect
the household's or a grown-up's Google Calendar (Google's sign-in in a new tab, then the address it lands on pasted
back), save a secret iCal address, say whose each calendar is, and set up a phone for notifications.

Never returned: a token, the client secret, an iCal address. A phone's ntfy topic is shown once, when it is made (the
phone needs it to subscribe), and after that only whether there is one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, SecretBody
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class WhoBody(Body):
    person_id: int | None = Field(default=None, ge=1)  # None: the household's


class BeginBody(SecretBody):
    person_id: int | None = Field(default=None, ge=1)
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=200)


class FinishBody(Body):
    person_id: int | None = Field(default=None, ge=1)
    url: str = Field(..., min_length=1, max_length=4000)


class IcsBody(SecretBody):
    person_id: int | None = Field(default=None, ge=1)
    url: str = Field(..., min_length=8, max_length=2000)


class OwnerBody(Body):
    key: str = Field(..., min_length=1, max_length=300)  # "<account>:<calendar id>" or "<account>"
    person_id: int | None = Field(default=None, ge=0)  # None / 0: the family's


def _calendar(dash: DashState) -> Any:
    hub = getattr(dash.ctx, "skills", None)
    cal = hub.get("calendar") if hub is not None and hasattr(hub, "get") else None
    if cal is None:
        raise HTTPException(status_code=503, detail="The calendar skill isn't running (skills.calendar).")
    return cal


def _push(dash: DashState) -> Any:
    hub = getattr(dash.ctx, "skills", None)
    heads = hub.get("heads_up") if hub is not None and hasattr(hub, "get") else None
    if heads is not None and getattr(heads, "push", None) is not None:
        return heads.push
    from littlebot.skills.push import Push  # noqa: PLC0415

    return Push(dash.ctx)


def _online(dash: DashState) -> None:
    if dash.ctx.config.is_offline:
        raise HTTPException(status_code=403, detail="LittleBot is in offline mode, so it can't reach the calendar.")
    if not dash.ctx.privacy.cloud_enabled:
        raise HTTPException(status_code=403, detail="The calendar needs cloud communication: switch it on in Privacy.")


def _fail(e: Exception) -> HTTPException:
    t = str(e).strip() or "That didn't work."
    return HTTPException(status_code=400, detail=(t[:1].upper() + t[1:]).rstrip(".") + ".")


async def view(dash: DashState) -> dict[str, Any]:
    cal = _calendar(dash)
    push = _push(dash)

    def one(person_id: int | None) -> dict[str, Any]:
        acc = cal.account(person_id)
        t = acc.auth.tokens() if acc is not None else None
        return {"google": t is not None, "connected_at": t.connected_at if t is not None else None,
                "ics_feeds": len(cal.ics_urls(acc)) if acc is not None else 0,
                "phone": bool(push.topic(person_id))}

    people = [{"id": p["id"], "name": p["name"], "role": p["role"], **one(p["id"])} for p in cal.people()]
    cal.forget_people_cache()  # the next look sees a person just added on Who's who
    try:
        calendars = await cal.calendars_view()
    except Exception:  # noqa: BLE001 - the page still works
        log.warning("dashboard: the calendar list failed", exc_info=True)
        calendars = []
    from littlebot.skills.google_calendar import REDIRECT_URI  # noqa: PLC0415

    return {"home": one(None), "people": people, "calendars": calendars, "client_known": cal.known_client() is not None,
            "redirect_uri": REDIRECT_URI, "ntfy_server": push.server(), "problem": cal.status().get("problem")}


@router.get("")
async def calendar_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return scrub(await view(dash))


@router.post("/google/begin")
async def google_begin(body: BeginBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Google's sign-in address for a new tab (nothing secret in it: not scrubbed)."""
    from littlebot.skills.agenda import AgendaError  # noqa: PLC0415
    from littlebot.skills.google_calendar import CalendarError  # noqa: PLC0415

    cal = _calendar(dash)
    _online(dash)
    try:
        url = cal.begin_connect(body.person_id, body.client_id, body.client_secret)
    except (CalendarError, AgendaError) as e:
        raise _fail(e) from None
    log.info("dashboard: a Google Calendar sign-in was started (%s)", "a person" if body.person_id else "household")
    return {"ok": True, "url": url}


@router.post("/google/finish")
async def google_finish(body: FinishBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    from littlebot.skills.agenda import AgendaError  # noqa: PLC0415
    from littlebot.skills.google_calendar import CalendarError  # noqa: PLC0415

    cal = _calendar(dash)
    _online(dash)
    try:
        await cal.finish_connect(body.person_id, body.url)
    except (CalendarError, AgendaError) as e:
        raise _fail(e) from None
    log.info("dashboard: Google Calendar connected (%s)", "a person" if body.person_id else "household")
    return scrub({"ok": True, **await view(dash)})


@router.post("/google/disconnect")
async def google_disconnect(body: WhoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    had = _calendar(dash).disconnect(body.person_id)
    log.info("dashboard: Google Calendar disconnected")
    return scrub({"ok": True, "was_connected": had, **await view(dash)})


@router.put("/ics")
async def ics_save(body: IcsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Read the iCal address once (it must work), then keep it (0600). The address is never sent back."""
    from littlebot.skills.agenda import AgendaError  # noqa: PLC0415

    cal = _calendar(dash)
    _online(dash)
    try:
        found = await cal.set_ics(body.person_id, body.url)
    except AgendaError as e:
        raise _fail(e) from None
    return scrub({"ok": True, "events": found, **await view(dash)})


@router.post("/ics/remove")
async def ics_remove(body: WhoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    had = _calendar(dash).remove_ics(body.person_id)
    return scrub({"ok": True, "removed": had, **await view(dash)})


@router.put("/owner")
async def owner(body: OwnerBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    from littlebot.skills.agenda import AgendaError  # noqa: PLC0415

    try:
        _calendar(dash).set_owner(body.key, body.person_id)
    except AgendaError as e:
        raise _fail(e) from None
    return scrub({"ok": True, **await view(dash)})


@router.post("/phone")
async def phone_make(body: WhoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """A new ntfy topic for the household's or a person's phone, and a test message to it. The topic is in this
    answer (once): the phone subscribes to it."""
    cal = _calendar(dash)
    if body.person_id is not None and cal.person(body.person_id) is None:
        raise HTTPException(status_code=404, detail="That person isn't on Who's who.")
    push = _push(dash)
    topic = push.make_topic(body.person_id)
    sent = await push.send("LittleBot is connected", "Heads-ups and reminders will show up here.", tags=("robot",),
                           to=body.person_id)
    out = await view(dash)
    return {**scrub(out), "ok": True, "topic": topic, "server": push.server(), "test_sent": sent,
            "test_problem": None if sent else push.last_error}


@router.post("/phone/test")
async def phone_test(body: WhoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    push = _push(dash)
    if not push.topic(body.person_id):
        raise HTTPException(status_code=409, detail="That phone isn't set up yet.")
    sent = await push.send("Test from LittleBot", "If you can read this, it works.", tags=("robot",),
                           to=body.person_id)
    return {"ok": sent, "detail": "Sent." if sent else f"It didn't go ({push.last_error or 'unknown'})."}


@router.post("/phone/remove")
async def phone_remove(body: WhoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    had = _push(dash).remove_topic(body.person_id)
    return scrub({"ok": True, "removed": had, **await view(dash)})
