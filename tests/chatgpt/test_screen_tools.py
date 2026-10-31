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
    import littlebot.screens.cards  # noqa: F401 - the real ones first, or they'd replace these on first use
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


async def test_in_a_voice_chat_a_slow_card_doesnt_hold_the_answer(ctx, cards, monkeypatch):
    import asyncio
    import time

    from littlebot.chatgpt import tools

    slow = asyncio.Event()

    async def show(spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        await slow.wait()  # its picture is still loading
        cards.shown.append((spec, source))
        return {"ok": True, "showing": "recipe for home"}

    monkeypatch.setattr(cards, "show", show)
    box = RobotToolbox(ctx, mode="live")
    t0 = time.monotonic()
    out = await run(box, "show_on_screen", card="recipe", title="Pancakes", steps=["Mix", "Fry"])
    assert time.monotonic() - t0 < tools.CARD_WAIT_S + 0.3
    assert out.startswith("ok: a recipe card is going on the screen now") and cards.shown == []
    slow.set()  # the picture came: the card goes up by itself
    for _ in range(50):
        if cards.shown:
            break
        await asyncio.sleep(0.01)
    assert [src for _spec, src in cards.shown] == ["tool"] and box.done == ["the screen shows recipe for home"]
    # typed chat waits for the card, as before
    slow.clear()
    chat = RobotToolbox(ctx, mode="chat")
    waiting = asyncio.ensure_future(run(chat, "show_on_screen", card="recipe", title="Tea", steps=["Boil"]))
    await asyncio.sleep(tools.CARD_WAIT_S + 0.2)
    assert not waiting.done()
    slow.set()
    assert (await waiting).startswith("ok: on the screen now")


async def test_the_weather_card_goes_up_while_the_forecast_is_fetched(ctx, cards, monkeypatch):
    import asyncio

    ctx.privacy.set(cloud_enabled=True)
    web = FakeWeb()
    weather_service(ctx, web)
    order: list[str] = []
    real_show = cards.show

    async def show(spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        order.append("card asked")
        return await real_show(spec, source=source)

    monkeypatch.setattr(cards, "show", show)
    from littlebot.info import weather

    real_report = weather.WeatherService.report

    async def report(self, place=None, *, days=2, hourly=False):
        await asyncio.sleep(0.05)
        out = await real_report(self, place, days=days, hourly=hourly)
        order.append("forecast came")
        return out

    monkeypatch.setattr(weather.WeatherService, "report", report)
    out = json.loads(await run(RobotToolbox(ctx, mode="live"), "get_weather"))
    assert out["on_screen"].startswith("weather for home") and order == ["card asked", "forecast came"]


class PictureScreens(FakeScreens):
    """An explain card with its picture showing (from a real call: "show the picture full screen" made a new card,
    and asked again it opened Google)."""

    active = True

    def __init__(self, picture: bool = True) -> None:
        super().__init__()
        from types import SimpleNamespace

        self.current = SimpleNamespace(kind="explain", title="Alexander the Great",
                                       slot=SimpleNamespace(shown=picture),
                                       describe=lambda: "Alexander the Great: point 1 of 3")
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed: list[str] = []

    def describe(self) -> str | None:
        return "Alexander the Great: point 1 of 3"

    def command(self, action: str, args: dict[str, Any]) -> bool:
        self.commands.append((action, args))
        return True

    def close(self, reason: str = "") -> bool:
        self.closed.append(reason)
        return True


def test_change_card_is_offered_only_with_a_card_up(ctx, cards):
    assert "change_card" not in {s.get("name") for s in RobotToolbox(ctx, mode="live").specs()}
    ctx.screens = PictureScreens()
    spec = next(s for s in RobotToolbox(ctx, mode="live").specs() if s.get("name") == "change_card")
    assert "explain (Alexander the Great: point 1 of 3), with a picture; actions: picture, next" in spec["description"]
    assert spec["description"].endswith(", close.")
    browser = [s for s in RobotToolbox(ctx, mode="live").specs() if s.get("name") == "search_in_browser"]
    assert not browser or "that is change_card" in browser[0]["description"]


async def test_change_card_shows_the_picture_full_screen(ctx, cards):
    ctx.screens = PictureScreens()
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="picture", args={"view": "fill"})
    assert out.startswith("ok: the screen shows the picture full screen now")
    assert ctx.screens.commands == [("picture", {"view": "fill"})]
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="next")
    assert out.startswith("ok: the screen shows Alexander the Great") and ctx.screens.commands[-1] == ("next", {})
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="close")
    assert out.startswith("ok:") and ctx.screens.closed == ["asked to"]


async def test_change_card_says_when_it_cant(ctx, cards):
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="picture")
    assert out.startswith("error: no card is on the screen")
    ctx.screens = PictureScreens(picture=False)
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="picture", args={"view": "fill"})
    assert out.startswith("error: the card on the screen has no picture") and ctx.screens.commands == []
    out = await run(RobotToolbox(ctx, mode="live"), "change_card", action="dance")
    assert out == "error: the explain card can't do that; it can: next, prev, point, all, close"


async def test_show_the_picture_full_screen_needs_no_text_model(ctx, cards):
    from littlebot.chatgpt import quick

    ctx.screens = PictureScreens()
    question = "Show the picture in full screen"
    text = await quick.answer(quick.match(question), RobotToolbox(ctx, mode="live", defer=True), question)
    assert text is not None and text.startswith("Done: the screen shows the picture full screen now")
    assert ctx.screens.commands == [("picture", {"view": "fill"})]
    ctx.screens = PictureScreens(picture=False)  # no picture: the text model says so
    assert await quick.answer(quick.match(question), RobotToolbox(ctx, mode="live", defer=True), question) is None
