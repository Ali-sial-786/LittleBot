"""The briefing and home cards (littlebot/screens/cards/briefing.py, home.py): registration, drawing, paging,
the voice's line, and a tap switching a light through the smart-home skill."""

from __future__ import annotations

import asyncio
import copy
from typing import Any


from littlebot.screens import base, preview
from littlebot.screens.cards import briefing as briefing_card
from littlebot.screens.cards import home as home_card
from littlebot.screens.cards.briefing import BriefingCard
from littlebot.screens.cards.home import HomeCard, state_label
from littlebot.skills.home import HomeError
from littlebot.testing import make_test_context


# ------------------------------------------------------------------ briefing
def test_briefing_card_is_registered_and_offered():
    types = base.card_types(offered_only=True)
    ct = types["briefing"]
    assert set(ct.fields) == {"title", "date", "weather", "conditions", "headlines", "reminder"}
    assert ct.fields["headlines"]["type"] == "array" and set(ct.actions) == {"next", "prev"}
    assert "home" in types and types["home"].required == ()


async def test_briefing_samples_build_and_draw():
    for spec in briefing_card.SAMPLES:
        card = await base.build(spec, None, base.RecordingHost())
        assert isinstance(card, BriefingCard)
        assert preview.surface(card, seconds=0.8).get_size() == (base.W, base.H)
    assert await base.build({"card": "briefing", "title": "Hi"}, None, base.RecordingHost()) is None


def test_briefing_card_pages_and_describes():
    spec = copy.deepcopy(briefing_card.SAMPLES[1])  # 7 headlines, no weather or reminder: 5 a page
    card = BriefingCard(spec)
    assert card.per_page == 5 and card.pages == 2 and [i for i, _ in card.shown()] == [0, 1, 2, 3, 4]
    assert "7 headlines (page 1 of 2)" in card.describe()
    assert card.command("next", {}) and card.page == 1 and [i for i, _ in card.shown()] == [5, 6]
    assert not card.command("next", {}) and card.command("prev", {}) and card.page == 0
    assert card.tap(600, 300) and card.page == 1
    assert card.tap(600, 300) and card.page == 0  # past the last page: back to the first
    assert card.key("right") and card.key("left") and card.page == 0
    card.drag(-80, 0)
    card.drag_end()
    assert card.page == 1
    preview.surface(card, seconds=0.5)
    full = BriefingCard(briefing_card.SAMPLES[0])
    assert full.per_page == 3 and full.pages == 1 and not full.tap(600, 300)
    assert full.describe().startswith("Good morning, Friday 30 October; 12° / 4° · light rain · rain 70%")
    assert full.describe().endswith("; next reminder: 9 am dentist")
    assert full.status()["headlines"] == 3


def test_briefing_card_cleans_what_the_ai_sends():
    card = BriefingCard({"card": "briefing", "title": "**Good** morning\n", "headlines": ["a", 3, None, "", "# b"],
                         "sources": ["X"], "temperature": True, "reminder": {"bad": 1}})
    assert card.title == "Good morning" and card.headlines == ["a", "3", "b"] and card.sources == ["X"]
    assert card.temperature is None and card.reminder == ""


# ------------------------------------------------------------------ home
DEVICES = home_card.SAMPLES[0]["devices"]


class StubHome:
    """Stands in for the SmartHome skill on the card's side."""

    name = "home"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.toggled: list[str] = []
        self.asked: list[str] = []

    def available(self) -> bool:
        return True

    async def device_list(self, wanted: str = "", *, fresh: bool = False) -> list[dict[str, Any]]:
        self.asked.append(wanted)
        if wanted == "broken":
            raise HomeError("timeout")
        return [d for d in copy.deepcopy(DEVICES) if d["kind"] == "light"] if wanted == "lights" else []

    async def toggle(self, entity_id: str) -> str | None:
        self.toggled.append(entity_id)
        if self.fail:
            raise HomeError("timeout")
        return "off"


class Hub:
    def __init__(self, skill: Any) -> None:
        self.skill = skill

    def get(self, name: str) -> Any:
        return self.skill if name == "home" else None


async def test_home_samples_build_draw_and_page():
    card = await base.build(copy.deepcopy(home_card.SAMPLES[0]), None, base.RecordingHost())
    assert isinstance(card, HomeCard) and card.pages == 2 and len(card.shown()) == 6
    assert preview.surface(card, seconds=1.0).get_size() == (base.W, base.H)
    assert card.describe().startswith("Lights (page 1 of 2): Kitchen Light on 80%, Porch Light off")
    assert card.command("next", {}) and [d["name"] for d in card.shown()] == ["Bedroom Fan", "Living Room TV",
                                                                            "Movie Night"]
    assert card.animating  # the fan spins
    preview.surface(card, seconds=0.5)
    assert card.key("left") and card.page == 0
    assert not card.switch(card.shown()[0])  # no skill to ask: nothing happens


async def test_home_card_from_a_filter_asks_the_skill():
    ctx = make_test_context()
    try:
        stub = StubHome()
        ctx.skills = Hub(stub)
        card = await base.build({"card": "home", "filter": "lights"}, ctx, base.RecordingHost())
        assert isinstance(card, HomeCard) and card.title == "Lights" and len(card.devices) == 2
        assert stub.asked == ["lights"]
        assert await base.build({"card": "home", "filter": "garden"}, ctx, base.RecordingHost()) is None
        assert await base.build({"card": "home", "filter": "broken"}, ctx, base.RecordingHost()) is None
        ctx.skills = None
        assert await base.build({"card": "home", "filter": "lights"}, ctx, base.RecordingHost()) is None
    finally:
        ctx.db.close()


async def test_a_tap_switches_a_light_and_a_failure_flips_it_back():
    host = base.RecordingHost()
    ok_home = StubHome()
    card = HomeCard({"title": "Lights"}, host, devices=copy.deepcopy(DEVICES), toggle=ok_home.toggle,
                    loop=asyncio.get_running_loop())
    kitchen = card.tile_rect(0)
    assert card.tap(*kitchen.center)
    assert card.devices[0]["state"] == "off" and host.sounds == ["pop"]  # flipped at once
    assert not card.tap(*kitchen.center)  # still asking: a second tap waits
    await asyncio.sleep(0.01)
    card.update(0.1)
    assert ok_home.toggled == ["light.kitchen"] and card.devices[0]["state"] == "off"
    lock = card.tile_rect(4)
    assert not card.tap(*lock.center)  # a lock is never switched by a tap
    assert card.tap(10, 470) and card.page == 1  # outside the tiles: the next page

    bad_home = StubHome(fail=True)
    card = HomeCard({"title": "Lights"}, host, devices=copy.deepcopy(DEVICES), toggle=bad_home.toggle,
                    loop=asyncio.get_running_loop())
    porch = card.tile_rect(1)
    assert card.tap(*porch.center) and card.devices[1]["state"] == "on"
    await asyncio.sleep(0.01)
    card.update(0.1)
    assert card.devices[1]["state"] == "off" and host.told[-1].startswith("Couldn't switch Porch Light")


def test_state_labels():
    assert state_label({"kind": "light", "state": "on", "brightness_percent": 40}) == "On 40%"
    assert state_label({"kind": "light", "state": "off"}) == "Off"
    assert state_label({"kind": "climate", "state": "heat", "target_temperature": 21.5}) == "21.5°"
    assert state_label({"kind": "climate", "state": "off", "target_temperature": 21}) == "Off"
    assert state_label({"kind": "climate", "state": "cool", "temperature_now": 24}) == "24°"
    assert state_label({"kind": "cover", "state": "open", "position_percent": 30}) == "30% open"
    assert state_label({"kind": "lock", "state": "unlocked"}) == "Unlocked"
    assert state_label({"kind": "scene", "state": "2026-10-30T07:00:00"}) == "Ready"
    assert state_label({"kind": "switch", "state": "unavailable"}) == "Offline"
