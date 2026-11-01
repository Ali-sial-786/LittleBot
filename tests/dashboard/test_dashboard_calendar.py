"""The dashboard's /api/calendar (littlebot/dashboard/routes/calendar_routes.py): each grown-up's Google sign-in,
iCal address, whose each calendar is and their phone; nothing secret is ever sent back, and deleting someone on
Who's who takes their calendar and phone with them."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from littlebot.skills.agenda import Calendar
from littlebot.skills.heads_up import HeadsUp
from littlebot.skills.hub import SkillHub
from littlebot.skills.push import Push
from littlebot.vision.faces import get_faces
from tests.skills.calendar_fakes import CLIENT_ID, SECRET, Clock, timed
from tests.skills.test_calendar_people import FakeNtfy, Router, google_for, sign_in

ICS = ("BEGIN:VCALENDAR\nX-WR-CALNAME:Work\nBEGIN:VEVENT\nUID:w1\nDTSTART;TZID=America/Toronto:20261102T130000\n"
       "DTEND;TZID=America/Toronto:20261102T140000\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n")


@pytest.fixture
def home(dctx) -> dict[str, Any]:
    dctx.privacy.set(cloud_enabled=True)
    clock = Clock()
    router = Router({"ATali": google_for("ali@example.com", [timed("dent", "Dentist", 2, 15)])}, ics=ICS)
    cal = Calendar(dctx, now=clock, transport=httpx.MockTransport(router))
    store = get_faces(dctx).store
    ali, sara = store.add_person("Ali", "dad"), store.add_person("Sara", "mom")
    sign_in(cal._token_path(ali["id"]), "ATali", clock)
    ntfy = FakeNtfy()
    push = Push(dctx, transport=httpx.MockTransport(ntfy))
    dctx.skills = SkillHub(dctx, [cal, HeadsUp(dctx, now=clock, push=push)])
    return {"cal": cal, "ali": ali["id"], "sara": sara["id"], "ntfy": ntfy, "push": push}


def no_secrets(text: str) -> None:
    for secret in (SECRET, "RT-secret", "ATali", "ics.example.com"):
        assert secret not in text


async def test_needs_the_login(client, home) -> None:
    assert (await client.get("/api/calendar")).status_code in (401, 403)
    assert (await client.post("/api/calendar/phone", json={})).status_code in (401, 403)


async def test_the_page_shows_who_is_connected_and_nothing_secret(authed, home) -> None:
    r = await authed.get("/api/calendar")
    d = r.json()
    assert d["home"] == {"google": False, "connected_at": None, "ics_feeds": 0, "phone": False}
    people = {p["name"]: p for p in d["people"]}
    assert people["Ali"]["google"] is True and people["Sara"]["google"] is False
    assert d["client_known"] is True  # Ali's sign-in: Sara can use the same Google app
    names = {c["name"]: c for c in d["calendars"]}
    assert names["ali@example.com"]["owner"] == home["ali"] and names["Family"]["owner"] is None  # by its name
    no_secrets(r.text)


async def test_connecting_someone_and_a_wrong_person(authed, home) -> None:
    r = await authed.post("/api/calendar/google/begin", json={"person_id": home["sara"]})
    assert r.status_code == 200 and CLIENT_ID in r.json()["url"] and SECRET not in r.text
    r = await authed.post("/api/calendar/google/begin", json={"person_id": 999})
    assert r.status_code == 400
    r = await authed.post("/api/calendar/google/finish", json={"person_id": home["sara"], "url": "nonsense"})
    assert r.status_code == 400
    r = await authed.post("/api/calendar/google/disconnect", json={"person_id": home["ali"]})
    assert r.json()["was_connected"] is True
    assert {p["name"]: p["google"] for p in r.json()["people"]} == {"Ali": False, "Sara": False}


async def test_offline_or_no_cloud_says_why(authed, home, dctx) -> None:
    dctx.privacy.set(cloud_enabled=False)
    r = await authed.post("/api/calendar/google/begin", json={"person_id": home["sara"]})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]
    r = await authed.put("/api/calendar/ics", json={"person_id": home["sara"], "url": "https://ics.example.com/a.ics"})
    assert r.status_code == 403


async def test_an_ical_address_is_checked_kept_and_never_sent_back(authed, home) -> None:
    sara = home["sara"]
    r = await authed.put("/api/calendar/ics", json={"person_id": sara, "url": "https://ics.example.com/sara.ics"})
    assert r.status_code == 200 and r.json()["events"] == 1
    assert {p["name"]: p["ics_feeds"] for p in r.json()["people"]}["Sara"] == 1
    no_secrets(r.text)
    no_secrets((await authed.get("/api/calendar")).text)
    bad = await authed.put("/api/calendar/ics", json={"person_id": sara, "url": "not an address"})
    assert bad.status_code == 400
    r = await authed.post("/api/calendar/ics/remove", json={"person_id": sara})
    assert r.json()["removed"] is True and {p["name"]: p["ics_feeds"] for p in r.json()["people"]}["Sara"] == 0


async def test_saying_whose_a_calendar_is(authed, home) -> None:
    key = f"p{home['ali']}:family123@group.calendar.google.com"
    r = await authed.put("/api/calendar/owner", json={"key": key, "person_id": home["sara"]})
    assert {c["name"]: c["owner"] for c in r.json()["calendars"]}["Family"] == home["sara"]
    r = await authed.put("/api/calendar/owner", json={"key": key, "person_id": 0})
    assert {c["name"]: c["owner"] for c in r.json()["calendars"]}["Family"] is None  # the family's again
    r = await authed.put("/api/calendar/owner", json={"key": key, "person_id": 999})
    assert r.status_code == 400


async def test_a_phone_topic_is_shown_once(authed, home) -> None:
    ali = home["ali"]
    r = await authed.post("/api/calendar/phone", json={"person_id": ali})
    d = r.json()
    topic = d["topic"]
    assert d["ok"] and d["test_sent"] and topic.startswith("littlebot-") and d["server"]
    assert [m["topic"] for m in home["ntfy"].sent] == [topic]
    after = await authed.get("/api/calendar")
    assert topic not in after.text and {p["name"]: p["phone"] for p in after.json()["people"]}["Ali"] is True
    r = await authed.post("/api/calendar/phone/test", json={"person_id": ali})
    assert r.json() == {"ok": True, "detail": "Sent."}
    r = await authed.post("/api/calendar/phone/remove", json={"person_id": ali})
    assert r.json()["removed"] is True
    assert (await authed.post("/api/calendar/phone/test", json={"person_id": ali})).status_code == 409
    assert (await authed.post("/api/calendar/phone", json={"person_id": 999})).status_code == 404


async def test_deleting_someone_takes_their_calendar_and_phone(authed, home) -> None:
    ali, cal, push = home["ali"], home["cal"], home["push"]
    await authed.post("/api/calendar/phone", json={"person_id": ali})
    assert cal._token_path(ali).exists() and push.topic(ali)
    assert (await authed.delete(f"/api/people/{ali}")).status_code == 200
    assert not cal._token_path(ali).exists() and not push.topic(ali)


async def test_no_calendar_skill(authed, dctx) -> None:
    dctx.skills = SkillHub(dctx, [])
    r = await authed.get("/api/calendar")
    assert r.status_code == 503 and "skills.calendar" in json.dumps(r.json())
