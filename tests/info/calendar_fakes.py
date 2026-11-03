"""Stand-ins for Google Calendar's iCal feeds (no network): two people, three calendars, a fixed "now".

Now is Tuesday 3 November 2026, 08:00 in Toronto (13:00 UTC). Ali has Work (a standup every Monday and
Tuesday at 09:30, longer today; the dentist on Thursday) and Personal (a conference on Wednesday and Thursday,
a cancelled lunch, the family dinner on Friday); Sam has Personal (the same family dinner, swimming on Saturday).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx

NOW = dt.datetime(2026, 11, 3, 13, 0, tzinfo=dt.timezone.utc)  # 08:00 in Toronto, a Tuesday

ALI_WORK = "https://calendar.google.com/calendar/ical/ali.work%40example.com/private-0a1b2c3d4e5f/basic.ics"
ALI_HOME = "https://calendar.google.com/calendar/ical/ali%40gmail.com/private-99aa88bb77cc/basic.ics"
SAM_HOME = "https://calendar.google.com/calendar/ical/sam%40gmail.com/private-5566ddee44ff/basic.ics"
PUBLIC = "https://calendar.google.com/calendar/ical/en.canadian%23holiday%40group.v.calendar.google.com/public/basic.ics"

TORONTO = """BEGIN:VTIMEZONE
TZID:America/Toronto
X-LIC-LOCATION:America/Toronto
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def ics(name: str, *events: str) -> bytes:
    body = "\n".join(["BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0",
                      "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{name}", "X-WR-TIMEZONE:America/Toronto",
                      TORONTO, *events, "END:VCALENDAR"])
    return (body.replace("\n", "\r\n") + "\r\n").encode()


def event(uid: str, summary: str, start: str, end: str | None = None, *extra: str) -> str:
    """``start`` / ``end``: "20261103T093000" (Toronto), "20261104" (a date: all day) or "...Z" (UTC)."""
    def prop(key: str, value: str) -> str:
        if len(value) == 8:
            return f"{key};VALUE=DATE:{value}"
        if value.endswith("Z"):
            return f"{key}:{value}"
        return f"{key};TZID=America/Toronto:{value}"

    lines = ["BEGIN:VEVENT", prop("DTSTART", start)]
    if end:
        lines.append(prop("DTEND", end))
    lines += [f"UID:{uid}", f"SUMMARY:{summary}", *extra, "END:VEVENT"]
    return "\n".join(lines)


FEEDS: dict[str, bytes] = {
    ALI_WORK: ics(
        "Ali Work",
        event("standup@x", "Standup", "20261005T093000", "20261005T100000", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU",
              "EXDATE;TZID=America/Toronto:20261110T093000",
              "DESCRIPTION:Dial in: 555-0100 PIN 1234 (never sent to the AI)"),
        event("standup@x", "Standup (long)", "20261103T093000", "20261103T103000",
              "RECURRENCE-ID;TZID=America/Toronto:20261103T093000"),
        event("dentist@x", "Dentist - Dr Lee", "20261105T120000", "20261105T130000", "LOCATION:12 King St\\, Toronto"),
        event("old@x", "Kick-off from last year", "20251020T150000Z", "20251020T160000Z"),
        event("call@x", "Planning call", "20261103T190000Z", "20261103T193000Z",
              "LOCATION:https://meet.google.com/abc-defg-hij"),
    ),
    ALI_HOME: ics(
        "ali@gmail.com",
        event("conf@x", "Conference", "20261104", "20261106", "LOCATION:Montreal\\, QC"),
        event("lunch@x", "Lunch with Jo", "20261104T170000Z", "20261104T180000Z", "STATUS:CANCELLED"),
        event("dinner@x", "Family dinner", "20261106T180000", "20261106T200000"),
    ),
    SAM_HOME: ics(
        "Sam",
        event("dinner@x", "Family dinner", "20261106T180000", "20261106T200000"),
        event("swim@x", "Swimming lessons", "20261107T100000", "20261107T110000", "RRULE:FREQ=WEEKLY;COUNT=10"),
    ),
    PUBLIC: ics("Holidays in Canada", event("rem@x", "Remembrance Day", "20261111", "20261112")),
}


class FakeGoogle:
    """httpx handler serving FEEDS. ``delay``: seconds before an answer (per link, or for all with "*");
    ``status``: an HTTP status per link; ``pages``: links that answer with a web page instead."""

    def __init__(self, feeds: dict[str, bytes] | None = None) -> None:
        self.feeds = dict(FEEDS if feeds is None else feeds)
        self.delay: dict[str, float] = {}
        self.status: dict[str, int] = {}
        self.pages: set[str] = set()
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(request)
        wait = self.delay.get(url, self.delay.get("*", 0.0))
        if wait:
            await asyncio.sleep(wait)
        if url in self.status:
            return httpx.Response(self.status[url], text="<html>nope</html>")
        if url in self.pages:
            return httpx.Response(200, text="<!doctype html><html><body>Google Calendar</body></html>",
                                  headers={"content-type": "text/html"})
        data = self.feeds.get(url)
        if data is None:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, content=data, headers={"content-type": "text/calendar; charset=UTF-8"})

    def count(self, url: str | None = None) -> int:
        return len([r for r in self.requests if url is None or str(r.url) == url])


class Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def calendar_service(ctx: Any, web: FakeGoogle | None = None, *, now: Any = None, clock: Any = None) -> Any:
    """A CalendarService on ``ctx`` (``ctx.extras["calendars"]``, as get_calendars would make it) using ``web``."""
    from littlebot.info.calendar import CalendarService

    svc = CalendarService(ctx, transport=httpx.MockTransport(web or FakeGoogle()), now=now or Monotonic(),
                          clock=clock or (lambda: NOW))
    ctx.extras["calendars"] = svc
    return svc


def add_family(svc: Any) -> None:
    """Ali (Work, Personal) and Sam (Personal), straight into the store (nothing is read)."""
    svc.store.add("Ali", "Work", ALI_WORK)
    svc.store.add("Ali", "Personal", ALI_HOME)
    svc.store.add("Sam", "Personal", SAM_HOME)
