"""Dashboard: Weather & time (Settings page) - where home is, where that came from, units, the weather now."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from weather_fakes import FakeWeb, weather_service  # noqa: E402


@pytest.fixture
def web(dctx) -> FakeWeb:
    w = FakeWeb()
    weather_service(dctx, w)
    return w


async def test_needs_a_parent_session(client, web):
    assert (await client.get("/api/info")).status_code == 401
    assert (await client.put("/api/info", json={"location": "Paris"})).status_code in (401, 403)


async def test_with_cloud_off_nothing_is_looked_up(authed, web):
    r = await authed.get("/api/info?lookup=true")
    assert r.status_code == 200
    d = r.json()
    w = d["weather"]
    assert w["source"] == "internet" and w["source_detail"] == "from your internet connection, approximate"
    assert w["location"] is None and w["found"] is False and w["blocked"] == "cloud_off"
    assert w["units"] == "auto" and d["web_search"] is True
    assert {"time_24h", "day", "date", "timezone"} <= set(d["time"])
    assert not web.requests


async def test_home_from_the_internet_connection(authed, web, dctx):
    dctx.privacy.set(cloud_enabled=True)
    assert (await authed.get("/api/info")).json()["weather"]["location"] is None  # not looked up without lookup
    w = (await authed.get("/api/info?lookup=true")).json()["weather"]
    assert w["location"] == "Toronto, Ontario" and w["source"] == "internet" and w["found"] is True
    assert w["units_in_use"] == "celsius" and w["timezone"] == "America/Toronto"
    assert web.hosts() == ["ipinfo.io"]
    now = (await authed.get("/api/info/weather")).json()
    assert now["summary"].startswith("Toronto, Ontario: now 12°C (feels like 9°C), sunny.")
    assert now["report"]["forecast"][0]["day"] == "today"


async def test_the_parent_changes_the_place_and_units(authed, web, dctx):
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.put("/api/info", json={"location": "Paris", "units": "fahrenheit"})
    assert r.status_code == 200, r.text
    w = r.json()["weather"]
    assert w["location"] == "Paris, Île-de-France, France" and w["source"] == "dashboard"
    assert w["source_detail"] == "set on the dashboard" and w["query"] == "Paris" and w["units_in_use"] == "fahrenheit"
    assert dctx.settings.get("info.location")["latitude"] == pytest.approx(48.85341)
    assert dctx.settings.get("info.units") == "fahrenheit"
    assert "°F" in (await authed.get("/api/info/weather")).json()["summary"]
    # back to automatic: the internet connection
    w = (await authed.put("/api/info", json={"location": "", "units": "auto"})).json()["weather"]
    assert w["source"] == "internet" and w["location"] == "Toronto, Ontario"
    assert dctx.settings.get("info.location") == {}


async def test_coordinates_work_even_with_cloud_off(authed, web, dctx):
    r = await authed.put("/api/info", json={"location": "43.65, -79.38"})
    assert r.status_code == 200
    w = r.json()["weather"]
    assert w["location"] == "43.65, -79.38" and w["source"] == "dashboard" and not web.requests


async def test_problems_are_explained(authed, web, dctx):
    r = await authed.put("/api/info", json={"location": "Paris"})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]
    assert (await authed.get("/api/info/weather")).status_code == 403
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.put("/api/info", json={"location": "Narnia"})
    assert r.status_code == 404 and "Couldn't find “Narnia”" in r.json()["detail"]
    assert dctx.settings.get("info.location") == {}  # nothing changed
    assert (await authed.put("/api/info", json={"units": "kelvin"})).status_code == 422
    assert (await authed.put("/api/info", json={"location": "x" * 121})).status_code == 422
    assert (await authed.put("/api/info", json={"place": "Paris"})).status_code == 422  # unknown field


async def test_no_home_found_is_a_plain_answer(authed, dctx):
    dctx.privacy.set(cloud_enabled=True)
    weather_service(dctx, FakeWeb(ip_ok=()))
    w = (await authed.get("/api/info?lookup=true")).json()["weather"]
    assert w["found"] is False and w["source"] == "internet"
    r = await authed.get("/api/info/weather")
    assert r.status_code == 409 and "Type a place" in r.json()["detail"]
