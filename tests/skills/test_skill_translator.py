"""Interpreter mode (littlebot/skills/translator.py): on and off, the context note, languages, idle time-out."""

from __future__ import annotations

import pytest

from littlebot.skills.hub import SkillHub
from littlebot.skills.translator import Translator, language_name
from littlebot.testing import make_test_context


class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup():
    ctx = make_test_context()
    clock = Clock()
    t = Translator(ctx, now=clock)
    yield ctx, t, clock
    ctx.db.close()


async def test_on_both_ways_and_off(setup):
    ctx, t, _ = setup
    assert t.context_note() == "" and not t.on
    out = await t.run("start_translator", {"language": "spanish"})
    assert out.startswith("ok: interpreter mode is on, translating between English and Spanish")
    note = t.context_note()
    assert note.startswith("Interpreter mode: translate everything the user says into Spanish, and Spanish into "
                           "English; speak only the translation, no commentary;")
    assert "show_on_screen" in note and "'stop translating' ends it" in note
    assert t.status() == {"name": "translator", "available": True, "on": True, "language": "Spanish",
                          "both_ways": True}
    assert (await t.run("stop_translator", {})).startswith("ok: interpreter mode is off (no more translating into "
                                                          "Spanish)")
    assert t.context_note() == "" and await t.run("stop_translator", {}) == "ok: interpreter mode wasn't on"


async def test_one_way(setup):
    ctx, t, _ = setup
    assert (await t.run("start_translator", {"language": "into French", "both_ways": False})).startswith(
        "ok: interpreter mode is on, translating into French")
    assert t.context_note().startswith("Interpreter mode: translate everything the user says into French; speak only")
    await t.run("start_translator", {"language": "ur", "both_ways": "yes please"})  # only a real true/false counts
    assert t.language == "Urdu" and t.both_ways is False


async def test_languages_are_checked(setup):
    ctx, t, _ = setup
    for bad in ("", None, 42, "123", "<script>", "english", "sign language", "a" * 60, "the one my neighbour speaks"):
        assert (await t.run("start_translator", {"language": bad})).startswith("error:"), bad
    assert not t.on
    assert language_name("Brazilian Portuguese") == "Brazilian Portuguese"
    assert language_name("in Haitian Creole") == "Haitian Creole"
    assert language_name("zh") == "Mandarin Chinese" and language_name("Farsi language") == "Persian"


async def test_it_ends_after_idle_minutes(setup):
    ctx, t, clock = setup
    await t.run("start_translator", {"language": "German"})
    clock.t += 10 * 60
    assert t.context_note().startswith("Interpreter mode")  # a turn: the idle time starts again
    clock.t += 14 * 60
    assert t.on
    clock.t += 2 * 60
    assert not t.on and t.context_note() == ""


async def test_turns_from_the_assistant_keep_it_alive(setup):
    ctx, t, clock = setup
    await t.start()
    await t.run("start_translator", {"language": "Arabic"})
    for _ in range(3):
        clock.t += 10 * 60
        ctx.bus.publish("assistant.heard", {"words": 4, "ms": 900}, source="test")
    assert t.on
    await t.stop()
    clock.t += 16 * 60
    assert not t.on


async def test_config_and_hub(setup):
    ctx, t, clock = setup
    hub = SkillHub(ctx, [t])
    assert [s["name"] for s in hub.specs()] == ["start_translator", "stop_translator"]
    assert "stop_translator" in hub.prompt_notes() and hub.context_notes() == ""
    await hub.run("start_translator", '{"language": "Korean"}')
    assert "Korean" in hub.context_notes()
    short = make_test_context(skills={"translator_idle_minutes": 2})
    try:
        s = Translator(short, now=clock)
        await s.run("start_translator", {"language": "Italian"})
        clock.t += 3 * 60
        assert not s.on
    finally:
        short.db.close()
    off = make_test_context(skills={"translator": False})
    try:
        assert not Translator(off).available()
    finally:
        off.db.close()
