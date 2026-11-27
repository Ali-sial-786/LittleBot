"""The Lists skill (littlebot/skills/lists.py): named lists, guessing the list, merging, forgiving removal."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.skills import lists as L
from littlebot.skills.hub import SkillHub
from littlebot.skills.lists import Lists, clean_item, guess_list, item_norm, list_key, title
from littlebot.testing import make_test_context


class FakeScreens:
    def __init__(self) -> None:
        self.shown: list[dict[str, Any]] = []

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append(dict(spec))
        return {"ok": True}


@pytest.fixture
def ls():
    ctx = make_test_context()
    ctx.screens = FakeScreens()
    t = [1_800_000_000.0]

    def now() -> float:
        t[0] += 1
        return t[0]

    yield Lists(ctx, now=now)
    ctx.db.close()


async def run(s: Lists, name: str, **args: Any) -> str:
    return await s.run(name, args)


def items(s: Lists, name: str) -> list[str]:
    return [r["item"] for r in s.items(name)]


async def test_add_read_and_the_card(ls: Lists) -> None:
    out = await run(ls, "add_to_list", items=["milk", "eggs", "a loaf of bread"])
    assert out == "ok: added milk, eggs and loaf of bread to the Shopping list (now 3 items)"
    assert ls.ctx.screens.shown[-1] == {"card": "lists", "list": "shopping", "linger": L.CHANGED_LINGER_S}
    out = await run(ls, "add_to_list", items=["Eggs", "2% milk"], list="my grocery list")
    assert out == "ok: added 2% milk to the Shopping list (now 4 items); already on the Shopping list: eggs"
    assert await run(ls, "read_list") == "ok: Shopping list, 4 items: milk, eggs, loaf of bread, 2% milk"
    assert ls.ctx.screens.shown[-1] == {"card": "lists", "list": "shopping"}
    assert ls.context_note() == "Lists: shopping (4 items)."


async def test_the_list_is_guessed_from_the_items(ls: Lists) -> None:
    assert "to the To-do list" in await run(ls, "add_to_list", items=["call the plumber", "renew passport"])
    assert "to the Shopping list" in await run(ls, "add_to_list", items=["diapers", "baby wipes"])
    assert "to the Shopping list" in await run(ls, "add_to_list", items=["buy bananas"])
    assert items(ls, "shopping") == ["diapers", "baby wipes", "bananas"]  # "buy" dropped on the shopping list
    assert "to the To-do list" in await run(ls, "add_to_list", items=["finish the tax return"])
    assert "to the Packing list" in await run(ls, "add_to_list", items=["sunscreen", "passports"], list="Packing List")
    assert await run(ls, "list_lists") == "ok: shopping (3); to-do (3); packing (2)"
    assert ls.context_note() == "Lists: shopping (3 items), to-do (3 items), packing (2 items)."


async def test_items_as_one_string(ls: Lists) -> None:
    await run(ls, "add_to_list", items="apples, pears and plums")
    await run(ls, "add_to_list", items="mac and cheese")
    assert items(ls, "shopping") == ["apples", "pears", "plums", "mac and cheese"]


async def test_forgiving_removal(ls: Lists) -> None:
    await run(ls, "add_to_list", items=["2% milk", "bananas", "eggs", "oat milk", "Bread"], list="shopping")
    await run(ls, "add_to_list", items=["call mom"], list="to-do")
    out = await run(ls, "remove_from_list", items=["banana", "bread"])
    assert out == "ok: took bananas and Bread off the Shopping list (3 left)"
    out = await run(ls, "remove_from_list", items=["milk"])
    assert out == "ok: 'milk' could be 2% milk or oat milk: ask which"
    out = await run(ls, "remove_from_list", items=["the oat milk"])
    assert out == "ok: took oat milk off the Shopping list (2 left)"
    assert await run(ls, "remove_from_list", items=["milk"]) == "ok: took 2% milk off the Shopping list (1 left)"
    assert await run(ls, "remove_from_list", items=["egs"]) == "ok: took eggs off the Shopping list (0 left)"
    assert await run(ls, "remove_from_list", items=["called mom"]) == "ok: took call mom off the To-do list (0 left)"
    assert await run(ls, "remove_from_list", items=["cheese"]) == "ok: not on any list: cheese"
    out = await run(ls, "remove_from_list", items=["cheese"], list="packing")
    assert out == "ok: not on the Packing list: cheese"


async def test_clear_and_empty_lists(ls: Lists) -> None:
    assert await run(ls, "list_lists") == "ok: no lists yet (the shopping and to-do lists are empty)"
    assert await run(ls, "read_list") == "ok: the Shopping list is empty"
    await run(ls, "add_to_list", items=["tent", "torch"], list="camping")
    assert await run(ls, "read_list") == "ok: Camping list, 2 items: tent, torch"  # the only list
    assert await run(ls, "read_list", list="to do") == "ok: the To-do list is empty; lists with items: camping (2)"
    assert await run(ls, "clear_list", list="camping list") == "ok: cleared the Camping list (2 items)"
    assert await run(ls, "clear_list", list="camping") == "ok: the Camping list was already empty"
    assert ls.context_note() == ""


@pytest.mark.parametrize("name, args, words", [
    ("add_to_list", {}, "give the items"),
    ("add_to_list", {"items": []}, "give the items"),
    ("add_to_list", {"items": 42}, "give the items"),
    ("add_to_list", {"items": ["  ", "..."]}, "empty"),
    ("add_to_list", {"items": ["milk"], "list": "the list"}, "empty"),
    ("add_to_list", {"items": ["milk"], "list": 5}, "list's name"),
    ("remove_from_list", {"items": None}, "give the items"),
    ("clear_list", {}, "which list"),
    ("nope", {}, "no tool"),
])
async def test_bad_arguments(ls: Lists, name: str, args: dict[str, Any], words: str) -> None:
    out = await ls.run(name, args)
    assert out.startswith("error:") and words in out


async def test_limits(ls: Lists, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "MAX_ITEMS", 3)
    out = await run(ls, "add_to_list", items=["a1", "b2", "c3", "d4"], list="x")
    assert "now 3 items" in out and "full" in out
    monkeypatch.setattr(L, "MAX_LISTS", 1)
    assert "already 1 lists" in await run(ls, "add_to_list", items=["y"], list="other")
    long = "z" * 200
    await run(ls, "add_to_list", items=[long], list="x")
    assert all(len(i) <= L.MAX_ITEM for i in items(ls, "x"))


async def test_tick_off_for_the_card(ls: Lists) -> None:
    await run(ls, "add_to_list", items=["milk", "eggs"])
    v = ls.version
    first = ls.items("shopping")[0]
    assert ls.tick_off("shopping", first["id"]) == "milk" and ls.version == v + 1
    assert ls.tick_off("shopping", first["id"]) is None
    assert items(ls, "Shopping") == ["eggs"]


async def test_switched_off_and_the_hub() -> None:
    ctx = make_test_context(skills={"lists": False})
    s = Lists(ctx)
    assert SkillHub(ctx, [s]).specs() == [] and s.context_note() == ""
    assert (await s.run("read_list", {})).startswith("error: lists are switched off")
    ctx = make_test_context()
    hub = SkillHub(ctx, [Lists(ctx)])
    assert [t["name"] for t in hub.specs()] == ["add_to_list", "remove_from_list", "read_list", "clear_list",
                                                "list_lists"]
    assert (await hub.run("add_to_list", '{"items": ["milk"]}')).startswith("ok: added milk")  # no screen: fine
    assert hub.status()["skills"][0]["lists"] == {"shopping": 1}


def test_names_and_items() -> None:
    for said, key in (("Shopping", "shopping"), ("my grocery list", "shopping"), ("Groceries", "shopping"),
                      ("To Do", "to-do"), ("todo list", "to-do"), ("our Packing List", "packing"),
                      ("Costco", "costco"), ("Amara's list", "amara's"), ("list", ""), (None, ""), (3, "")):
        assert list_key(said) == key, said
    assert title("to-do") == "To-do list" and title("costco") == "Costco list" and title("") == "List"
    assert clean_item("  some fresh Eggs. ") == "fresh Eggs" and clean_item(["x"]) == ""
    assert clean_item("buy milk", shopping=True) == "milk" and clean_item("buy a present") == "buy a present"
    assert item_norm("Tomatoes") == item_norm("tomato") and item_norm("the Berries") == "berry"
    assert item_norm("2% Milk") == "2% milk" and item_norm("glasses") == "glass"
    assert guess_list(["milk", "call the bank"]) == "shopping"
    assert guess_list(["call the bank", "email the school"]) == "to-do"
    assert guess_list(["a 2 kg bag of rice"]) == "shopping" and guess_list(["water the plants"]) == "to-do"
    assert guess_list(["sparkling water"]) == "shopping" and guess_list(["something odd"]) == "to-do"
