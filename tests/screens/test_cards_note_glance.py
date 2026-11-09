"""The note and glance cards (littlebot/screens/cards/note.py, glance.py): build, draw, describe, tap."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards.glance import UPCOMING_LINES, GlanceCard, upcoming_lines
from littlebot.screens.cards.note import NoteCard
from littlebot.testing import make_test_context


class Pic:
    ready = True
    credit = "Wikipedia"

    def __init__(self) -> None:
        self.surface = pygame.Surface((640, 480))


async def test_note_builds_draws_and_closes_on_tap() -> None:
    assert base.card_types(offered_only=True)["note"].required == ("text",)
    assert await base.build({"card": "note"}, None, base.RecordingHost()) is None
    c = await base.build({"card": "note", "title": "Dinner", "text": "Dinner's ready! Come down please."}, None,
                         base.RecordingHost())
    assert preview.surface(c, seconds=0.6).get_size() == (base.W, base.H)
    assert c.describe().startswith("a note: Dinner")
    assert c.tap(100, 100) and c.done


async def test_glance_draws_with_and_without_weather() -> None:
    weather = {"now": {"temperature": 12, "conditions": "sunny"}, "units": {"temperature": "°C"},
               "forecast": [{"high": 14, "low": 3}]}
    c = GlanceCard({"card": "glance"}, base.RecordingHost(), weather=weather,
                   upcoming=["6:00 pm: call mom", "Shopping list: 3 items"])
    assert preview.surface(c, seconds=0.3).get_size() == (base.W, base.H)
    assert "call mom" in c.describe() and "weather" in c.describe()
    bare = GlanceCard({"card": "glance"}, base.RecordingHost())
    assert preview.surface(bare, seconds=0.3).get_size() == (base.W, base.H)


async def test_glance_builds_offline() -> None:
    ctx = make_test_context()
    c = await base.build({"card": "glance"}, ctx, base.RecordingHost())
    assert isinstance(c, GlanceCard)
    assert upcoming_lines(ctx) == []


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


async def test_note_as_big_as_it_fits_with_a_picture_beside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    short = NoteCard({"title": "Dinner", "text": "Dinner's ready!"}, base.RecordingHost())
    long = NoteCard({"title": "Dad", "text": "Back in twenty minutes, I went to get milk and bread. " * 8},
                    base.RecordingHost())
    assert short._wide[0] >= 84 and len(short._wide[1]) <= 2  # a few words: huge
    assert base.BODY_PX <= long._wide[0] < short._wide[0] and len(long._wide[1]) <= base.MAX_LINES
    host = base.RecordingHost(pictures={"kitten": Pic()})
    pic = NoteCard({"title": "Biscuit", "text": "Say hello to Biscuit!", "picture_query": "kitten"}, host)
    assert host.asked_pictures == [("kitten", "")] and pic._beside is not None and pic.slot.shown
    crowded = NoteCard({"title": "x", "text": "word " * 80, "picture_query": "kitten"},
                       base.RecordingHost(pictures={"kitten": Pic()}))
    assert crowded._beside is None  # the words need the whole panel: no picture
    no_url = NoteCard({"title": "x", "text": "y", "picture_url": "http://example.com/a.jpg"}, base.RecordingHost())
    assert no_url.picture is None
    for card in (short, long, pic, crowded):
        card.update(1.0)
        assert unpainted(card) == 0
        assert min(drawn_sizes(card, monkeypatch)) >= base.SMALL_PX


async def test_glance_fills_both_halves_and_keeps_to_max_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    weather = {"now": {"temperature": 12, "conditions": "partly cloudy"}, "units": {"temperature": "°C"},
               "forecast": [{"high": 14, "low": 3}]}
    many = ["Pasta timer: 7:59 left (on the screen)", "3 pm Dentist", "Tomorrow 8:30 am School drop-off and then the "
            "long drive to grandma's house in the country", "Shopping list: 7 items", "To-do list: 3 items"]
    c = GlanceCard({"card": "glance"}, base.RecordingHost(), weather=weather, upcoming=many)
    assert len(c.upcoming) == 4 and sum(len(i) for i in c._items) <= UPCOMING_LINES
    assert all(len(i) <= 2 for i in c._items)
    for card in (c, GlanceCard({"card": "glance"}, base.RecordingHost(), twelve=False)):
        assert unpainted(card) == 0
        assert min(drawn_sizes(card, monkeypatch)) >= base.SMALL_PX
