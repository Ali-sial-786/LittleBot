"""The Persona skill (littlebot/skills/persona.py): styles, the setting, the brain's persona prompt."""

from __future__ import annotations

import re

import pytest

from littlebot.core.events import Topics
from littlebot.skills.hub import SkillHub
from littlebot.skills.persona import BASE, SETTING, STYLE_TEXT, STYLES, Persona, clean_style
from littlebot.testing import make_test_context


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


@pytest.fixture
def ctx():
    c = make_test_context()
    yield c
    c.db.close()


async def test_default_and_switching_by_voice(ctx) -> None:
    p = Persona(ctx)
    assert p.style() == "warm" and ctx.settings.get(SETTING) == "warm"
    changes = []
    ctx.bus.subscribe(Topics.SETTINGS_CHANGED, lambda e: changes.append(dict(e.data)))
    assert await p.run("set_personality", {"style": "be more concise"}) == \
        "ok: style set to concise: the shortest useful answers. Answer that way from now on."
    assert p.style() == "concise" and ctx.settings.get(SETTING) == "concise"
    assert changes[-1]["key"] == SETTING and changes[-1]["value"] == "concise"
    assert "shortest useful answer" in p.persona_prompt()
    assert (await p.run("set_personality", {"style": "Funnier please"})).startswith("ok: style set to witty")
    assert (await p.run("set_personality", {"style": "back to normal"})).startswith("ok: style set to warm")
    out = await p.run("set_personality", {"style": "custom: talk like a pirate <b>"})
    assert out.startswith("ok: style set to custom: talk like a pirate b.")
    assert p.persona_prompt().startswith(BASE) and "talk like a pirate b" in p.persona_prompt()
    assert "family-friendly" in p.persona_prompt()
    assert Persona(ctx).style() == "custom: talk like a pirate b"  # kept (a setting: it survives restarts)


async def test_bad_or_missing_styles(ctx) -> None:
    p = Persona(ctx)
    assert (await p.run("set_personality", {"style": "grumpy"})).startswith("error: unknown style")
    assert (await p.run("set_personality", {"style": 7})).startswith("error: unknown style")
    assert (await p.run("set_personality", {"style": "custom:"})).startswith("error:")
    out = await p.run("set_personality", {})
    assert out == "ok: the style is warm; the choices: warm, witty, calm, concise, playful, or custom: <description>"
    assert (await p.run("nope", {})).startswith("error:")
    assert p.style() == "warm"


def test_the_dashboard_setting_is_checked_too(ctx) -> None:
    p = Persona(ctx)
    assert ctx.settings.set(SETTING, "Playful") == "playful"
    assert p.style() == "playful"
    with pytest.raises(ValueError):
        ctx.settings.set(SETTING, "evil overlord")
    assert p.style() == "playful"


def test_config_default_and_a_bad_one() -> None:
    c = make_test_context(skills={"persona": "calm"})
    assert Persona(c).style() == "calm" and "unhurried" in Persona(c).persona_prompt()
    c = make_test_context(skills={"persona": "sulky"})
    assert Persona(c).style() == "warm"


@pytest.mark.parametrize("style", STYLES)
def test_every_style_is_a_few_sentences(ctx, style: str) -> None:
    p = Persona(ctx)
    ctx.settings.set(SETTING, style)
    text = p.persona_prompt()
    assert 3 <= sentences(text) <= 6
    assert text.startswith(BASE) and text.endswith(STYLE_TEXT[style])
    assert "LittleBot" in text and "family-friendly" in text and "short" in text


def test_aliases() -> None:
    for said, style in (("witty", "witty"), ("Be funny", "witty"), ("sarcastic", "witty"), ("calm down", "calm"),
                        ("a bit gentler", "calm"), ("more relaxed", "calm"), ("less chatty", "concise"),
                        ("shorter answers", "concise"), ("be silly", "playful"), ("baby friendly", "playful"),
                        ("default", "warm"), ("", None), (None, None)):
        assert clean_style(said) == style, said


async def test_in_the_hub(ctx) -> None:
    hub = SkillHub(ctx, [Persona(ctx)])
    assert [s["name"] for s in hub.specs()] == ["set_personality"]
    assert hub.prompt_notes().startswith("Personality:") and hub.context_notes() == ""
    assert (await hub.run("set_personality", '{"style": "calm"}')).startswith("ok: style set to calm")
    assert hub.get("persona").persona_prompt().endswith(STYLE_TEXT["calm"])
    assert hub.status()["skills"][0]["style"] == "calm"
