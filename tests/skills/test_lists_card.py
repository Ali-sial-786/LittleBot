"""The lists card (littlebot/screens/cards/lists.py): live from the Lists skill, tick boxes, paging, previews."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import lists as card_mod
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards.lists import ROW_H, ROWS, TICK_S, ListCard
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
    assert not card.tap(kit.PANEL.centerx, kit.PANEL.bottom - 20)  # under the rows: nothing


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


async def test_short_items_in_two_columns_eight_a_page(rig) -> None:
    ctx, skill = rig
    add(skill, "packing", *[f"thing {i}" for i in range(1, 13)])
    card = await base.build({"card": "lists", "list": "packing", "linger": 12}, ctx, base.RecordingHost())
    assert card.linger_seconds == 12.0 and card.columns == 2 and card.per_page == 6  # 12: 6 + 6, not 8 + 4
    assert card.pages == 2 and len(card.page_rows()) == 6 and card.status()["columns"] == 2
    assert card.describe() == ("Packing list: 12 items (page 1 of 2): thing 1, thing 2, thing 3, thing 4, thing 5, "
                               "thing 6")
    preview.surface(card, seconds=0.2)
    left, right = card._row_rect(0), card._row_rect(3)  # down the first column, then the second
    assert right.x > left.right and right.y == left.y and card._row_rect(2).y > card._row_rect(1).y
    back, fwd = card._arrows()
    assert card.tap(*fwd) and card.page == 1
    assert card.describe().endswith("(page 2 of 2): thing 7, thing 8, thing 9, thing 10, thing 11, thing 12")
    assert card.command("next", {}) and card.page == 1  # stays on the last page
    assert card.tap(*back) and card.page == 0
    assert card.key("right") and card.page == 1 and card.key("left") and card.page == 0
    assert card.command("page", {"n": 2}) and card.page == 1
    assert not card.command("page", {"n": 9})
    preview.surface(card, seconds=0.2)
    rect = card._row_rect(4)  # thing 11, in the second column
    card.tap(rect.centerx, rect.centery)
    run(card, TICK_S)
    assert card.pages == 2 and "thing 11" not in card.describe()
    for _ in range(3):  # down to 8: they all fit on one page
        rect = card._row_rect(0)
        assert card.tap(rect.centerx, rect.centery)
        run(card, TICK_S + 0.1)
    assert len(card.rows) == 8 and card.pages == 1 and card.page == 0  # the last page went: back one


async def test_long_items_in_one_column_four_a_page(rig) -> None:
    ctx, skill = rig
    long = [f"call the plumber about the leaking kitchen tap number {i}" for i in range(1, 7)]
    add(skill, "to-do", *long)
    card = await base.build({"card": "lists", "list": "to-do"}, ctx, base.RecordingHost())
    assert card.columns == 1 and card.pages == 2 and len(card.page_rows()) == 3 <= ROWS
    assert all(1 <= len(r["lines"]) <= 2 for r in card.rows)
    assert card.size >= base.BODY_PX
    assert sum(len(r["lines"]) for r in card.page_rows()) <= base.MAX_LINES
    preview.surface(card, seconds=0.2)
    rect = card._row_rect(2)
    assert card.tap(rect.centerx, rect.y + 4)  # anywhere on the row ticks it
    run(card, TICK_S)
    assert len(card.rows) == 5


async def test_rows_are_big_tick_targets_inside_the_panel() -> None:
    long = [f"a long to-do item number {i} to do" for i in range(5)]
    for items in (["milk"], [f"thing {i}" for i in range(9)], long):
        card = ListCard({"card": "lists"}, base.RecordingHost(), key="shopping", items=items)
        for page in range(card.pages):
            card.command("page", {"n": page + 1})
            rects = [card._row_rect(i) for i in range(len(card.page_rows()))]
            assert all(r.h >= ROW_H >= 90 and kit.PANEL.contains(r) for r in rects)
            assert not any(a.colliderect(b) for i, a in enumerate(rects) for b in rects[i + 1:])
            if card.pages > 1:  # the arrows sit under the rows
                assert all(r.bottom <= card._arrows()[0][1] - 30 for r in rects)


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
    lists = [s["items"] for s in card_mod.SAMPLES] + [["strawberries and blueberries"] + [f"x{i}" for i in range(9)],
                                                     ["a very long item " * 6] * 5]
    for items in lists:
        card = ListCard({"card": "lists"}, base.RecordingHost(), key="shopping", items=list(items))
        if card.rows:
            rect = card._row_rect(0)
            card.tap(rect.centerx, rect.centery)  # struck through too
            card.update(0.1)
        assert unpainted(card) == 0
        assert min(drawn_sizes(card, monkeypatch)) >= base.SMALL_PX
        assert not card.rows or card.size >= base.BODY_PX
        assert sum(len(r["lines"]) for r in card.page_rows()) <= base.MAX_LINES
