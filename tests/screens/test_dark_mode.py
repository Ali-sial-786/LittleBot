"""Dark mode (setting display.theme): the art theme's colour rules, the face, the voice's ways to switch it."""

from __future__ import annotations

import asyncio

import pytest

pygame = pytest.importorskip("pygame")

from littlebot.games import art  # noqa: E402


@pytest.fixture
def dark():
    art.set_dark(True)
    yield
    art.set_dark(False)


def test_light_theme_changes_nothing():
    art.set_dark(False)
    for c in (art.INK, art.CREAM, art.WHITE, art.LEMON, art.BG_TOP):
        assert art.surface(c) == c and art.ink(c) == c


def test_dark_theme_turns_surfaces_dark_and_ink_light(dark):
    for pale in (art.CREAM, art.WHITE, art.BG_TOP, art.mix(art.SKY, art.WHITE, 0.84)):
        assert art._lightness(art.surface(pale)) < 0.3, pale  # cream panels, pale tints: dark
    for candy in art.CANDY:
        assert art.surface(candy) == candy  # bright candy colours stay
    assert art._lightness(art.ink(art.INK)) > 0.55  # plum text: light...
    lilac = art.ink(art.INK)
    assert lilac[0] > lilac[1] and lilac[2] > lilac[1]  # ...and still plum-ish
    assert art.ink(art.WHITE) == art.WHITE  # white text (on a chip) stays white
    assert art.readable(art.INK, art.mix(art.LEMON, art.WHITE, 0.2)) == art.INK  # dark text on a bright chip stays
    assert art.readable(art.INK, art.CREAM) == art.ink(art.INK)  # ...on a panel that turned dark, it turns light


def test_the_background_and_caches_follow_the_theme(dark):
    night = art.background((64, 48))
    assert sum(night.get_at((32, 40))[:3]) < 150
    art.set_dark(False)
    day = art.background((64, 48))
    assert sum(day.get_at((32, 40))[:3]) > 600 and day is not night
    art.set_dark(True)
    assert art.background((64, 48)) is night  # each theme's sprite is kept
    assert art.set_dark(True) is False and art.set_dark(False) is True  # "changed?"


def test_the_face_follows_the_setting():
    from littlebot.face.controller import FaceController
    from littlebot.testing import make_test_context

    ctx = make_test_context(fakes=False)
    face = FaceController(ctx, seed=1)
    pink = tuple(ctx.config.face.background_color)
    assert face.frame().background == pink
    ctx.settings.set("display.theme", "dark")
    assert face.frame().background == tuple(ctx.config.face.dark_background_color)
    ctx.settings.set("display.theme", "light")
    assert face.frame().background == pink
    ctx.settings.set("display.theme", "dark")
    again = FaceController(ctx, seed=1)  # kept across a restart
    assert again.frame().background == tuple(ctx.config.face.dark_background_color)
    with pytest.raises(ValueError):
        ctx.settings.set("display.theme", "purple")


@pytest.mark.parametrize("said, theme", [
    ("dark mode", "dark"), ("Switch to dark mode.", "dark"), ("go into night mode please", "dark"),
    ("turn off dark mode", "light"), ("light mode", "light"), ("switch back to light mode", "light"),
])
def test_said_plainly_it_switches_at_once(said, theme):
    from littlebot.assistant import intents
    from littlebot.chatgpt.tools import RobotToolbox
    from littlebot.testing import make_test_context

    ctx = make_test_context()
    ctx.settings.set("display.theme", "light" if theme == "dark" else "dark")
    intent = intents.match(said)
    assert intent is not None and intent.name == "theme"
    words = asyncio.run(intents.run(intent, RobotToolbox(ctx, mode="chat", defer=False), ctx))
    assert ctx.settings.get("display.theme") == theme and words in ("Dark mode.", "Light mode.")


def test_a_question_about_it_is_not_a_switch():
    from littlebot.assistant import intents
    from littlebot.chatgpt import quick

    for q in ("what is dark mode?", "do you have a night mode?", "the dark mode is nice"):
        assert intents.match(q) is None and quick.match(q) is None


def test_the_voice_tool_and_the_hand_off_shortcut():
    from littlebot.chatgpt import quick
    from littlebot.chatgpt.tools import TOOLS, RobotToolbox
    from littlebot.testing import make_test_context

    assert any(t.name == "set_screen_theme" and "live" in t.modes for t in TOOLS)
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="live", defer=False)
    assert asyncio.run(box.run("set_screen_theme", {"theme": "dark"})).startswith("ok:")
    assert ctx.settings.get("display.theme") == "dark"
    assert asyncio.run(box.run("set_screen_theme", {"theme": "blue"})).startswith("error")
    asked = quick.match("Put LittleBot in light mode.")
    told = asyncio.run(quick.answer(asked, box, "Put LittleBot in light mode."))
    assert told.startswith("Done: light mode is on") and ctx.settings.get("display.theme") == "light"
