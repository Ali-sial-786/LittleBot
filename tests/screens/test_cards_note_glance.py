"""The note and glance cards (littlebot/screens/cards/note.py, glance.py): build, draw, describe, tap."""

from __future__ import annotations

from littlebot.screens import base, preview
from littlebot.screens.cards.glance import GlanceCard, upcoming_lines
from littlebot.testing import make_test_context


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
