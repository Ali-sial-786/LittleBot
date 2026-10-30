"""The smart-home skill (littlebot/skills/home.py) against a fake Home Assistant (httpx.MockTransport): when it is
offered, listing devices, finding them by name, every action, confirmations, errors, and that the token never
shows."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from littlebot.skills import home as H
from littlebot.skills.home import SmartHome, is_lan, kind_of, name_score, service_for, words
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context

TOKEN = "secret-ha-token-123"
URL = "http://homeassistant.local:8123"

STATES: list[dict[str, Any]] = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light", "brightness": 204}},
    {"entity_id": "light.kitchen_ceiling", "state": "off", "attributes": {"friendly_name": "Kitchen Ceiling Light"}},
    {"entity_id": "light.living_room", "state": "off", "attributes": {"friendly_name": "Living Room Lamp"}},
    {"entity_id": "switch.kettle", "state": "off", "attributes": {"friendly_name": "Kettle Plug"}},
    {"entity_id": "fan.bedroom", "state": "on", "attributes": {"friendly_name": "Bedroom Fan", "percentage": 50}},
    {"entity_id": "climate.hallway", "state": "heat",
     "attributes": {"friendly_name": "Thermostat", "current_temperature": 19.5, "temperature": 21}},
    {"entity_id": "cover.living_room_blinds", "state": "closed",
     "attributes": {"friendly_name": "Living Room Blinds", "current_position": 0}},
    {"entity_id": "cover.garage_door", "state": "closed",
     "attributes": {"friendly_name": "Garage Door", "device_class": "garage"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "media_player.living_room_tv", "state": "playing",
     "attributes": {"friendly_name": "Living Room TV", "media_title": "The News"}},
    {"entity_id": "scene.movie_night", "state": "2026-10-29T20:00:00", "attributes": {"friendly_name": "Movie Night"}},
    {"entity_id": "script.bedtime", "state": "off", "attributes": {"friendly_name": "Bedtime"}},
    {"entity_id": "sensor.outside", "state": "7", "attributes": {"friendly_name": "Outside Temperature"}},
    {"entity_id": "automation.morning", "state": "on", "attributes": {"friendly_name": "Morning lights"}},
    {"bad": "entry"},
]


class FakeHA:
    """GET /api/states and POST /api/services/<domain>/<service>, like Home Assistant."""

    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.states = copy.deepcopy(STATES)
        self.status = status
        self.delay = delay
        self.calls: list[tuple[str, str, Any]] = []  # (method, path, body)
        self.auth: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("Authorization", ""))
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=self.states)
        if request.method == "POST" and request.url.path.startswith("/api/services/"):
            _, _, _, domain, service = request.url.path.split("/")
            eid = (body or {}).get("entity_id")
            changed = []
            for s in self.states:
                if s.get("entity_id") == eid:
                    if service in ("turn_on", "open_cover", "unlock"):
                        s["state"] = {"open_cover": "open", "unlock": "unlocked"}.get(service, "on")
                    elif service in ("turn_off", "close_cover", "lock"):
                        s["state"] = {"close_cover": "closed", "lock": "locked"}.get(service, "off")
                    elif service == "toggle":
                        s["state"] = "off" if s["state"] == "on" else "on"
                    changed.append(s)
            return httpx.Response(200, json=changed)
        return httpx.Response(404, json={"message": "not found"})

    def services(self) -> list[tuple[str, Any]]:
        return [(p.removeprefix("/api/services/"), b) for m, p, b in self.calls if m == "POST"]


def make(url: str = URL, token: str | None = TOKEN, *, ha: FakeHA | None = None, mode: str = "simulation",
         cloud: bool = True) -> tuple[Any, SmartHome, FakeHA]:
    ctx = make_test_context(mode=mode, skills={"home_assistant_url": url})
    if token is not None:
        ctx.config.home_assistant_token = SecretStr(token)
    if cloud and mode != "offline":
        ctx.privacy.set(cloud_enabled=True)
    ha = ha or FakeHA()
    return ctx, SmartHome(ctx, transport=httpx.MockTransport(ha)), ha


@pytest.fixture
def env():
    ctx, skill, ha = make()
    yield ctx, skill, ha
    ctx.db.close()


async def control(skill: SmartHome, target: str, action: str, **kw: Any) -> str:
    return await skill.run("home_control", {"target": target, "action": action, **kw})


# ------------------------------------------------------------------ when it is offered
def test_offered_only_when_set_up_and_allowed():
    ctx, skill, _ = make()
    assert skill.available() and skill.status()["set_up"] and [t["name"] for t in skill.tools()] == [
        "home_devices", "home_control"]
    ctx.privacy.set(cloud_enabled=False)
    assert skill.available()  # on the home network: the cloud switch doesn't matter
    ctx.db.close()
    for url, token in (("", TOKEN), (URL, None), (URL, ""), ("ftp://x", TOKEN), ("not a url", TOKEN)):
        ctx, skill, _ = make(url, token)
        assert not skill.available() and skill.blocked_reason() == "not_set_up"
        ctx.db.close()
    ctx, skill, _ = make(mode="offline")
    assert skill.blocked_reason() == "offline"
    ctx.db.close()
    ctx, skill, _ = make("https://abc.ui.nabu.casa", cloud=False)
    assert skill.blocked_reason() == "cloud_off"  # on the internet: only with cloud communication on
    ctx.privacy.set(cloud_enabled=True)
    assert skill.available()
    ctx.db.close()


def test_lan_addresses():
    for u in ("http://homeassistant.local:8123", "http://192.168.1.20:8123", "http://10.0.0.5", "http://127.0.0.1",
              "http://homeassistant:8123", "http://[fe80::1]:8123", "http://ha.lan"):
        assert is_lan(u), u
    for u in ("https://abc.ui.nabu.casa", "https://8.8.8.8", "https://home.example.com", ""):
        assert not is_lan(u), u


async def test_the_skill_hub_hides_it_until_set_up():
    ctx, skill, _ = make(token=None)
    try:
        hub = SkillHub(ctx, [skill])
        assert hub.specs() == [] and hub.prompt_notes() == ""
        assert (await skill.run("home_devices", {})).startswith("error: the smart home isn't set up")
        ctx.config.home_assistant_token = SecretStr(TOKEN)
        assert [s["name"] for s in hub.specs()] == ["home_devices", "home_control"]
        assert "confirmed true" in hub.prompt_notes()
    finally:
        ctx.db.close()


# ------------------------------------------------------------------ home_devices
async def test_devices_listed_filtered_and_cached(env):
    ctx, skill, ha = env
    out = json.loads(await skill.run("home_devices", {}))
    names = [d["name"] for d in out["devices"]]
    assert out["count"] == 12 and "Outside Temperature" not in names and "Morning lights" not in names
    assert names[:3] == ["Kitchen Ceiling Light", "Kitchen Light", "Living Room Lamp"]  # lights first, by name
    kitchen = next(d for d in out["devices"] if d["name"] == "Kitchen Light")
    assert kitchen == {"name": "Kitchen Light", "kind": "light", "state": "on", "brightness_percent": 80}
    thermostat = next(d for d in out["devices"] if d["kind"] == "climate")
    assert thermostat["temperature_now"] == 19.5 and thermostat["target_temperature"] == 21
    assert all("entity_id" not in d for d in out["devices"])
    out = json.loads(await skill.run("home_devices", {"area_or_kind": "lights"}))
    assert [d["name"] for d in out["devices"]] == ["Kitchen Ceiling Light", "Kitchen Light", "Living Room Lamp"]
    out = json.loads(await skill.run("home_devices", {"area_or_kind": "the kitchen lights"}))
    assert [d["name"] for d in out["devices"]] == ["Kitchen Ceiling Light", "Kitchen Light"]
    out = json.loads(await skill.run("home_devices", {"area_or_kind": "living room"}))
    assert {d["kind"] for d in out["devices"]} == {"light", "cover", "media_player"}
    out = json.loads(await skill.run("home_devices", {"area_or_kind": "thermostat"}))
    assert [d["name"] for d in out["devices"]] == ["Thermostat"]
    assert (await skill.run("home_devices", {"area_or_kind": "garden"})).startswith("ok: no devices match 'garden'")
    assert [c[1] for c in ha.calls] == ["/api/states"]  # one request: cached for 30 s
    assert set(ha.auth) == {f"Bearer {TOKEN}"}


async def test_devices_show_the_home_card(env):
    ctx, skill, _ = env
    shown: list[dict[str, Any]] = []

    class Screens:
        async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
            shown.append(spec)
            return {"ok": True}

    ctx.screens = Screens()
    await skill.run("home_devices", {"area_or_kind": "kitchen lights"})
    assert shown[0]["card"] == "home" and shown[0]["title"] == "Kitchen lights"
    assert [d["entity_id"] for d in shown[0]["devices"]] == ["light.kitchen_ceiling", "light.kitchen"]


# ------------------------------------------------------------------ home_control
async def test_turning_things_on_and_off(env):
    ctx, skill, ha = env
    assert await control(skill, "kitchen light", "turn_off") == "ok: turned off Kitchen Light"
    assert await control(skill, "Kettle Plug", "turn_on") == "ok: turned on Kettle Plug"
    assert await control(skill, "living room lamp", "toggle") == "ok: switched Living Room Lamp on"
    assert await control(skill, "switch.kettle", "turn_off") == "ok: turned off Kettle Plug"  # an entity id
    assert ha.services() == [("light/turn_off", {"entity_id": "light.kitchen"}),
                             ("switch/turn_on", {"entity_id": "switch.kettle"}),
                             ("light/toggle", {"entity_id": "light.living_room"}),
                             ("switch/turn_off", {"entity_id": "switch.kettle"})]


async def test_values_brightness_temperature_color_position(env):
    ctx, skill, ha = env
    assert await control(skill, "kitchen light", "set_brightness", value=40) == "ok: set Kitchen Light to 40%"
    assert await control(skill, "kitchen light", "set_brightness", value="25%") == "ok: set Kitchen Light to 25%"
    assert await control(skill, "kitchen light", "set_brightness", value=0) == "ok: turned off Kitchen Light"
    assert await control(skill, "bedroom fan", "set_brightness", value=70) == "ok: set Bedroom Fan to 70%"
    assert await control(skill, "thermostat", "set_temperature", value=21.4) == "ok: set Thermostat to 21.5°"
    assert await control(skill, "the heating", "set_temperature", value="22") == "ok: set Thermostat to 22°"
    assert await control(skill, "kitchen light", "set_color", value="Blue") == "ok: Kitchen Light is now blue"
    assert await control(skill, "kitchen light", "set_color", value="warm white") == \
        "ok: Kitchen Light is now warm white"
    assert await control(skill, "blinds", "open", value=30) == "ok: set Living Room Blinds to 30% open"
    assert await control(skill, "living room blinds", "close") == "ok: closed Living Room Blinds"
    assert await control(skill, "movie night", "activate") == "ok: activated Movie Night"
    assert await control(skill, "bedtime", "activate") == "ok: started Bedtime"
    assert ha.services() == [
        ("light/turn_on", {"entity_id": "light.kitchen", "brightness_pct": 40}),
        ("light/turn_on", {"entity_id": "light.kitchen", "brightness_pct": 25}),
        ("light/turn_off", {"entity_id": "light.kitchen"}),
        ("fan/set_percentage", {"entity_id": "fan.bedroom", "percentage": 70}),
        ("climate/set_temperature", {"entity_id": "climate.hallway", "temperature": 21.5}),
        ("climate/set_temperature", {"entity_id": "climate.hallway", "temperature": 22.0}),
        ("light/turn_on", {"entity_id": "light.kitchen", "color_name": "blue"}),
        ("light/turn_on", {"entity_id": "light.kitchen", "color_temp_kelvin": 2700}),
        ("cover/set_cover_position", {"entity_id": "cover.living_room_blinds", "position": 30}),
        ("cover/close_cover", {"entity_id": "cover.living_room_blinds"}),
        ("scene/turn_on", {"entity_id": "scene.movie_night"}),
        ("script/turn_on", {"entity_id": "script.bedtime"}),
    ]


async def test_bad_values_are_refused_before_asking_home_assistant(env):
    ctx, skill, ha = env
    for target, action, value in (("kitchen light", "set_brightness", 140), ("kitchen light", "set_brightness", None),
                                  ("kitchen light", "set_brightness", True), ("thermostat", "set_temperature", 300),
                                  ("thermostat", "set_temperature", "warm"), ("kitchen light", "set_color", "plaid"),
                                  ("blinds", "open", 101)):
        assert (await control(skill, target, action, value=value)).startswith("error:"), (target, action, value)
    assert await control(skill, "", "turn_on") == "error: say which device"
    assert (await control(skill, "kitchen light", "explode")).startswith("error: unknown action")
    assert ha.services() == []


async def test_names_ambiguity_and_wrong_kinds(env):
    ctx, skill, ha = env
    ask = await control(skill, "kitchen", "turn_on")
    assert ask.startswith("ask: which one:") and "'Kitchen Light'" in ask and "'Kitchen Ceiling Light'" in ask
    assert await control(skill, "kitchen ceiling", "turn_on") == "ok: turned on Kitchen Ceiling Light"
    assert (await control(skill, "the garden gnome", "turn_on")).startswith("error: no device called")
    assert (await control(skill, "front door", "turn_off")) == \
        "error: Front Door is a lock: it can't turn off; say lock or unlock"
    assert await control(skill, "thermostat", "set_color", value="red") == \
        "error: Thermostat is a thermostat: it can't set color"
    assert await control(skill, "kitchn light", "turn_off") == "ok: turned off Kitchen Light"  # a slip of the ear
    assert ha.services() == [("light/turn_on", {"entity_id": "light.kitchen_ceiling"}),
                             ("light/turn_off", {"entity_id": "light.kitchen"})]


async def test_locks_and_garage_doors_need_a_clear_yes(env):
    ctx, skill, ha = env
    q = await control(skill, "front door", "unlock")
    assert q.startswith("ask: do you want me to unlock Front Door?") and "confirmed true" in q
    assert (await control(skill, "front door", "unlock", confirmed="yes")).startswith("ask:")  # only a real true
    assert (await control(skill, "front door", "lock")).startswith("ask: do you want me to lock Front Door?")
    assert (await control(skill, "garage", "open")).startswith("ask: do you want me to open Garage Door?")
    assert ha.services() == []
    assert await control(skill, "front door", "unlock", confirmed=True) == "ok: unlocked Front Door"
    assert await control(skill, "front door", "lock", confirmed=True) == "ok: locked Front Door"
    assert await control(skill, "garage door", "open", confirmed=True) == "ok: opened Garage Door"
    assert await control(skill, "garage door", "close") == "ok: closed Garage Door"  # closing is always fine
    assert [s for s, _ in ha.services()] == ["lock/unlock", "lock/lock", "cover/open_cover", "cover/close_cover"]


async def test_errors_are_short_kinds_and_never_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    for status, words_ in ((401, "refused LittleBot's token"), (404, "doesn't know that"), (500, "had a problem"),
                           (400, "couldn't do that")):
        ctx, skill, _ = make(ha=FakeHA(status=status))
        try:
            assert (await skill.run("home_devices", {})) == f"error: {H.ERRORS[H.HomeError(_kind(status)).kind]}"
            assert words_ in await skill.run("home_devices", {})
        finally:
            ctx.db.close()
    ctx, skill, _ = make(ha=FakeHA(delay=0.3))
    try:
        ctx.config.skills.home_timeout_seconds = 0.05
        assert await skill.run("home_devices", {}) == "error: the smart home didn't answer in time"
    finally:
        ctx.db.close()

    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ctx = make_test_context(skills={"home_assistant_url": URL})
    ctx.config.home_assistant_token = SecretStr(TOKEN)
    try:
        skill = SmartHome(ctx, transport=httpx.MockTransport(down))
        assert (await skill.run("home_control", {"target": "x", "action": "turn_on"})).startswith(
            "error: the smart home couldn't be reached")
    finally:
        ctx.db.close()

    async def junk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    ctx = make_test_context(skills={"home_assistant_url": URL})
    ctx.config.home_assistant_token = SecretStr(TOKEN)
    try:
        skill = SmartHome(ctx, transport=httpx.MockTransport(junk))
        assert "couldn't read" in await skill.run("home_devices", {})
        assert TOKEN not in json.dumps(skill.status())
    finally:
        ctx.db.close()
    assert TOKEN not in caplog.text


def _kind(status: int) -> str:
    return {401: "auth", 404: "not_found", 500: "server", 400: "bad_request"}[status]


async def test_toggle_for_the_card(env):
    ctx, skill, ha = env
    assert await skill.toggle("light.kitchen") == "off"
    assert await skill.toggle("switch.kettle") == "on"
    with pytest.raises(H.HomeError):
        await skill.toggle("lock.front_door")  # never by a tap
    out = json.loads(await skill.run("home_devices", {"area_or_kind": "kettle"}))
    assert out["devices"][0]["state"] == "on"  # what changed shows at once
    await skill.stop()


# ------------------------------------------------------------------ helpers
def test_words_kinds_and_scores():
    assert words("The Kitchen's Lights!") == ["the", "kitchen", "light"]
    assert kind_of(words("kitchen lights")) == ("light", ["kitchen"])
    assert kind_of(words("garage door")) == ("cover", [])
    assert kind_of(words("living room")) == (None, ["living", "room"])
    assert name_score("kitchen light", "Kitchen Light") == 1.0
    assert name_score("kitchen", "Kitchen Light") > name_score("kitchen", "Kitchen Ceiling Light")
    assert name_score("garden", "Kitchen Light") < H.MATCH_MIN
    assert H.card_title("kitchen lights") == "Kitchen lights" and H.card_title("") == "Smart home"
    assert H.card_title("locks") == "Locks" and H.card_title("bedroom") == "Bedroom"
    d = H.device({"entity_id": "light.x", "state": "on", "attributes": {}})
    assert d["name"] == "X" and service_for(d, "turn_on", None) == ("light", "turn_on", {}, "turned on X")
    with pytest.raises(ValueError):
        service_for({"kind": "scene", "name": "S", "entity_id": "scene.s"}, "turn_off", None)
