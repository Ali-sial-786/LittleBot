"""The calendar card (littlebot/screens/cards/calendar.py): a snapshot of the calendar's events from the spec, one
day or a few days, paged five rows at a time."""

from __future__ import annotations

from typing import Any

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import calendar as card_mod
from littlebot.screens.cards.calendar import ARROW_Y, HIGHLIGHT_S, CalendarCard, hex_color


async def make(spec: dict[str, Any]) -> CalendarCard:
    card = await base.build({"card": "calendar", **spec}, None, base.RecordingHost())
    assert isinstance(card, CalendarCard)
    return card


def ev(i: int, day: str = "Today", **more: Any) -> dict[str, Any]:
    return {"id": f"e{i}", "day": day, "when": f"{i}:00 pm", "text": f"thing {i}", **more}


def test_registered_and_offered() -> None:
    ct = base.card_types(offered_only=True)["calendar"]
    assert ct.fields == {} and ct.required == ()
    assert {"next", "prev", "page"} <= set(ct.actions)
    assert "calendar tools" in ct.description


async def test_one_day() -> None:
    card = await make({"title": "Today", "linger": 12, "items": [
        {"id": "e1", "day": "Today", "when": "3:00 pm", "text": "dentist", "where": "Main St Clinic",
         "color": "#7986CB"},
        {"id": "e2", "day": "Today", "when": "6:00 pm", "text": "Soccer"}]})
    assert card.title == "Today" and card.linger_seconds == 12.0 and card.pages == 1
    assert [r["type"] for r in card.rows] == ["event", "event"]  # one day: no day labels
    assert card.rows[0]["color"] == (0x79, 0x86, 0xCB) and card.rows[1]["color"] == art.SKY
    assert card.describe() == "Calendar (Today), 2 events: 3:00 pm Dentist at Main St Clinic; 6:00 pm Soccer"
    assert card.status()["events"] == 2 and card.status()["pages"] == 1
    assert not card.tap(400, 300) and not card.command("next", {}) and not card.key("right")
    assert preview.surface(card, seconds=0.2).get_size() == (base.W, base.H)


async def test_several_days_get_day_labels() -> None:
    card = await make({"title": "This week", "items": [
        ev(1, "Today"), ev(2, "Tomorrow", when="All day", text="Zayn's birthday"), ev(3, "Tomorrow")]})
    assert [(r["type"], r["text"]) for r in card.rows] == [
        ("day", "Today"), ("event", "Thing 1"), ("day", "Tomorrow"), ("event", "Zayn's birthday"),
        ("event", "Thing 3")]
    assert card.describe() == ("Calendar (This week), 3 events: Today 1:00 pm Thing 1; Tomorrow All day Zayn's "
                               "birthday; Tomorrow 3:00 pm Thing 3")
    preview.surface(card, seconds=0.2)


async def test_empty() -> None:
    card = await make({"items": []})
    assert card.title == "Calendar" and card.describe() == "Calendar: Nothing on the calendar" and card.pages == 1
    preview.surface(card, seconds=0.2)
    card = await make({"title": "Saturday", "empty": "Nothing on Saturday"})
    assert card.describe() == "Calendar (Saturday): Nothing on Saturday"
    preview.surface(card, seconds=0.2)


async def test_paging() -> None:
    card = await make({"title": "Today", "items": [ev(i) for i in range(1, 8)]})
    assert card.pages == 2 and len(card.page_rows()) == 5
    assert card.describe().startswith("Calendar (Today), 7 events (page 1 of 2): 1:00 pm Thing 1; ")
    assert card.tap(400, 300) and card.page == 1
    assert card.describe() == "Calendar (Today), 7 events (page 2 of 2): 6:00 pm Thing 6; 7:00 pm Thing 7"
    preview.surface(card, seconds=0.2)
    assert card.tap(400, 300) and card.page == 0  # round again
    assert card.key("right") and card.page == 1 and card.key("left") and card.page == 0
    assert card.command("next", {}) and card.page == 1 and card.command("prev", {}) and card.page == 0
    assert card.command("page", {"n": 2}) and card.page == 1
    assert not card.command("page", {"n": 3}) and not card.command("page", {"n": "x"}) and card.page == 1
    assert card.tap(kit.PANEL.x + 44, ARROW_Y) and card.page == 0  # the left arrow
    assert not card.command("dance", {})


async def test_a_day_label_never_ends_a_page() -> None:
    items = [ev(i, "Today") for i in range(1, 4)] + [ev(4, "Tomorrow"), ev(5, "Tomorrow")]
    card = await make({"items": items})
    # Today, 1, 2, 3 | Tomorrow, 4, 5: the "Tomorrow" label would have been the 5th row, alone
    assert card.pages == 2 and [r["type"] for r in card.page_rows()] == ["day", "event", "event", "event"]
    card.command("next", {})
    assert card.page_rows()[0] == {"type": "day", "text": "Tomorrow"}


async def test_highlight_glows_for_a_moment() -> None:
    card = await make({"highlight": "e2", "items": [ev(1), ev(2)]})
    assert card.highlight == "e2"
    preview.surface(card, seconds=0.3)
    card.update(HIGHLIGHT_S + 1)
    preview.surface(card, seconds=0.1)


async def test_bad_items_and_colours_are_ignored() -> None:
    bad: dict[str, Any] = {"title": ["x"], "linger": "soon", "highlight": {"x": 1}, "items": [
        "x", None, 3, {"text": None}, {"text": ""}, {"when": "3 pm"},
        {"text": "**ok**", "when": {"a": 1}, "where": ["no"], "color": "red", "day": 5},
        {"text": "two", "color": "#12345"}, {"text": "three", "color": "#GGGGGG"}, {"text": "four", "color": 7}]}
    card = await make(bad)
    assert card.title == "Calendar" and card.linger_seconds is None and card.highlight is None
    assert [e["text"] for e in card.events] == ["Ok", "Two", "Three", "Four"]
    assert all(e["color"] == art.SKY for e in card.events)
    assert card.events[0]["when"] == "" and card.events[0]["where"] == ""
    assert not card.multi_day  # one day ("5"), the rest without a day
    assert card.describe() == "Calendar, 4 events: Ok; Two; Three; Four"
    preview.surface(card, seconds=0.2)
    assert len((await make({"items": [ev(i) for i in range(100)]})).events) == 60
    assert len((await make({"items": "nope"})).events) == 0
    long = await make({"items": [ev(1, text="a very long event name " * 10, where="somewhere far " * 10)]})
    assert len(long.events[0]["text"]) <= 83
    preview.surface(long, seconds=0.1)


def test_hex_color() -> None:
    assert hex_color("#7986CB") == (121, 134, 203) and hex_color(" #ffffff ") == (255, 255, 255)
    assert hex_color("7986CB") == art.SKY and hex_color(None, art.MINT) == art.MINT


async def test_samples_draw() -> None:
    for spec in card_mod.SAMPLES:
        card = await base.build(spec, None, base.RecordingHost())
        assert isinstance(card, CalendarCard)
        assert preview.surface(card, seconds=0.4).get_size() == (base.W, base.H)
    one, few, empty = [await make(s) for s in card_mod.SAMPLES]
    assert not one.multi_day and few.multi_day and not empty.events
    assert few.describe().startswith("Calendar (This week), 4 events (page 1 of 2): Today 3:00 pm Dentist; Tomorrow All day")
