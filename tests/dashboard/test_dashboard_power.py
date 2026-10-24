"""Dashboard: the battery (``/api/power``): behind the login, "not fitted" on mains, the state, banners, settings."""

from __future__ import annotations

import pytest

from littlebot.core.events import Topics
from littlebot.power.monitor import SimulatedMonitor, cell_for_percent
from littlebot.power.protocol import Reading
from littlebot.power.service import PowerService

from dashboard_fakes import make_client


def _reading(percent: float, *, charging: bool = False, t: float = 26.5) -> Reading:
    c = cell_for_percent(percent) + (0.1 if charging else 0.0)
    return Reading(voltage=round(2 * c, 3), cell1=round(c, 3), temperature_c=t, charging=charging, charged=False,
                   uptime_s=5, fw=1)


@pytest.fixture
def power(dctx):
    """A fitted battery, fed by hand (``power.monitor: none`` so nothing is read from a port)."""
    svc = PowerService(dctx)
    svc.process(_reading(64))
    dctx.power = svc
    return svc


async def test_power_needs_the_parent_login(client):
    assert (await client.get("/api/power")).status_code == 401
    assert (await client.post("/api/power/show")).status_code in (401, 403)


async def test_mains_power_is_not_fitted(authed, dctx):
    d = (await authed.get("/api/power")).json()
    assert d["fitted"] is False and d["detail"] == "not fitted" and d["percent"] is None
    status = (await authed.get("/api/status")).json()
    assert status["power"]["fitted"] is False and status["alerts"] == []
    r = await authed.post("/api/power/show")
    assert r.status_code == 409 and "mains" in r.json()["detail"]
    assert (await authed.post("/api/power/simulate", json={"percent": 10})).status_code == 409
    # a power service with no monitor plugged in: the same
    dctx.power = PowerService(dctx)
    d = (await authed.get("/api/power")).json()
    assert d["fitted"] is False and d["detail"] == "not fitted"


async def test_a_fitted_battery_its_state_and_the_face_sign(authed, dctx, power):
    d = (await authed.get("/api/power")).json()
    assert d["fitted"] and d["connected"] and d["percent"] == 64 and d["temperature_c"] == 26.5
    assert d["voltage"] == pytest.approx(2 * cell_for_percent(64), abs=0.01) and d["charging"] is False
    assert d["settings"]["low_percent"] == 20 and d["settings"]["critical_percent"] == 7
    assert d["settings"]["poweroff_allowed"] is False  # the test context isn't the Pi
    status = (await authed.get("/api/status")).json()
    assert status["power"]["percent"] == 64
    seen = []
    dctx.bus.subscribe(Topics.POWER_SHOW, seen.append)
    r = await authed.post("/api/power/show")
    assert r.status_code == 200 and r.json()["ok"] is True and seen and seen[0].get("percent") == 64


async def test_low_hot_and_empty_become_banners(authed, dctx, power):
    dctx.bus.publish(Topics.POWER_LOW, {"percent": 19}, source="power")
    dctx.bus.publish(Topics.POWER_HOT, {"temperature_c": 52.0, "level": "warm"}, source="power")
    dctx.bus.publish(Topics.POWER_CRITICAL, {"percent": 6, "reason": "the battery is at 6%"}, source="power")
    alerts = (await authed.get("/api/status")).json()["alerts"]
    assert [a["title"] for a in alerts] == ["Battery low", "Battery is hot", "Battery empty"]
    assert "19%" in alerts[0]["detail"] and alerts[0]["kind"] == "power"
    assert any(a["kind"] == "attention" and "empty" in a["detail"] for a in alerts)
    assert any("52" in a["detail"] for a in alerts)
    # ordinary news (charging, a reading) is no banner
    dctx.bus.publish(Topics.POWER_CHARGING, {"percent": 6}, source="power")
    dctx.bus.publish(Topics.POWER_BATTERY, {"percent": 6}, source="power")
    assert len((await authed.get("/api/status")).json()["alerts"]) == len(alerts)


async def test_settings_are_validated_and_applied_live(authed, dctx, power):
    r = await authed.put("/api/power/settings", json={"low_percent": 30, "announce_low": False})
    assert r.status_code == 200, r.text
    assert dctx.settings.get("power.low_percent") == 30 and dctx.settings.get("power.announce_low") is False
    assert r.json()["settings"]["low_percent"] == 30 and power._thresholds().low_percent == 30
    assert (await authed.put("/api/power/settings", json={"critical_percent": 30})).status_code == 422  # not below low
    assert (await authed.put("/api/power/settings", json={"low_percent": 90})).status_code == 422
    assert (await authed.put("/api/power/settings", json={})).status_code == 422
    r = await authed.put("/api/power/settings", json={"shutdown_when_empty": False})
    assert r.status_code == 200 and dctx.settings.get("power.shutdown_when_empty") is False


async def test_the_simulated_pack_can_be_set(authed, dctx):
    svc = PowerService(dctx, source=SimulatedMonitor(percent=50, interval_s=60))
    dctx.power = svc
    await svc.start()
    try:
        r = await authed.post("/api/power/simulate", json={"percent": 15, "charging": True, "temperature_c": 30})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["kind"] == "simulated" and d["charging"] is True and d["temperature_c"] == 30
        assert d["percent"] == pytest.approx(15, abs=1)
        assert (await authed.post("/api/power/simulate", json={"percent": 150})).status_code == 422
    finally:
        await svc.stop()


async def test_csrf_is_needed_to_change_battery_settings(app):
    from dashboard_fakes import do_setup

    async with make_client(app) as c:
        await do_setup(c, app)
        c.headers.pop("x-csrf-token", None)
        assert (await c.put("/api/power/settings", json={"low_percent": 25})).status_code == 403
