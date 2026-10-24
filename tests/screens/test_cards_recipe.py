"""The "recipe" card: ingredients, big steps, two at a time, timers, stickiness and when it is done."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import recipe


class Pic:
    def __init__(self, surface: Any = None, ready: bool = True, credit: str = "Wikipedia") -> None:
        self.ready, self.surface, self.credit = ready, surface, credit


async def make(spec: dict[str, Any], host: base.RecordingHost | None = None) -> recipe.RecipeCard:
    card = await base.build({"card": "recipe", **spec}, None, host or base.RecordingHost())
    assert isinstance(card, recipe.RecipeCard)
    return card


def advance(card: base.Card, seconds: float, step: float = 1.0) -> None:
    for _ in range(round(seconds / step)):
        card.update(step)


PANCAKES = recipe.SAMPLES[0]
STEPS = ["Mash the bananas", "Whisk in the eggs", "Stir in the oats", "Rest for 5 minutes", "Cook 2 minutes a side"]


def test_registered_with_fields_and_actions() -> None:
    ct = base.card_types()["recipe"]
    assert ct.required == ("title", "steps")
    assert ct.fields["steps_at_once"]["enum"] == [1, 2]
    assert {"next", "prev", "step", "ingredients", "close"} <= set(ct.actions)
    assert recipe.RecipeCard.sticky is True


async def test_every_sample_builds_and_every_screen_draws() -> None:
    for spec in recipe.SAMPLES:
        for picture in (Pic(pygame.Surface((330, 220))), Pic(None, ready=False), Pic(None)):
            card = await base.build(spec, None, base.RecordingHost(pictures={spec.get("picture_query", ""): picture}))
            assert card is not None
            for _ in range(len(card.screens())):
                assert preview.surface(card, seconds=0.5).get_size() == (base.W, base.H)
                card.command("next", {})


async def test_no_steps_no_card() -> None:
    host = base.RecordingHost()
    assert await base.build({"card": "recipe", "title": "Toast", "steps": []}, None, host) is None
    assert await base.build({"card": "recipe", "title": "Toast", "steps": ["", " "]}, None, host) is None
    assert await base.build({"card": "recipe", "steps": ["Toast it"]}, None, host) is None


async def test_ingredients_first_then_steps_then_enjoy() -> None:
    host = base.RecordingHost()
    card = await make(PANCAKES, host)
    assert host.asked_pictures == [("pancakes", "")]
    assert card.view == "ingredients"
    first = card.describe()
    assert first.startswith("Ingredients for Banana pancakes (page 1 of 2): 2 ripe bananas, 2 eggs")
    assert card.command("next", {}) and card.view == "ingredients" and "page 2 of 2" in card.describe()
    assert card.command("next", {}) and card.describe() == "Step 1 of 7: Mash the bananas in a bowl"
    assert card.status()["step"] == 1 and card.status()["steps"] == 7
    assert card.command("step", {"n": 3}) and card.describe() == ("Step 3 of 7: Stir in the oats, baking powder "
                                                                  "and cinnamon")
    assert card.command("prev", {}) and card.describe().startswith("Step 2 of 7")
    assert card.command("ingredients", {}) and card.describe() == first
    assert card.command("step", {"n": 7}) and card.command("next", {})
    assert card.view == "finished" and card.describe() == "Banana pancakes: all 7 steps done"
    assert host.sounds[-1] == "tada" and "pop" in host.sounds
    assert card.command("next", {}) and card.view == "finished"  # nothing after "enjoy"
    assert not card.command("step", {"n": 0}) and not card.command("step", {"n": 8})
    assert not card.command("stir", {})


async def test_without_ingredients_it_starts_at_step_one() -> None:
    card = await make({"title": "Toast", "steps": ["Toast the bread", "Butter it"]})
    assert card.describe() == "Step 1 of 2: Toast the bread"
    assert not card.command("ingredients", {})
    assert card.command("start", {}) and card.steps_showing() == [0]


async def test_two_steps_at_a_time() -> None:
    card = await make({"title": "Soup", "steps_at_once": 2, "steps": STEPS})
    screens = [s for s in card.screens() if s["view"] == "steps"]
    assert [[i for i, _ in s["steps"]] for s in screens] == [[0, 1], [2, 3], [4]]
    assert card.describe() == "Steps 1 and 2 of 5: Mash the bananas Then: Whisk in the eggs"
    assert card.command("step", {"n": 4}) and card.steps_showing() == [2, 3]
    assert card.command("next", {}) and card.describe().startswith("Step 5 of 5")
    for s in screens:
        assert s["size"] >= base.STEP_PX
        assert sum(len(lines) for _, lines in s["steps"]) <= base.MAX_LINES


async def test_a_pair_too_long_for_one_screen_is_split() -> None:
    long = "Carefully fold the beaten egg whites into the chocolate mixture in three parts without knocking out air"
    card = await make({"title": "Mousse", "steps_at_once": 2, "steps": [long, long, "Chill"]})
    groups = [[i for i, _ in s["steps"]] for s in card.screens() if s["view"] == "steps"]
    assert groups[0] == [0]  # too long to pair with the next one
    assert sorted(i for g in groups for i in g) == [0, 1, 2]


async def test_steps_are_big_and_never_more_than_max_lines() -> None:
    huge = " ".join(["MMMMMM"] * 28)  # 195 characters (steps are capped at 200) of the widest letters
    card = await make({"title": "Long", "steps": ["Stir in the flour", huge]})
    screens = [s for s in card.screens() if s["view"] == "steps"]
    assert screens[0]["size"] == recipe.STEP_SIZES[0]  # a short step gets the biggest size
    parts = [s for s in screens if s["steps"][0][0] == 1]
    assert len(parts) >= 2 and parts[0]["parts"] == len(parts)
    width = card._step_width()
    for s in screens:
        assert s["size"] >= base.STEP_PX
        for _, lines in s["steps"]:
            assert len(lines) <= base.MAX_LINES
            for line in lines:
                w = sum(recipe._tok_w(t, s["size"]) for t in line) + kit.text_w(" ", s["size"]) * (len(line) - 1)
                assert w <= width
    card.command("step", {"n": 2})
    assert card.describe().startswith("Step 2 of 2:")
    card.command("next", {})
    assert card.describe().startswith("Step 2 of 2 (continued):")


@pytest.mark.parametrize(("text", "phrase", "seconds"), [
    ("Simmer for 10 minutes", "10 minutes", 600),
    ("Bake 1 hour 20 mins until golden", "1 hour 20 mins", 4800),
    ("Cook for 3 to 5 minutes, stirring", "3 to 5 minutes", 180),
    ("Rest for half an hour", "half an hour", 1800),
    ("Boil for about ten minutes", "ten minutes", 600),
    ("Microwave for 30 seconds", "30 seconds", 30),
    ("Roast for an hour and a half", "an hour and a half", 5400),
    ("Let it cool for a minute", "a minute", 60),
    ("Leave 5-7 minutes", "5-7 minutes", 300),
])
def test_finding_a_cooking_time(text: str, phrase: str, seconds: int) -> None:
    found = recipe.find_timer(text)
    assert found is not None
    a, b, s = found
    assert text[a:b] == phrase and s == seconds


@pytest.mark.parametrize("text", ["Add 2 cups of flour", "Serve with 3 eggs", "Preheat the oven to 200C", ""])
def test_no_cooking_time(text: str) -> None:
    assert recipe.find_timer(text) is None


def test_short_time_and_tokens() -> None:
    assert recipe.short_time(600) == "10 min" and recipe.short_time(5400) == "1 h 30 min"
    assert recipe.short_time(3600) == "1 h" and recipe.short_time(45) == "45 s"
    toks = recipe.tokens("Cook for 3 to 5 minutes, stirring often")
    assert ("3 to 5 minutes", True, False) in toks and (",", False, True) in toks
    assert recipe._segments(toks) == [("Cook for", False, False), ("3 to 5 minutes", True, False),
                                      (", stirring often", False, True)]


async def test_timer_shows_in_status_for_the_director() -> None:
    card = await make({"title": "Soup", "steps": STEPS})
    assert card.status()["timer_seconds"] is None
    card.command("step", {"n": 4})
    assert card.status()["timer_seconds"] == 300 and card.status()["view"] == "steps"
    preview.surface(card, seconds=0.5)


async def test_taps_move_on_and_tell_the_voice() -> None:
    host = base.RecordingHost()
    card = await make({"title": "Soup", "steps": STEPS}, host)
    assert card.tap(600, 300) and card.steps_showing() == [1]
    assert host.told[-1] == "The screen shows Step 2 of 5: Whisk in the eggs"
    assert card.tap(40, 300) and card.steps_showing() == [0]
    assert card.key("space") and card.steps_showing() == [1]
    assert not card.key("q")


async def test_sticky_until_done() -> None:
    card = await make({"title": "Soup", "steps": STEPS})
    advance(card, 1800)  # half an hour simmering without a touch: still there
    assert not card.done
    card.command("step", {"n": 5})
    advance(card, recipe.DONE_AFTER_LAST_S - 10)
    assert not card.done
    card.command("prev", {})
    card.command("next", {})  # touched again: the clock starts over
    advance(card, recipe.DONE_AFTER_LAST_S - 10)
    assert not card.done
    advance(card, 20)
    assert card.done


async def test_enjoy_screen_goes_after_a_while_and_close_ends_it() -> None:
    card = await make({"title": "Soup", "steps": STEPS})
    card.command("step", {"n": 5})
    card.command("next", {})
    advance(card, recipe.FINISHED_DONE_S - 2)
    assert not card.done
    advance(card, 3)
    assert card.done
    other = await make({"title": "Soup", "steps": STEPS})
    assert other.command("close", {}) and other.done


async def test_an_hour_untouched_mid_recipe_is_done() -> None:
    card = await make({"title": "Soup", "steps": STEPS})
    card.command("step", {"n": 2})
    advance(card, recipe.IDLE_DONE_S - 5)
    assert not card.done
    advance(card, 10)
    assert card.done


async def test_failed_picture_reflows_the_ingredients_and_keeps_the_place() -> None:
    pic = Pic(None, ready=False)
    many = [f"Ingredient number {i} with a long name" for i in range(9)]
    card = await make({"title": "Stew", "ingredients": many, "steps": STEPS, "picture_query": "stew"},
                      base.RecordingHost(pictures={"stew": pic}))
    card.command("next", {})
    shown_first = card.current()["items"][0][0]
    pic.ready = True  # failed
    card.update(0.1)
    assert not card.slot.wanted
    assert any(i == shown_first for i, _ in card.current()["items"])
    card.command("step", {"n": 1})
    pic2 = Pic(None, ready=False)
    card2 = await make({"title": "Stew", "ingredients": many, "steps": STEPS, "picture_query": "stew"},
                       base.RecordingHost(pictures={"stew": pic2}))
    card2.command("step", {"n": 2})
    pic2.ready = True
    card2.update(0.1)
    assert card2.steps_showing() == [1]


async def test_ingredient_pages_fit() -> None:
    card = await make(PANCAKES, base.RecordingHost(pictures={"pancakes": Pic(pygame.Surface((300, 200)))}))
    for scr in card.screens():
        if scr["view"] == "ingredients":
            assert sum(len(lines) for _, lines in scr["items"]) <= base.MAX_LINES
    assert recipe.INGREDIENT_PX >= base.BODY_PX


async def test_presenter_box_stays_free_and_text_sizes_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = art.font
    monkeypatch.setattr(art, "font", lambda size: (sizes.append(size), real(size))[1])
    art.clear_caches()
    kit.wrap.cache_clear()
    card = await make(PANCAKES, base.RecordingHost(pictures={"pancakes": Pic(pygame.Surface((330, 220)))}))
    box = pygame.Rect(*base.PRESENTER_BOX)
    bg = art.background((base.W, base.H), seed=41)
    for _ in range(len(card.screens())):
        advance(card, 2.0, 0.25)
        surf = pygame.Surface((base.W, base.H))
        card.draw(surf)
        assert pygame.image.tobytes(surf.subsurface(box), "RGB") == pygame.image.tobytes(bg.subsurface(box), "RGB")
        card.command("next", {})
    assert sizes and min(sizes) >= base.SMALL_PX
