"""The calendar card (littlebot/screens/cards/calendar.py): a snapshot of the calendar's events from the spec, one
day or a few days, as many as fit and at most MAX_LINES lines a page; big when they all fit on one page."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import calendar as card_mod
from littlebot.screens.cards.calendar import (
    ARROW_Y,
    BACK_X,
    BIG,
    HIGHLIGHT_S,
    INNER,
    NORMAL,
    CalendarCard,
    hex_color,
    paginate,
)


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
    assert all(r["lay"]["tier"] is BIG for r in card.rows)  # all on one page: big
    assert card.rows[0]["lay"]["side"] == "Main St Clinic" and not card.rows[0]["lay"]["below"]  # beside it
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
    card = await make({"title": "Today", "items": [ev(i) for i in range(1, 13)]})
    assert all(r["lay"]["tier"] is NORMAL for r in card.rows)  # more than a page: the normal size
    assert card.pages == 2 and len(card.page_rows()) == 6  # 12: 6 + 6, not 8 + 4
    assert card.describe().startswith("Calendar (Today), 12 events (page 1 of 2): 1:00 pm Thing 1; ")
    assert card.tap(400, 300) and card.page == 1
    assert card.describe() == ("Calendar (Today), 12 events (page 2 of 2): 7:00 pm Thing 7; 8:00 pm Thing 8; 9:00 pm "
                               "Thing 9; 10:00 pm Thing 10; 11:00 pm Thing 11; 12:00 pm Thing 12")
    preview.surface(card, seconds=0.2)
    assert card.tap(400, 300) and card.page == 0  # round again
    assert card.key("right") and card.page == 1 and card.key("left") and card.page == 0
    assert card.command("next", {}) and card.page == 1 and card.command("prev", {}) and card.page == 0
    assert card.command("page", {"n": 2}) and card.page == 1
    assert not card.command("page", {"n": 3}) and not card.command("page", {"n": "x"}) and card.page == 1
    assert card.tap(BACK_X, ARROW_Y) and card.page == 0  # the left arrow
    assert not card.command("dance", {})
    eight = await make({"items": [ev(i) for i in range(1, 9)]})
    assert eight.pages == 1 and len(eight.page_rows()) == 8  # eight one-line events: one page


async def test_pages_keep_to_max_lines_and_the_panel() -> None:
    long = "a parent-teacher meeting about the school trip to the science museum"
    items = [ev(i, "Today" if i < 5 else "Tomorrow", where="Oakwood Primary School" if i % 2 else "",
                text=long if i % 3 == 0 else f"thing {i}") for i in range(1, 15)]
    card = await make({"items": items})
    assert card.pages > 1
    for page in range(card.pages):
        card.command("page", {"n": page + 1})
        rows = card._pages[page]
        assert sum(r["k"] for r in rows) <= base.MAX_LINES
        assert sum(r["h"] for r in rows) <= INNER.h - 70
        assert rows[0]["type"] == "day" and rows[-1]["type"] == "event"  # every page says which day it is
        preview.surface(card, seconds=0.1)
    shown = [r["id"] for p in card._pages for r in p if r["type"] == "event"]
    assert shown == [f"e{i}" for i in range(1, 15)]  # every event once, in order
    wrapped = next(r for r in card.rows if r.get("id") == "e3")
    assert len(wrapped["lay"]["lines"]) == 2 and wrapped["lay"]["size"] >= base.BODY_PX and wrapped["lay"]["below"]


async def test_a_day_label_never_ends_a_page() -> None:
    def row(kind: str, text: str = "") -> dict[str, Any]:
        return {"type": kind, "text": text, "h": 10, "k": 1}

    # wherever the page ends, it never ends on a heading, and a page that goes on with a day starts with its heading
    for today in range(1, 14):
        rows = [row("day", "Today"), *[row("event", f"t{i}") for i in range(today)], row("day", "Tomorrow"),
                row("event", "m1"), row("event", "m2")]
        for room in (1000, 75):
            pages = paginate(rows, room=room, paged_room=room)
            assert all(p[-1]["type"] == "event" and p[0]["type"] == "day" for p in pages), (today, room)
            assert all(sum(r["k"] for r in p) <= base.MAX_LINES for p in pages)
            events = [r["text"] for p in pages for r in p if r["type"] == "event"]
            assert events == [r["text"] for r in rows if r["type"] == "event"]
    card = await make({"items": [ev(i, "Today") for i in range(1, 7)] + [ev(7, "Tomorrow"), ev(8, "Tomorrow")]})
    assert card.pages == 2 and card.page_rows()[-1]["type"] == "event"
    card.command("next", {})
    assert card.page_rows()[0]["type"] == "day" and card.page_rows()[0]["text"] in ("Today", "Tomorrow")


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
    assert one.pages == 1 and few.pages == 2  # five events on three days: ten lines
    assert few.describe().startswith("Calendar (This week), 5 events (page 1 of 2): Today 3:00 pm Dentist; "
                                     "Tomorrow All day")


def drawn_sizes(card: Any, monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """The text sizes ``card.draw`` uses."""
    sizes: set[int] = set()
    for mod, name in ((art, "text"), (art, "text_outlined"), (art, "pill"), (kit, "text_block")):
        real = getattr(mod, name)

        def spy(*a: Any, _real: Any = real, **kw: Any) -> Any:
            sizes.add(int(a[1] if len(a) > 1 else kw.get("size")))
            return _real(*a, **kw)

        monkeypatch.setattr(mod, name, spy)
    card.draw(pygame.Surface((base.W, base.H)))
    monkeypatch.undo()
    return sizes


def unpainted(card: Any) -> int:
    """How many canvas pixels ``card.draw`` leaves as they were."""
    surf = pygame.Surface((base.W, base.H))
    surf.fill((255, 0, 255))
    card.draw(surf)
    return pygame.transform.threshold(None, surf, (255, 0, 255), (0, 0, 0, 0), set_behavior=0)


async def test_readable_and_the_whole_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    words = "a parent-teacher meeting about the school trip "
    long = [ev(i, "Today" if i < 6 else "Tomorrow", text=words * (i % 2 + 1),
               where="Oakwood Primary School, the hall by the playground") for i in range(1, 12)]
    for spec in [*card_mod.SAMPLES, {"card": "calendar", "items": long, "highlight": "e2"}]:
        card = await base.build(spec, None, base.RecordingHost())
        for page in range(card.pages):
            card.command("page", {"n": page + 1})
            assert unpainted(card) == 0
            assert min(drawn_sizes(card, monkeypatch)) >= base.SMALL_PX
            for row in card.page_rows():
                if row["type"] == "event":
                    assert row["lay"]["size"] >= base.BODY_PX and row["lay"]["tier"].side_px >= base.SMALL_PX
