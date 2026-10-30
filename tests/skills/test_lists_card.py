"""The lists card (littlebot/screens/cards/lists.py): live from the Lists skill, tick boxes, paging, previews."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.screens import base, preview
from littlebot.screens.cards import lists as card_mod
from littlebot.screens.cards.lists import ROWS, TICK_S, ListCard
from littlebot.skills.hub import SkillHub
from littlebot.skills.lists import Lists
from littlebot.testing import make_test_context


@pytest.fixture
def rig():
    ctx = make_test_context()
    skill = Lists(ctx)
    ctx.skills = SkillHub(ctx, [skill])
    yield ctx, skill
    ctx.db.close()


def add(skill: Lists, key: str, *items: str) -> None:
    skill.add(key, list(items))


def run(card: ListCard, seconds: float, step: float = 0.05) -> None:
    t = 0.0
    while t < seconds - 1e-9:
        card.update(step)
        t += step


def test_registered_and_offered() -> None:
    ct = base.card_types(offered_only=True)["lists"]
    assert set(ct.fields) == {"list"} and ct.required == ()
    assert {"next", "prev", "page", "list"} <= set(ct.actions)
    assert all(s["card"] == "lists" for s in card_mod.SAMPLES)


async def test_shows_the_list_and_ticks_items_off(rig) -> None:
    ctx, skill = rig
    add(skill, "shopping", "milk", "eggs", "bread")
    host = base.RecordingHost()
    card = await base.build({"card": "lists", "list": "Grocery list"}, ctx, host)
    assert isinstance(card, ListCard) and card.title == "Shopping list"
    assert card.describe() == "Shopping list: 3 items: milk, eggs, bread"
    assert card.linger_seconds is None and not card.sticky
    rect = card._row_rect(1)
    assert card.tap(rect.centerx, rect.centery)
    assert host.sounds == ["pop"] and host.told == ["eggs was just ticked off the Shopping list on the screen."]
    assert card.tap(rect.centerx, rect.centery)  # a second tap while it goes: nothing more
    assert host.sounds == ["pop"]
    preview.surface(card, seconds=0.1)  # drawn struck through
    assert [r["item"] for r in skill.items("shopping")] == ["milk", "eggs", "bread"]
    run(card, TICK_S)
    assert [r["item"] for r in skill.items("shopping")] == ["milk", "bread"]
    assert card.describe() == "Shopping list: 2 items: milk, bread"
    assert not card.tap(card._row_rect(4).centerx, card._row_rect(4).centery)  # an empty row


async def test_follows_changes_made_by_voice(rig) -> None:
    ctx, skill = rig
    card = await base.build({"card": "lists"}, ctx, base.RecordingHost())
    assert card.describe() == "Shopping list: empty"
    assert preview.surface(card, seconds=0.2).get_size() == (base.W, base.H)
    await skill.run("add_to_list", {"items": ["apples", "pears"]})
    card.update(0.03)
    assert card.describe() == "Shopping list: 2 items: apples, pears"
    await skill.run("clear_list", {"list": "shopping"})
    card.update(0.03)
    assert card.describe() == "Shopping list: empty"


async def test_pages_of_five(rig) -> None:
    ctx, skill = rig
    add(skill, "packing", *[f"thing {i}" for i in range(1, 13)])
    card = await base.build({"card": "lists", "list": "packing", "linger": 12}, ctx, base.RecordingHost())
    assert card.linger_seconds == 12.0 and card.pages == 3 and len(card.page_rows()) == ROWS
    assert card.describe().startswith("Packing list: 12 items (page 1 of 3): thing 1, thing 2")
    preview.surface(card, seconds=0.2)
    back, fwd = card._arrows()
    assert card.tap(*fwd) and card.page == 1
    assert card.command("next", {}) and card.page == 2
    assert card.describe() == "Packing list: 12 items (page 3 of 3): thing 11, thing 12"
    assert card.command("next", {}) and card.page == 2  # stays on the last page
    assert card.tap(*back) and card.page == 1
    assert card.key("left") and card.page == 0
    assert card.command("page", {"n": 3}) and card.page == 2
    assert not card.command("page", {"n": 9})
    preview.surface(card, seconds=0.2)
    rect = card._row_rect(1)
    card.tap(rect.centerx, rect.centery)  # thing 12
    run(card, TICK_S)
    assert card.pages == 3 and card.describe().endswith("thing 11")
    card.tap(card._row_rect(0).centerx, card._row_rect(0).centery)
    run(card, TICK_S)
    assert card.pages == 2 and card.page == 1  # the last page emptied: back one


async def test_switch_lists_by_voice_and_defaults(rig) -> None:
    ctx, skill = rig
    add(skill, "to-do", "call the bank")
    card = await base.build({"card": "lists"}, ctx, base.RecordingHost())
    assert card.list_name == "to-do"  # no shopping list yet: the one there is
    add(skill, "shopping", "milk")
    card = await base.build({"card": "lists"}, ctx, base.RecordingHost())
    assert card.list_name == "shopping"
    assert card.command("list", {"name": "todo"}) and card.title == "To-do list"
    assert card.describe() == "To-do list: 1 item: call the bank"
    assert not card.command("list", {"name": ""}) and not card.command("dance", {})
    assert card.status()["list"] == "to-do"


async def test_previews_without_the_skill() -> None:
    for spec in card_mod.SAMPLES:
        card = await base.build(spec, None, base.RecordingHost())
        assert isinstance(card, ListCard)
        assert preview.surface(card, seconds=0.3).get_size() == (base.W, base.H)
    card = await base.build({"card": "lists", "list": "packing", "items": ["tent", "", "torch", {"x": 1}]}, None,
                            base.RecordingHost())
    assert card.describe() == "Packing list: 2 items: tent, torch"
    card.tap(card._row_rect(0).centerx, card._row_rect(0).centery)
    run(card, TICK_S)
    assert card.describe() == "Packing list: 1 item: torch"


async def test_a_skill_that_fails_keeps_the_card_up(rig) -> None:
    ctx, skill = rig
    add(skill, "shopping", "milk")
    card = await base.build({"card": "lists"}, ctx, base.RecordingHost())

    def broken(*_: Any) -> Any:
        raise RuntimeError("db gone")

    skill.items = broken  # type: ignore[method-assign]
    skill.tick_off = broken  # type: ignore[method-assign]
    skill.version += 1
    card.update(0.03)
    assert card.describe() == "Shopping list: 1 item: milk"
    card.tap(card._row_rect(0).centerx, card._row_rect(0).centery)
    run(card, TICK_S)
    preview.surface(card, seconds=0.1)
