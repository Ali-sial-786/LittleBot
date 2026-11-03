"""Calendars (Settings page): the family's Google calendars LittleBot may read, each person's Work, Personal...

A parent adds each calendar with its person, a name ("Work") and its secret iCal link; the link is read first,
so a wrong one is refused at once with what to do about it. The link itself never comes back to the browser
(only "calendar.google.com, secret address"). See littlebot/info/calendar.py and docs/CALENDAR.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, field_validator

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, clean_text
from littlebot.dashboard.scrub import scrub
from littlebot.info.calendar import MAX_LINK, NAME_MAX, CalendarError, get_calendars, summary_text

router = APIRouter(prefix="/api/calendars", tags=["calendars"])

_STATUS = {"bad_link": 422, "not_found": 422, "not_calendar": 422, "too_big": 422, "offline": 403, "cloud_off": 403,
           "no_calendars": 409, "not_installed": 503, "timeout": 504, "network": 502, "bad_response": 502,
           "limited": 503, "whose": 422, "unknown_person": 404, "unknown_calendar": 404, "bad_date": 422}


class CalendarBody(Body):
    person: str = Field(min_length=1, max_length=NAME_MAX + 20)
    label: str = Field(default="", max_length=NAME_MAX + 20)  # "" = the calendar's own name
    link: str = Field(min_length=1, max_length=MAX_LINK + 50)

    @field_validator("person", "label")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v, max_len=NAME_MAX + 20, field="name")


class CalendarUpdateBody(Body):
    person: str | None = Field(default=None, min_length=1, max_length=NAME_MAX + 20)
    label: str | None = Field(default=None, min_length=1, max_length=NAME_MAX + 20)
    link: str | None = Field(default=None, max_length=MAX_LINK + 50)  # None / "" = keep the link

    @field_validator("person", "label")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return None if v is None else clean_text(v, max_len=NAME_MAX + 20, field="name")


def _fail(e: CalendarError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(e.kind, 502), detail=e.help)


def _view(dash: DashState) -> dict[str, Any]:
    return scrub(get_calendars(dash.ctx).status())


def _check(check: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if check is None else {k: v for k, v in check.items() if not k.startswith("_")}


@router.get("")
async def calendars(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("")
async def add_calendar(body: CalendarBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    try:
        cal, check = await get_calendars(dash.ctx).add(body.person, body.label, body.link)
    except CalendarError as e:
        raise _fail(e) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    log.info("calendar: a calendar was added from the dashboard")
    return {"ok": True, "calendar": scrub(cal), "check": scrub(_check(check)), **_view(dash)}


@router.put("/{cal_id}")
async def update_calendar(body: CalendarUpdateBody, cal_id: int = Path(ge=1),
                          dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    try:
        cal, check = await get_calendars(dash.ctx).update(cal_id, person=body.person, label=body.label,
                                                          link=body.link or None)
    except KeyError:
        raise HTTPException(status_code=404, detail="That calendar isn't on the list.") from None
    except CalendarError as e:
        raise _fail(e) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, "calendar": scrub(cal), "check": scrub(_check(check)), **_view(dash)}


@router.delete("/{cal_id}")
async def remove_calendar(cal_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if not get_calendars(dash.ctx).remove(cal_id):
        raise HTTPException(status_code=404, detail="That calendar isn't on the list.")
    log.info("calendar: a calendar was removed from the dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/{cal_id}/check")
async def check_calendar(cal_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Read the calendar now: whether it works, its name, how many events in the next two weeks, the next one."""
    try:
        check = await get_calendars(dash.ctx).check(cal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="That calendar isn't on the list.") from None
    return {**scrub(_check(check)), **_view(dash)}


@router.get("/agenda")
async def agenda(dash: DashState = Depends(get_dash), day: str = Query("today", max_length=40),
                 person: str | None = Query(None, max_length=60)) -> dict[str, Any]:
    """What LittleBot would say is on (the same answer the AI gets), and lines for the parent."""
    try:
        result = await get_calendars(dash.ctx).agenda(day=day, person=person or None)
    except CalendarError as e:
        raise _fail(e) from None
    return scrub({"ok": True, "summary": summary_text(result), "agenda": result})
