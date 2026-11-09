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
    (sample("number"), "Eiffel Tower: 330 m tall (Built: 1889; Steps: 1,665; Visitors: 7 million a year)"),
    ({**sample("number"), "stats": []}, "Eiffel Tower: 330 m tall"),
    (sample("score"), "Arsenal 2 (Saka 23', Havertz 70'), Chelsea 1 (Palmer 81'), Full time"),
    ({"layout": "score", "teams": [{"name": "A", "score": "2"}, {"name": "B", "score": "1"}], "status": "Full time"},
     "A 2, B 1, Full time"),
    (sample("profile"), "Giraffe (Animal): Tallest animal on land; Sleeps only about 30 minutes a day; Its tongue is "
                        "about 50 cm long; Lives in African savannas (Height: 5.5 m; Weight: 1,200 kg; "
                        "Lives: 25 years; Top speed: 60 km/h)"),
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
    {"layout": "profile", "title": "Giraffe"},
    {"layout": "item", "kicker": "Today"},
    {"title": "no layout", "text": "x"},
])
async def test_what_each_layout_needs(spec: dict[str, Any]) -> None:
    assert await base.build({"card": "facts", **spec}, None, base.RecordingHost()) is None


@pytest.mark.parametrize(("spec", "items"), [
    ({"layout": "compare", "columns": [{"name": "A", "facts": ["1"]}]}, ["A", "1"]),
    ({"layout": "compare", "columns": [{"name": "A"}, {"name": "B"}]}, ["A", "B"]),
    ({"layout": "score", "teams": [{"name": "A", "score": 1}]}, ["A", "score: 1"]),
])
async def test_a_layout_missing_its_part_still_shows_its_text(spec: dict[str, Any], items: list[str]) -> None:
    card = await make(spec)
    assert card.layout == "list" and card.items == items


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


async def test_a_comparison_it_cannot_read_still_shows_its_text(caplog: pytest.LogCaptureFixture) -> None:
    # whatever shape it comes in, it is shown (the parent's ask, 2026-10-29); the log says how it came
    caplog.set_level("INFO", logger="littlebot.screens")
    card = await make({"layout": "compare", "title": "Genghis Khan vs Kublai Khan",
                       "columns": ["Genghis Khan", "Kublai Khan"],
                       "rows": ["Genghis Khan built the empire through conquest across Asia.",
                                "Kublai Khan, his grandson, ruled China as emperor."]})
    assert card.layout == "list" and card.items[0].startswith("Genghis Khan built") and len(card.items) == 2
    assert "rows: 9 words, marks .; 8 words, marks ,." in caplog.text  # its shape, never its words
    assert "conquest" not in caplog.text


async def test_any_text_in_any_shape_is_shown() -> None:
    card = await make({"layout": "compare", "title": "A or B", "rows": ["Size", "Age"]})
    assert card.layout == "list" and card.items == ["Size", "Age"]
    card = await make({"layout": "score", "teams": [{"name": "Lions", "score": 3}], "stuff": {"when": "Sunday"}})
    assert card.layout == "list" and card.items == ["Lions", "score: 3", "when: Sunday"]


async def test_a_card_with_no_text_at_all_is_not_made(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="littlebot.screens")
    assert await base.build({"card": "facts", "layout": "compare", "title": "A or B"}, None,
                            base.RecordingHost()) is None
    assert "facts card not made" in caplog.text and "title=str" in caplog.text


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


async def test_eight_short_items_share_one_page_in_two_columns() -> None:
    card = await make(sample("list"))
    assert [len(p) for p in card.pages()] == [8] and card.list_columns == 2  # all eight planets at once
    assert card.describe() == "Planets in order: Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus, Neptune"
    assert not card.tap(50, 200)  # one page: nothing to turn
    pic = await make({**sample("list"), "picture_query": "planets"},
                     base.RecordingHost(pictures={"planets": Pic(pygame.Surface((300, 200)))}))
    assert pic.list_columns == 1 and sum(len(p) for p in pic.pages()) == 8  # beside a picture: one column
    long = await make({"layout": "list", "items": ["a rather long item that needs most of a line here"] * 6})
    assert long.list_columns == 1
    few = await make({"layout": "list", "items": ["one", "two", "three"]})
    assert few.list_columns == 1
    preview.surface(card, seconds=1)


async def test_list_pages() -> None:
    card = await make(sample("list", 2))  # twelve names
    pages = card.pages()
    assert [len(p) for p in pages] == [6, 6] and card.list_columns == 2  # evenly paged, MAX_LINES at most
    assert card.describe() == "Baby names this year: Olivia, Noah, Amelia, Muhammad, Isla, Oliver (page 1 of 2)"
    assert card.command("next", {}) and card.describe() == ("Baby names this year: Lily, George, Freya, Leo, Ava, "
                                                            "Arthur (page 2 of 2)")
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
        width = card._text_right(card.slot.wanted) - (kit.PANEL.x + kit.PAD + facts.LIST_INDENT)
        for page in card.pages():
            assert sum(len(lines) for _, lines in page) <= base.MAX_LINES
            assert all(kit.text_w(s, facts.LIST_PX) <= width for _, lines in page for s in lines)
            pitch = kit.line_h(facts.LIST_PX)
            height = sum(pitch * len(ls) for _, ls in page) + facts.LIST_GAP * (len(page) - 1)
            assert height <= card.inner.h  # nothing runs off the panel
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
    size, lines = facts.item_fit(short.text, 900, 380)
    assert size == facts.ITEM_SIZES[0] and len(lines) == 1
    long = " ".join(["word"] * 45)
    for width, height in ((900, 380), (500, 300), (500, 120)):
        size, lines = facts.item_fit(long, width, height)
        assert size >= base.BODY_PX and len(lines) <= base.MAX_LINES
        assert kit.line_h(size) * (len(lines) - 1) + art.font(size).get_height() <= max(height, kit.line_h(size))
        assert all(kit.text_w(s, size) <= width for s in lines)
    size, lines = facts.item_fit(long, 500, 120)
    assert size == base.BODY_PX and lines[-1].endswith("...")  # too much even at BODY_PX: cut short, not shrunk


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


@pytest.mark.parametrize(("raw", "stats"), [
    (["Height: 5.5 m", {"label": "Weight", "value": "1,200 kg"}, "Fast", 7, None, {"label": "x"}, ":"],
     [("Height", "5.5 m"), ("Weight", "1,200 kg"), ("", "Fast"), ("", "7")]),  # four at most, junk dropped
    ("Built: 1889", [("Built", "1889")]),  # one stat as a plain string
    ([{"name": "Speed", "number": 60}], [("Speed", "60")]),
    (None, []),
])
async def test_stats(raw: Any, stats: list[tuple[str, str]]) -> None:
    card = await make({"layout": "number", "value": "1", "stats": raw})
    assert card.stats == stats


async def test_stat_tiles_fit_the_width_in_even_rows() -> None:
    card = await make(sample("profile"))
    for width in (card.inner.w, 536, 300):
        rows = card._stat_rows(width)
        assert sorted(i for r in rows for i in r) == list(range(len(card.stats)))
        assert max(len(r) for r in rows) - min(len(r) for r in rows) <= 1  # 4 tiles: 4, or 2 + 2, never 3 + 1
        per_row = max(len(r) for r in rows)
        assert per_row == 1 or per_row * facts.STAT_MIN_W + (per_row - 1) * facts.STAT_GAP <= width
    assert card._stat_rows(card.inner.w) == [[0, 1, 2, 3]]  # the full width: one row
    tile = facts.stat_tile("Top speed", "60 km/h", 220, art.SKY)
    assert tile.get_width() == 220 and tile.get_height() >= facts.STAT_H


async def test_profile_with_a_picture_and_stats_fits_one_page() -> None:
    card = await make(sample("profile"), base.RecordingHost(pictures={"giraffe": Pic(pygame.Surface((300, 200)))}))
    assert card.slot.wanted and len(card.pages()) == 1 and len(card.pages()[0]) == 4
    five = await make({**sample("profile"), "facts": ["a fact that is long enough to wrap onto two lines here"] * 5})
    for page in five.pages():  # the stats row takes a line of the budget
        assert sum(len(ls) for _, ls in page) <= base.MAX_LINES - len(five._stat_rows(five.inner.w))
    assert len(five.facts) == facts.MAX_FACTS == 5


async def test_compare_holds_four_columns_and_eight_rows() -> None:
    card = await make(sample("compare", 2))
    assert [c["name"] for c in card.columns] == ["Lion", "Tiger", "Leopard", "Cheetah"]
    x0, col_w, label_w = card._col_geometry()
    assert label_w == facts.LABEL_W_NARROW and col_w >= 150
    assert x0 + 4 * col_w + 3 * facts.COL_GAP <= card.inner.right
    assert card.describe().startswith("The big cats: Lion vs Tiger vs Leopard vs Cheetah: Weight: 190 kg, 220 kg, ")
    five = await make({"layout": "compare", "columns": [{"name": n, "facts": ["1"]} for n in "ABCDE"]})
    assert len(five.columns) == facts.MAX_COLUMNS == 4
    rows = await make({"layout": "compare", "rows": [f"R{i}" for i in range(12)],
                       "columns": [{"name": "A", "facts": [str(i) for i in range(12)]},
                                   {"name": "B", "facts": [str(i) for i in range(12)]}]})
    assert len(rows.rows) == facts.MAX_ROWS and sum(len(p) for p in rows.pages()) == facts.MAX_ROWS
    table, labels = facts._table([], [["", "A", "B", "C", "D", "E"], ["Size", "1", "2", "3", "4", "5"]], "")
    assert [c["name"] for c in table] == ["A", "B", "C", "D"] and labels == ["Size"]


async def test_long_column_names_take_two_lines_in_the_header() -> None:
    card = await make({"layout": "compare", "columns": [{"name": "Breast milk from mum", "facts": ["a"]},
                                                        {"name": "Formula", "facts": ["b"]},
                                                        {"name": "Cow's milk", "facts": ["c"]},
                                                        {"name": "Oat drink", "facts": ["d"]}]})
    assert card._head_lines() == 2 and card._head_h() > facts.HEAD_H
    preview.surface(card, seconds=1)


async def test_a_team_can_say_who_scored() -> None:
    card = await make({"layout": "score", "teams": [{"name": "A", "score": "1", "scorers": "Kane 9'"},
                                                    {"name": "B", "score": "0", "detail": ""}]})
    assert card.teams == [{"name": "A", "score": "1", "detail": "Kane 9'"}, {"name": "B", "score": "0"}]
    assert card.describe() == "A 1 (Kane 9'), B 0"
    preview.surface(card, seconds=1)


async def test_the_note_shows_under_a_list_and_a_comparison() -> None:
    for spec in (sample("list"), sample("compare", 2)):
        card = await make(spec)
        assert card.note and len(card.pages()) == 1  # still room for everything on one page
        preview.surface(card, seconds=1)


async def test_the_fields_tell_the_ai_the_new_limits() -> None:
    fields = base.card_types()["facts"].fields
    assert fields["columns"]["maxItems"] == 4 and fields["items"]["maxItems"] == 12
    assert fields["facts"]["maxItems"] == 5 and fields["stats"]["maxItems"] == 4
    assert "detail" in fields["teams"]["items"]["properties"]
    from littlebot.screens.director import clean_spec

    cleaned, why = clean_spec({**sample("profile"), "stats": ["Height: 5.5 m", {"label": "Weight", "value": "1 t"}]},
                              base.card_types())
    assert cleaned is not None and cleaned["stats"] == ["Height: 5.5 m", {"label": "Weight", "value": "1 t"}], why
    cleaned, why = clean_spec(sample("score"), base.card_types())
    assert cleaned is not None and cleaned["teams"][0]["detail"] == "Saka 23', Havertz 70'", why
