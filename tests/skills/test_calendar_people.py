"""Each grown-up's own calendar (littlebot/skills/agenda.py, "Whose calendar"): the person talking (voice / face
recognition) gets their own events and the family's, adds go on their own calendar, an unknown speaker gets everyone's
with whose each is, heads-ups name the person and go to their phone, and the dashboard's set-up helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from littlebot.assistant.who import Who
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.google_calendar import GoogleAuth, Tokens
from littlebot.skills.heads_up import HeadsUp
from littlebot.skills.hub import SkillHub
from littlebot.skills.push import Push
from littlebot.vision.faces import get_faces
from tests.skills.calendar_fakes import CLIENT_ID, MONDAY_10, SECRET, FakeGoogle, make, timed

FAMILY = {"id": "family123@group.calendar.google.com", "summary": "Family", "accessRole": "writer",
          "backgroundColor": "#e67c73", "selected": True}


def google_for(email: str, events: list[dict[str, Any]], family: bool = True) -> FakeGoogle:
    g = FakeGoogle()
    g.calendars = [{"id": email, "summary": email, "primary": True, "accessRole": "owner", "selected": True}] + (
        [dict(FAMILY)] if family else [])
    g.events = {email: events, FAMILY["id"]: [timed("pizza", "Pizza night", 2, 19)]}
    return g


class Router:
    """One fake Google per signed-in person, told apart by their access token."""

    def __init__(self, fakes: dict[str, FakeGoogle], ics: str = "") -> None:
        self.fakes = fakes
        self.ics = ics

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "ics.example.com":
            return httpx.Response(200, text=self.ics) if self.ics else httpx.Response(404)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        fake = self.fakes.get(token) or next(iter(self.fakes.values()))
        return await fake(request)


class FakeAssistant:
    def __init__(self) -> None:
        self.who: Who | None = None

    def speaker(self) -> Who | None:
        return self.who

    def status(self) -> dict[str, Any]:
        return {"active": False}


class FakeNtfy:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})


def sign_in(path: Any, token: str, clock: Any) -> None:
    GoogleAuth(path).store.write(Tokens(client_id=CLIENT_ID, client_secret=SECRET, access_token=token,
                                        refresh_token="RT-secret", expires_at=clock() + 30000, connected_at=clock()))


@pytest.fixture
def family():
    ali_g = google_for("ali@example.com", [timed("dent", "Dentist", 2, 15, location="Main St Clinic")])
    sara_g = google_for("sara@example.com", [timed("yoga", "Yoga", 2, 18)])
    router = Router({"ATali": ali_g, "ATsara": sara_g})
    ctx, cal, _google, clock = make(connected=False)
    cal._transport = httpx.MockTransport(router)
    for acc in cal._accounts.values():  # the household's (not signed in) uses the router too
        acc.auth._transport = cal._transport
        acc.google._transport = cal._transport
    store = get_faces(ctx).store
    ali, sara = store.add_person("Ali", "dad"), store.add_person("Sara", "mom")
    store.add_person("Amara", "baby")
    sign_in(cal._token_path(ali["id"]), "ATali", clock)
    sign_in(cal._token_path(sara["id"]), "ATsara", clock)
    assistant = FakeAssistant()
    ctx.assistant = assistant
    yield ctx, cal, clock, assistant, ali, sara, ali_g, sara_g
    ctx.db.close()


def talking(assistant: FakeAssistant, person: dict[str, Any], sure: bool = True) -> None:
    assistant.who = Who(person["id"], person["name"], person["role"], "voice", sure)


async def test_the_person_talking_gets_their_own_events_and_the_familys(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    assert [p["name"] for p in cal.people()] == ["Ali", "Sara"]  # the baby has no calendar
    await cal.refresh()
    titles = {e.title: e.owner for e in cal._cache}
    assert titles == {"Dentist": ali["id"], "Yoga": sara["id"], "Pizza night": None}  # on both: the family's
    talking(assistant, ali)
    note = cal.context_note()
    assert "Dentist" in note and "Pizza night" in note and "Yoga" not in note
    assert "Ali is talking" in note and "not shown: Sara's 1" in note
    talking(assistant, sara, sure=False)
    note = cal.context_note()
    assert "Yoga" in note and "Dentist" not in note and "probably Sara" in note


async def test_not_sure_who_is_talking_shows_everyones_with_whose(family):
    ctx, cal, *_ = family
    await cal.refresh()
    note = cal.context_note()
    assert "Dentist" in note and "Yoga" in note and "not sure who is talking" in note
    assert "Ali's" in note and "Sara's" in note and "the family's" in note
    assert "Each grown-up has their own calendar" in cal.prompt_note()
    props = cal.tools()[0]["parameters"]["properties"]
    assert "person" in props


async def test_calendar_events_for_someone_else_and_everyone(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    talking(assistant, ali)
    got = json.loads(await cal.run("calendar_events", {"from": "today", "person": "Sara's"}))
    assert [e["title"] for e in got["events"]] == ["Yoga", "Pizza night"] and got["whose"] == "Sara's and the family's"
    assert cal.shown[-1]["title"].startswith("Sara")
    got = json.loads(await cal.run("calendar_events", {"from": "today"}))
    assert [e["title"] for e in got["events"]] == ["Dentist", "Pizza night"] and got["not_shown"] == "Sara's 1"
    got = json.loads(await cal.run("calendar_events", {"from": "today", "person": "everyone"}))
    assert {e["title"]: e["whose"] for e in got["events"]} == {"Pizza night": "the family's", "Dentist": "Ali's",
                                                                "Yoga": "Sara's"}
    assert "isn't on Who's who" in await cal.run("calendar_events", {"from": "today", "person": "Bob"})


async def test_an_add_goes_on_the_calendar_of_whoever_is_talking(family):
    ctx, cal, clock, assistant, ali, sara, ali_g, sara_g = family
    await cal.refresh()
    talking(assistant, sara)
    res = await cal.run("add_calendar_event", {"title": "Hair cut", "start": "2026-11-04T11:00"})
    assert "to Sara's calendar" in res
    assert sara_g.posts() and not ali_g.posts()
    ev = next(e for e in cal._cache if e.title == "Hair cut")
    assert ev.owner == sara["id"] and ev.account == f"p{sara['id']}"
    talking(assistant, ali)
    res = await cal.run("add_calendar_event", {"title": "Oil change", "start": "2026-11-05T09:00", "person": "Sara"})
    assert "Sara's calendar" in res and len(sara_g.posts()) == 2 and not ali_g.posts()
    talking(assistant, ali, sure=False)
    res = await cal.run("add_calendar_event", {"title": "Gym", "start": "2026-11-05T07:00"})
    assert "Ali's calendar" in res and "probably Ali" in res and len(ali_g.posts()) == 1


async def test_nobody_known_and_no_family_calendar_asks_whose(family):
    ctx, cal, clock, assistant, ali, sara, ali_g, sara_g = family
    res = await cal.run("add_calendar_event", {"title": "Party", "start": "2026-11-07"})
    assert res.startswith("error") and "whose calendar" in res and "Ali's" in res and "Sara's" in res
    assert not ali_g.posts() and not sara_g.posts()
    res = await cal.run("add_calendar_event", {"title": "Party", "start": "2026-11-07", "person": "family"})
    assert "no family calendar" in res


async def test_changing_and_cancelling_use_the_owners_sign_in(family):
    ctx, cal, clock, assistant, ali, sara, ali_g, sara_g = family
    await cal.refresh()
    yoga = next(e for e in cal._cache if e.title == "Yoga")
    res = await cal.run("delete_calendar_event", {"id": yoga.key})
    assert "from Sara's calendar" in res and "not deleted yet" in res
    res = await cal.run("delete_calendar_event", {"id": yoga.key, "confirmed": True})
    assert res.startswith("ok: cancelled")
    assert any(m == "DELETE" for m, _p, _b in sara_g.requests) and not any(m == "DELETE" for m, _p, _b in ali_g.requests)
    dent = next(e for e in cal._cache if e.title == "Dentist")
    assert (await cal.run("change_calendar_event", {"id": dent.key, "start": "4 pm"})).startswith("ok: changed")
    assert any(m == "PATCH" for m, _p, _b in ali_g.requests)
    assert next(e for e in cal._cache if e.title == "Dentist").owner == ali["id"]


async def test_the_morning_briefing_is_the_person_talkings(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    await cal.refresh()
    talking(assistant, sara)
    note = cal.briefing_note("morning")
    assert "Yoga" in note and "Dentist" not in note


async def test_a_heads_up_names_the_person_and_goes_to_their_phone(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    ntfy = FakeNtfy()
    push = Push(ctx, transport=httpx.MockTransport(ntfy))
    ctx.config.ntfy_topic = SecretStr("littlebot-family-topic")
    ali_topic = push.make_topic(ali["id"])
    heads = HeadsUp(ctx, now=clock, push=push)
    ctx.skills = SkillHub(ctx, [cal, heads])
    said: list[dict[str, Any]] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: said.append(dict(e.data)))
    clock.set(MONDAY_10.replace(hour=14, minute=31))
    await cal.refresh()
    await heads.tick()
    await heads.drain()
    assert [s["text"] for s in said] == ["Heads up, Ali: Dentist at 3 pm, in 29 minutes, at Main St Clinic."]
    assert [m["topic"] for m in ntfy.sent] == [ali_topic]  # only Ali's phone
    clock.set(MONDAY_10.replace(hour=17, minute=31))
    await cal.refresh()
    await heads.tick()
    await heads.drain()
    assert said[-1]["text"].startswith("Heads up, Sara: Yoga")
    assert ntfy.sent[-1]["topic"] == "littlebot-family-topic"  # Sara has no phone of her own: the family's
    # "send it to my phone": the person talking's own
    talking(assistant, ali)
    assert "Ali's phone" in await heads.run("send_to_phone", {"text": "milk, eggs"})
    assert ntfy.sent[-1]["topic"] == ali_topic
    assert "person" in next(t for t in heads.tools() if t["name"] == "send_to_phone")["parameters"]["properties"]


async def test_family_events_go_to_every_phone(family):
    ctx, cal, *_ , ali, sara, _a, _s = family
    ntfy = FakeNtfy()
    push = Push(ctx, transport=httpx.MockTransport(ntfy))
    ctx.config.ntfy_topic = SecretStr("littlebot-family-topic")
    a, s = push.make_topic(ali["id"]), push.make_topic(sara["id"])
    assert await push.send("Pizza night in 30 minutes", "7 pm")
    assert sorted(m["topic"] for m in ntfy.sent) == sorted(["littlebot-family-topic", a, s])
    assert push.topics(ali["id"]) == [a]
    push.remove_topic(sara["id"])
    assert push.topics(sara["id"]) == ["littlebot-family-topic"]


async def test_a_household_calendar_named_after_someone_is_theirs(family):
    ctx, cal, clock, assistant, ali, sara, ali_g, _s = family
    home_g = google_for("home@example.com", [timed("pta", "PTA meeting", 2, 19)], family=False)
    home_g.calendars.append({"id": "sara-cal", "summary": "Sara's calendar", "accessRole": "owner", "selected": True})
    home_g.events["sara-cal"] = [timed("book", "Book club", 3, 19)]
    cal._transport.handler.fakes["AThome"] = home_g  # type: ignore[attr-defined]
    sign_in(ctx.extras["google_calendar_token_file"], "AThome", clock)
    cal.home.auth.reload()
    await cal.refresh()
    owners = {e.title: e.owner for e in cal._cache}
    assert owners["PTA meeting"] is None and owners["Book club"] == sara["id"]
    cal.set_owner("home:home@example.com", ali["id"])  # the dashboard: the household's main calendar is Ali's
    assert {e.title: e.owner for e in cal._cache}["PTA meeting"] == ali["id"]
    view = await cal.calendars_view()
    assert {c["name"]: c["owner"] for c in view}["Sara's calendar"] == sara["id"]
    # now nobody known: with a household sign-in, it goes there (its main calendar is Ali's now)
    res = await cal.run("add_calendar_event", {"title": "Party", "start": "2026-11-07"})
    assert "to Ali's calendar" in res and home_g.posts()


async def test_a_persons_ical_address_and_forgetting_them(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    cal._transport.handler.ics = ("BEGIN:VCALENDAR\nX-WR-CALNAME:Work\nBEGIN:VEVENT\nUID:w1\n"  # type: ignore[attr-defined]
                                  "DTSTART;TZID=America/Toronto:20261102T130000\n"
                                  "DTEND;TZID=America/Toronto:20261102T140000\nSUMMARY:Standup\nEND:VEVENT\n"
                                  "END:VCALENDAR\n")
    found = await cal.set_ics(sara["id"], "webcal://ics.example.com/sara.ics")
    assert found == 1 and cal._ics_path(sara["id"]).exists()
    assert {e.title: e.owner for e in cal._cache}["Standup"] == sara["id"]
    with pytest.raises(Exception):
        await cal.set_ics(sara["id"], "not an address")
    cal.set_owner(f"p{sara['id']}:ics0", 0)  # the dashboard: that one is the family's
    assert {e.title: e.owner for e in cal._cache}["Standup"] is None
    cal.forget_person(sara["id"])
    assert not cal._ics_path(sara["id"]).exists() and not cal._token_path(sara["id"]).exists()
    assert not any(k.startswith(f"p{sara['id']}") for k in ctx.settings.get("calendar.owners"))
    assert all(e.account != f"p{sara['id']}" for e in cal._cache)


async def test_connecting_a_person_reuses_the_households_google_client(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    url = cal.begin_connect(sara["id"])  # no client given: the one Ali's sign-in was made with
    assert CLIENT_ID in url and SECRET not in url
    with pytest.raises(Exception):
        cal.begin_connect(999)
    assert cal.disconnect(sara["id"]) is True
    await cal.refresh()
    assert "Yoga" not in {e.title for e in cal._cache}


async def test_one_persons_calendar_alone_says_nothing_about_whose(family):
    ctx, cal, clock, assistant, ali, sara, *_ = family
    cal.forget_person(sara["id"])
    await cal.refresh()
    note = cal.context_note()
    assert "Dentist" in note and "Ali's" not in note and "not sure who" not in note
    assert "person" not in cal.tools()[0]["parameters"]["properties"]
    assert "to the Ali" not in await cal.run("add_calendar_event", {"title": "Run", "start": "2026-11-04T07:00"})
