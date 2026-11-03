"""Dashboard: Calendars (Settings page) - add each person's calendars by their secret link, check, rename, remove."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from calendar_fakes import ALI_HOME, ALI_WORK, SAM_HOME, FakeGoogle, calendar_service  # noqa: E402


@pytest.fixture
def web(dctx) -> FakeGoogle:
    w = FakeGoogle()
    dctx.config.info.timezone = "America/Toronto"
    calendar_service(dctx, w)
    return w


def secrets(*links: str) -> list[str]:
    return [link.split("/private-")[1].split("/")[0] for link in links]


async def test_needs_a_parent_session(client, web):
    assert (await client.get("/api/calendars")).status_code == 401
    r = await client.post("/api/calendars", json={"person": "Ali", "label": "Work", "link": ALI_WORK})
    assert r.status_code in (401, 403) and not web.requests


async def test_a_person_with_a_work_and_a_personal_calendar(authed, web, dctx):
    dctx.privacy.set(cloud_enabled=True)
    assert (await authed.get("/api/calendars")).json()["calendars"] == []
    r = await authed.post("/api/calendars", json={"person": "Ali", "label": "Work", "link": ALI_WORK})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["calendar"]["label"] == "Work" and d["calendar"]["link"] == "calendar.google.com, secret address"
    assert d["check"] == {"ok": True, "problem": None, "name": "Ali Work", "events_soon": 5,
                          "next": "Standup (long): today 09:30"}
    r = await authed.post("/api/calendars", json={"person": "Ali", "label": "Personal",
                                                  "link": ALI_HOME.replace("https://", "webcal://")})
    assert r.status_code == 200, r.text
    await authed.post("/api/calendars", json={"person": "Sam", "label": "Personal", "link": SAM_HOME})
    d = (await authed.get("/api/calendars")).json()
    assert [(c["person"], c["label"], c["ok"]) for c in d["calendars"]] == [
        ("Ali", "Work", True), ("Ali", "Personal", True), ("Sam", "Personal", True)]
    assert d["people"] == ["Ali", "Sam"] and d["count"] == 3 and d["max"] == 20 and d["installed"] is True
    for secret in secrets(ALI_WORK, ALI_HOME, SAM_HOME):
        assert secret not in r.text and secret not in str(d)
    today = (await authed.get("/api/calendars/agenda")).json()
    assert today["summary"] == "09:30-10:30 Standup (long) (Ali, Work)\n14:00-14:30 Planning call (Ali, Work)"
    friday = (await authed.get("/api/calendars/agenda?day=friday")).json()
    assert friday["summary"] == "18:00-20:00 Family dinner (Ali and Sam, Personal)"


async def test_wrong_links_say_what_to_do(authed, web, dctx):
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.post("/api/calendars", json={"person": "Ali", "label": "Work",
                                                  "link": "https://calendar.google.com/calendar/r"})
    assert r.status_code == 422 and "Secret address in iCal format" in r.json()["detail"]
    r = await authed.post("/api/calendars", json={"person": "Ali", "label": "Work",
                                                  "link": "https://calendar.google.com/calendar/ical/x/private-1/basic.ics"})
    assert r.status_code == 422 and "no calendar at that link" in r.json()["detail"]
    r = await authed.post("/api/calendars", json={"person": "<b>", "label": "Work", "link": ALI_WORK})
    assert r.status_code == 422
    assert (await authed.get("/api/calendars")).json()["calendars"] == []


async def test_with_cloud_off_the_calendar_is_kept_but_not_read(authed, web):
    r = await authed.post("/api/calendars", json={"person": "Ali", "label": "Work", "link": ALI_WORK})
    assert r.status_code == 200
    d = r.json()
    assert d["check"]["ok"] is False and d["check"]["problem"] == "cloud_off" and d["blocked"] == "cloud_off"
    assert d["calendar"]["ok"] is None and not web.requests
    assert (await authed.post(f"/api/calendars/{d['calendar']['id']}/check")).json()["problem"] == "cloud_off"
    assert (await authed.get("/api/calendars/agenda")).status_code == 403


async def test_check_rename_and_remove(authed, web, dctx):
    dctx.privacy.set(cloud_enabled=True)
    cal = (await authed.post("/api/calendars", json={"person": "Ali", "label": "Work", "link": ALI_WORK})).json()
    cal_id = cal["calendar"]["id"]
    web.status[ALI_WORK] = 404  # the secret address was reset in Google
    d = (await authed.post(f"/api/calendars/{cal_id}/check")).json()
    assert d["ok"] is False and d["problem"] == "not_found" and d["calendar"]["ok"] is False
    assert "changes if it was reset" in d["calendar"]["problem_detail"]
    r = await authed.put(f"/api/calendars/{cal_id}", json={"label": "Office", "link": ALI_HOME})
    assert r.status_code == 200 and r.json()["calendar"]["label"] == "Office" and r.json()["check"]["ok"] is True
    r = await authed.put(f"/api/calendars/{cal_id}", json={"person": "Alison"})
    assert r.json()["calendar"]["person"] == "Alison" and r.json()["check"] is None
    assert (await authed.post("/api/calendars", json={"person": "alison", "label": "office",
                                                      "link": SAM_HOME})).status_code == 422  # same name
    assert (await authed.delete(f"/api/calendars/{cal_id}")).json()["calendars"] == []
    assert (await authed.delete(f"/api/calendars/{cal_id}")).status_code == 404
    assert (await authed.post(f"/api/calendars/{cal_id}/check")).status_code == 404
