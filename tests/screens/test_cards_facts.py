"""The "facts" card: its six layouts, what each needs, paging and readability."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import facts


class Pic:
    def __init__(self, surface: Any = None, ready: bool = True, credit: str = "Wikipedia") -> None:
        self.ready, self.surface, self.credit = ready, surface, credit


async def make(spec: dict[str, Any], host: base.RecordingHost | None = None) -> facts.FactsCard:
    card = await base.build({"card": "facts", **spec}, None, host or base.RecordingHost())
    assert isinstance(card, facts.FactsCard)
    return card


def sample(layout: str, n: int = 0) -> dict[str, Any]:
    return [s for s in facts.SAMPLES if s["layout"] == layout][n]


def test_registered_with_layouts_fields_and_actions() -> None:
    ct = base.card_types()["facts"]
    assert ct.required == ("layout",)
    assert set(ct.fields["layout"]["enum"]) == set(facts.LAYOUTS) == {"number", "list", "compare", "profile",
                                                                       "score", "item"}
    for layout in facts.LAYOUTS:
        assert f'"{layout}"' in ct.description
    assert {"next", "prev", "page"} <= set(ct.actions)
    assert {s["layout"] for s in facts.SAMPLES} == set(facts.LAYOUTS)


async def test_every_sample_builds_and_draws() -> None:
    for spec in facts.SAMPLES:
        for picture in (Pic(pygame.Surface((330, 220))), Pic(pygame.Surface((200, 300))), Pic(None, ready=False),
                        Pic(None)):
            card = await base.build(spec, None, base.RecordingHost(pictures={spec.get("picture_query", ""): picture}))
            assert card is not None, spec
            for seconds in (0.0, 1.0, 6.0):
                assert preview.surface(card, seconds=seconds).get_size() == (base.W, base.H)
            card.command("next", {})
            preview.surface(card, seconds=0.5)


@pytest.mark.parametrize(("spec", "said"), [
    (sample("number"), "Eiffel Tower: 330 m tall"),
    (sample("score"), "Arsenal 2, Chelsea 1, Full time"),
    (sample("item"), ("Today in history: 24 October 1945: The United Nations was founded, to help countries work "
                     "together for peace.")),
    (sample("compare"), ("Lion or tiger?: Lion vs Tiger: Weight: 190 kg, 220 kg; Lives in: Africa, Asia; "
                        "Lives: In groups, Alone")),
])
async def test_describe(spec: dict[str, Any], said: str) -> None:
    assert (await make(spec)).describe() == said


@pytest.mark.parametrize("spec", [
    {"layout": "number", "title": "x"},
    {"layout": "list", "items": []},
    {"layout": "compare", "columns": [{"name": "A", "facts": ["1"]}]},
    {"layout": "compare", "columns": [{"name": "A"}, {"name": "B"}]},
    {"layout": "profile", "title": "Giraffe"},
    {"layout": "score", "teams": [{"name": "A", "score": 1}]},
    {"layout": "item", "kicker": "Today"},
    {"title": "no layout", "text": "x"},
])
async def test_what_each_layout_needs(spec: dict[str, Any]) -> None:
    assert await base.build({"card": "facts", **spec}, None, base.RecordingHost()) is None


LION_TIGER = "Lion or tiger?: Lion vs Tiger: Weight: 190 kg, 220 kg; Lives in: Africa, Asia"


@pytest.mark.parametrize(("spec", "said"), [
    ({"rows": [{"label": "Weight", "Lion": "190 kg", "Tiger": "220 kg"},  # records, a key per column
               {"label": "Lives in", "Lion": "Africa", "Tiger": "Asia"}]}, LION_TIGER),
    ({"columns": ["Lion", "Tiger"],  # names as columns, each row a label and its values
      "rows": [{"label": "Weight", "values": ["190 kg", "220 kg"]}, {"label": "Lives in", "values": ["Africa", "Asia"]}]},
     LION_TIGER),
    ({"rows": [["", "Lion", "Tiger"], ["Weight", "190 kg", "220 kg"], ["Lives in", "Africa", "Asia"]]},  # a table
     LION_TIGER),
    ({"columns": [["Lion", "190 kg", "Africa"], ["Tiger", "220 kg", "Asia"]], "rows": ["Weight", "Lives in"]},
     LION_TIGER),
    ({"rows": [{"name": "Lion", "facts": ["190 kg", "Africa"]}, {"name": "Tiger", "facts": ["220 kg", "Asia"]}]},
     "Lion or tiger?: Lion vs Tiger: 190 kg, 220 kg; Africa, Asia"),  # the columns under the wrong name
    # names as columns, each row a line of text with both values (what Haiku sent on the robot, 2026-10-29)
    ({"columns": ["Lion", "Tiger"], "rows": ["Weight: 190 kg vs 220 kg", "Lives in: Africa | Asia"]}, LION_TIGER),
    ({"rows": ["Weight: 190 kg / 220 kg", "Lives in: Africa; Asia"]}, LION_TIGER),  # names only in the title
])
async def test_a_comparison_written_as_a_table_still_shows(spec: dict[str, Any], said: str) -> None:
    from littlebot.screens.director import clean_spec

    cleaned, why = clean_spec({"card": "facts", "layout": "compare", "title": "Lion or tiger?", **spec},
                              base.card_types())
    assert cleaned is not None, why  # the director keeps the table's shape for the card
    card = await make(cleaned)
    assert card.layout == "compare" and card.describe() == said
    preview.surface(card, seconds=1.0)


async def test_a_comparison_with_names_only_in_the_title_and_values_by_row() -> None:
    card = await make({"layout": "compare", "title": "Genghis Khan vs Kublai Khan",
                       "rows": [{"label": "Known for", "values": ["Conquest", "Ruling China"]}]})
    assert [c["name"] for c in card.columns] == ["Genghis Khan", "Kublai Khan"] and card.rows == ["Known for"]


async def test_a_comparison_with_a_line_for_each_of_them() -> None:
    card = await make({"layout": "compare", "title": "Genghis Khan or Kublai Khan?",
                       "rows": ["Genghis: united the tribes", "Kublai: ruled China", "Genghis Khan: conquered Asia"]})
    assert card.columns == [{"name": "Genghis Khan", "facts": ["united the tribes", "conquered Asia"]},
                            {"name": "Kublai Khan", "facts": ["ruled China"]}] and card.rows == []


async def test_an_empty_comparison_says_what_it_was_sent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="littlebot.screens")
    assert await base.build({"card": "facts", "layout": "compare", "title": "A or B", "rows": ["Size", "Age"]},
                            None, base.RecordingHost()) is None
    assert "rows=[2 x str]" in caplog.text and "title=str" in caplog.text


async def test_a_profile_without_its_title_shows_its_facts_as_a_list() -> None:
    card = await base.build({"card": "facts", "layout": "profile", "facts": ["a"]}, None, base.RecordingHost())
    assert card is not None and card.layout == "list"


async def test_unknown_layout_is_an_item() -> None:
    card = await make({"layout": "poster", "text": "Hello"})
    assert card.layout == "item" and card.describe() == "Hello"


async def test_pictures_only_for_layouts_that_show_them() -> None:
    for layout in facts.LAYOUTS:
        host = base.RecordingHost()
        spec = {**sample(layout), "picture_query": "thing"}
        await make(spec, host)
        wants = layout in ("number", "list", "profile", "item")
        assert bool(host.asked_pictures) == wants, layout


async def test_list_pages() -> None:
    card = await make(sample("list"))
    pages = card.pages()
    assert [len(p) for p in pages] == [4, 4]  # eight planets, evenly paged
    assert card.describe() == "Planets in order: Mercury, Venus, Earth, Mars (page 1 of 2)"
    assert card.command("next", {}) and card.describe() == "Planets in order: Jupiter, Saturn, Uranus, Neptune (page 2 of 2)"
    assert card.command("next", {}) and card.page == 1
    assert card.command("prev", {}) and card.page == 0
    assert card.command("page", {"n": 2}) and card.page == 1
    assert not card.command("page", {"n": 3}) and not card.command("shuffle", {})
    assert card.tap(50, 200) and card.page == 0
    assert card.key("right") and card.page == 1
    assert card.status()["pages"] == 2 and card.status()["layout"] == "list"


async def test_single_page_layouts_ignore_taps() -> None:
    card = await make(sample("number"))
    assert len(card.pages()) == 1 and not card.tap(600, 300)


async def test_text_fits_and_pages_hold_at_most_max_lines() -> None:
    long_items = [f"item {i} " + "with quite a few extra words " * 2 for i in range(9)]
    for pic in (Pic(pygame.Surface((300, 200))), None):
        host = base.RecordingHost(pictures={"x": pic} if pic else None)
        card = await make({"layout": "list", "title": "Long", "items": long_items, "picture_query": "x"}, host)
        width = card._text_right(card.slot.wanted) - (kit.PANEL.x + kit.PAD + 58)
        for page in card.pages():
            assert sum(len(lines) for _, lines in page) <= base.MAX_LINES
            assert all(kit.text_w(s, facts.LIST_PX) <= width for _, lines in page for s in lines)
    card = await make({"layout": "profile", "title": "Owl", "subtitle": "Bird",
                       "facts": ["a fact that is long enough to wrap onto more than one line"] * 3})
    for page in card.pages():
        assert sum(len(lines) for _, lines in page) <= base.MAX_LINES
    rows = [f"Row {i}" for i in range(8)]
    card = await make({"layout": "compare", "rows": rows,
                       "columns": [{"name": "A", "facts": [f"value {i}" for i in range(8)]},
                                   {"name": "B", "facts": [f"value {i}" for i in range(8)]},
                                   {"name": "C", "facts": ["x"]}]})
    assert len(card.pages()) >= 2
    for page in card.pages():
        assert sum(card._row_lines(r) for r in page) <= base.MAX_LINES - 1
    card.command("next", {})
    assert "(page 2 of" in card.describe()
    preview.surface(card, seconds=1)


async def test_compare_without_row_labels_lines_facts_up() -> None:
    card = await make(sample("compare", 1))
    assert card._col_geometry()[2] == 0  # no label column
    assert card.describe().startswith("Milk for babies: Breast milk vs Formula vs Cow's milk: ")
    for c in range(3):
        for r in range(2):
            lines = card._cell_lines(c, r)
            assert len(lines) <= 2 and all(kit.text_w(s, facts.CELL_PX) <= card._col_geometry()[1] - 16
                                           for s in lines)


async def test_item_text_size_and_lines() -> None:
    short = await make({"layout": "item", "text": "Octopuses have three hearts."})
    size, lines = kit.fit(short.text, facts.ITEM_SIZES, 600)
    assert size == facts.ITEM_SIZES[0] and len(lines) <= base.MAX_LINES
    long = "word " * 45
    size, lines = kit.fit(long.strip(), facts.ITEM_SIZES, 380)
    assert size >= base.BODY_PX


async def test_presenter_box_stays_free_and_text_sizes_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    box = pygame.Rect(*base.PRESENTER_BOX)
    bg = art.background((base.W, base.H), seed=53)
    for spec in facts.SAMPLES:
        sizes: list[int] = []
        real = art.font
        monkeypatch.setattr(art, "font", lambda size, real=real, sizes=sizes: (sizes.append(size), real(size))[1])
        art.clear_caches()
        kit.wrap.cache_clear()
        card = await base.build(spec, None, base.RecordingHost(pictures={spec.get("picture_query", ""): Pic(
            pygame.Surface((330, 220)))}))
        for _ in range(20):
            card.update(0.25)
        surf = pygame.Surface((base.W, base.H))
        card.draw(surf)
        assert pygame.image.tobytes(surf.subsurface(box), "RGB") == pygame.image.tobytes(bg.subsurface(box), "RGB")
        assert sizes and min(sizes) >= base.SMALL_PX, spec["layout"]
        monkeypatch.setattr(art, "font", real)
