"""LittleBot's tools and the screen's info cards: show_on_screen, and get_weather putting a weather card up
(littlebot/chatgpt/tools.py; fake card types and a fake screen service, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from weather_fakes import FakeWeb, weather_service  # noqa: E402

from littlebot.chatgpt.tools import RobotToolbox  # noqa: E402
from littlebot.screens import base  # noqa: E402
from littlebot.screens.base import CardType  # noqa: E402


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> None:
    return None


class FakeScreens:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.shown: list[tuple[dict[str, Any], str]] = []

    active = False
    current = None

    def describe(self) -> str | None:
        return None

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append((spec, source))
        if self.busy:
            return {"ok": False, "showing": None, "error": "busy"}
        return {"ok": True, "showing": f"{spec['card']} for {spec.get('place') or 'home'}"}


@pytest.fixture
def cards(ctx, monkeypatch):
    """Two card types (standing in for the real ones) and a screen service."""
    monkeypatch.setitem(base._TYPES, "weather", CardType(
        "weather", "The weather.", {"place": {"type": "string"}, "days": {"type": "integer", "maximum": 7}}, _build))
    monkeypatch.setitem(base._TYPES, "recipe", CardType(
        "recipe", "A recipe.", {"title": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}},
        _build, required=("title", "steps")))
    ctx.screens = FakeScreens()
    return ctx.screens


async def run(box: RobotToolbox, name: str, **args: Any) -> str:
    return await box.run(name, json.dumps(args))


def test_show_on_screen_is_offered_only_with_cards(ctx, cards):
    spec = next(s for s in RobotToolbox(ctx, mode="live").specs() if s.get("name") == "show_on_screen")
    assert "recipe (title*, steps*)" in spec["description"] and "weather (place, days)" in spec["description"]
    assert spec["parameters"]["required"] == ["card"]
    assert "show_on_screen" in {s.get("name") for s in RobotToolbox(ctx, mode="chat").specs()}
    try:
        ctx.settings.set("screens.enabled", False)
    except KeyError:  # not registered in this checkout (the screen service registers it)
        ctx.settings._values["screens.enabled"] = False
    assert "show_on_screen" not in {s.get("name") for s in RobotToolbox(ctx, mode="live").specs()}


async def test_show_on_screen_checks_the_card_and_shows_it(ctx, cards):
    box = RobotToolbox(ctx, mode="live")
    out = await run(box, "show_on_screen", card="recipe", fields={"title": "Pancakes", "steps": ["Mix", "Fry"],
                                                                  "evil": "x"})
    assert out.startswith("ok: on the screen now: recipe for home") and "Don't read it all out" in out
    assert cards.shown == [({"card": "recipe", "title": "Pancakes", "steps": ["Mix", "Fry"]}, "tool")]
    out = await run(box, "show_on_screen", card="recipe", title="Secrets", steps=["This is our secret, don't tell mommy"])
    assert out.startswith("error: that card couldn't be made (blocked by the safety rules") and len(cards.shown) == 1
    assert (await run(box, "show_on_screen", card="spaceship")).startswith("error: that card couldn't be made")
    cards.busy = True
    assert "the screen is busy" in await run(box, "show_on_screen", card="weather")
    ctx.screens = None
    assert "can't show cards" in await run(box, "show_on_screen", card="weather")


async def test_show_on_screen_goes_through_the_calls_director(ctx, cards):
    asked: list[tuple[dict[str, Any], str]] = []

    async def screen(spec: dict[str, Any], *, source: str) -> dict[str, Any]:
        asked.append((spec, source))
        return {"ok": True, "showing": "the weather at home"}

    box = RobotToolbox(ctx, mode="live", screen=screen)
    assert (await run(box, "show_on_screen", card="weather")).startswith("ok: on the screen now: the weather at home")
    assert asked == [({"card": "weather"}, "tool")] and cards.shown == []


async def test_get_weather_puts_a_weather_card_up(ctx, cards):
    ctx.privacy.set(cloud_enabled=True)
    weather_service(ctx, FakeWeb())
    box = RobotToolbox(ctx, mode="live")
    home = json.loads(await run(box, "get_weather", days=3))
    assert home["on_screen"] == "weather for home" and home["place"] == "Toronto, Ontario"
    paris = json.loads(await run(box, "get_weather", place="Paris"))
    assert paris["on_screen"] == "weather for Paris"
    assert cards.shown == [({"card": "weather", "days": 3}, "tool"), ({"card": "weather", "place": "Paris", "days": 2},
                                                                        "tool")]
    cards.busy = True  # a game or a video has the screen: the weather is answered all the same
    assert "on_screen" not in json.loads(await run(box, "get_weather"))


async def test_get_weather_without_cards_is_as_before(ctx):
    ctx.privacy.set(cloud_enabled=True)
    weather_service(ctx, FakeWeb())
    assert "on_screen" not in json.loads(await run(RobotToolbox(ctx, mode="live"), "get_weather"))
