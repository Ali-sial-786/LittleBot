"""The "explain" card: key points revealed one at a time, paging, commands, readability."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import explain


class Pic:
    def __init__(self, surface: Any = None, ready: bool = True, credit: str = "Wikipedia") -> None:
        self.ready, self.surface, self.credit = ready, surface, credit


def host_with(picture: Any = None) -> base.RecordingHost:
    host = base.RecordingHost()
    if picture is not None:
        host.picture = lambda query="", url="": (host.asked_pictures.append((query, url)), picture)[1]  # type: ignore[method-assign]
    return host


async def make(spec: dict[str, Any], host: base.RecordingHost | None = None) -> explain.ExplainCard:
    card = await base.build({"card": "explain", **spec}, None, host or base.RecordingHost())
    assert isinstance(card, explain.ExplainCard)
    return card


def advance(card: base.Card, seconds: float, step: float = 0.1) -> None:
    for _ in range(round(seconds / step)):
        card.update(step)


SPEC = {"title": "How rainbows form", "points": ["Sunlight enters a raindrop", "The drop bends the light",
                                                 "Each colour bends differently", "The colours fan out"]}


def test_registered_with_fields_and_actions() -> None:
    ct = base.card_types()["explain"]
    assert ct.required == ("title", "points")
    assert {"title", "points", "picture_query", "pace"} <= set(ct.fields)
    assert {"next", "prev", "point"} <= set(ct.actions)
    assert "NOT a sentence" in ct.description


async def test_every_sample_builds_and_draws() -> None:
    for spec in explain.SAMPLES:
        card = await base.build(spec, None, base.RecordingHost())
        assert card is not None
        for seconds in (0.0, 1.0, 30.0):
            surf = preview.surface(card, seconds=seconds)
            assert surf.get_size() == (base.W, base.H)


async def test_no_points_no_card() -> None:
    assert await base.build({"card": "explain", "title": "x", "points": []}, None, base.RecordingHost()) is None
    assert await base.build({"card": "explain", "title": "x", "points": ["", "  "]}, None, base.RecordingHost()) is None
    assert await base.build({"card": "explain", "points": ["a", "b"]}, None, base.RecordingHost()) is None


async def test_points_appear_at_a_gentle_pace() -> None:
    card = await make({**SPEC, "pace": 3})
    assert card.describe().startswith("How rainbows form: about to show 4 points")
    advance(card, explain.FIRST_AT + 0.05)
    assert card.shown == 1
    assert card.describe() == "How rainbows form: point 1 of 4 showing: Sunlight enters a raindrop"
    advance(card, 3.0)
    assert card.shown == 2
    advance(card, 30)
    assert card.shown == 4 and "point 4 of 4" in card.describe()
    assert card.status()["point"] == 4


async def test_next_prev_point_and_all() -> None:
    card = await make(SPEC)
    advance(card, 1.0)
    assert card.command("next", {}) and card.shown == 2
    assert card.command("prev", {}) and card.shown == 1
    advance(card, explain.HOLD_S - 1)  # after going back it waits before revealing more
    assert card.shown == 1
    assert card.command("point", {"n": 3}) and card.shown == 3
    assert "Each colour bends differently" in card.describe()
    assert not card.command("point", {"n": 9}) and not card.command("point", {})
    assert card.command("all", {}) and card.shown == 4
    assert card.command("next", {}) and card.shown == 4  # nothing more: still understood
    assert not card.command("dance", {})


async def test_tap_and_keys() -> None:
    card = await make(SPEC)
    advance(card, 1.0)
    assert card.tap(600, 300) and card.shown == 2
    assert card.tap(50, 300) and card.shown == 1
    assert card.key("right") and card.shown == 2
    assert card.key("left") and card.shown == 1
    assert not card.key("x")


async def test_pages_turn_with_the_newest_point() -> None:
    points = ["A first idea that needs quite a few words here", "A second idea that also needs words",
              "A third idea that is long enough", "Fourth idea with words", "Fifth idea with more words"]
    card = await make({"title": "Ideas", "points": points, "picture_query": "idea"},
                      host_with(Pic(pygame.Surface((300, 200)))))
    pages = card.layout()["pages"]
    assert len(pages) >= 2
    assert sorted(i for p in pages for i in p) == list(range(5))
    advance(card, 0.8)
    assert card._page == 0
    card.command("point", {"n": 5})
    card.update(0.05)
    assert card._page == card.page_of(4) == len(pages) - 1


async def test_lines_fit_and_pages_hold_at_most_max_lines() -> None:
    long = "word " * 60
    card = await make({"title": "T", "points": [long, "short one", "another short one"]})
    for pic in (True, False):
        card.slot.picture = Pic(pygame.Surface((10, 10))) if pic else None
        card._layouts.clear()
        lay = card.layout()
        for lines in lay["lines"]:
            assert len(lines) <= base.MAX_LINES
            assert all(kit.text_w(s, lay["size"]) <= lay["width"] for s in lines)
        for page in lay["pages"]:
            assert sum(len(lay["lines"][i]) for i in page) <= base.MAX_LINES or len(page) == 1
        assert lay["size"] >= base.BODY_PX


async def test_picture_asked_for_and_layout_reflows_when_it_fails() -> None:
    loading = Pic(None, ready=False, credit="")
    host = host_with(loading)
    card = await make({**SPEC, "picture_query": "rainbow"}, host)
    assert host.asked_pictures == [("rainbow", "")]
    assert card.slot.wanted and card.layout()["size"] == explain.SIZE_NARROW
    preview.surface(card, seconds=0.5)  # the placeholder
    loading.ready = True  # failed: no surface
    card.update(0.1)
    assert not card.slot.wanted and card.layout()["size"] == explain.SIZE_WIDE
    preview.surface(card, seconds=0.5)


async def test_picture_fades_in() -> None:
    pic = Pic(None, ready=False)
    card = await make({**SPEC, "picture_query": "rainbow"}, host_with(pic))
    advance(card, 1.0)
    pic.surface, pic.ready = pygame.Surface((330, 220)), True
    card.update(0.05)
    assert card.slot.shown and card.slot.ready_at == pytest.approx(card.t)
    preview.surface(card, seconds=0.2)
    assert card.status()["picture"] == "shown"


async def test_presenter_box_stays_free_and_text_sizes_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = art.font
    monkeypatch.setattr(art, "font", lambda size: (sizes.append(size), real(size))[1])
    art.clear_caches()
    kit.wrap.cache_clear()
    card = await make({**SPEC, "picture_query": "rainbow"}, host_with(Pic(pygame.Surface((330, 220)))))
    advance(card, 30)
    surf = pygame.Surface((base.W, base.H))
    card.draw(surf)
    box = pygame.Rect(*base.PRESENTER_BOX)
    bg = art.background((base.W, base.H), seed=23)
    assert pygame.image.tobytes(surf.subsurface(box), "RGB") == pygame.image.tobytes(bg.subsurface(box), "RGB")
    assert sizes and min(sizes) >= base.SMALL_PX
