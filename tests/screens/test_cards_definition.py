"""The "definition" card: meanings, spelling, translations, paging and readability."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from littlebot.games import art
from littlebot.screens import base, preview
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import definition


class Pic:
    def __init__(self, surface: Any = None, ready: bool = True, credit: str = "Wikipedia") -> None:
        self.ready, self.surface, self.credit = ready, surface, credit


async def make(spec: dict[str, Any], host: base.RecordingHost | None = None) -> definition.DefinitionCard:
    card = await base.build({"card": "definition", **spec}, None, host or base.RecordingHost())
    assert isinstance(card, definition.DefinitionCard)
    return card


def advance(card: base.Card, seconds: float, step: float = 0.1) -> None:
    for _ in range(round(seconds / step)):
        card.update(step)


MEANING = {"word": "encyclopedia", "say_it": "en-SY-kluh-PEE-dee-uh", "part": "noun",
           "meanings": ["A book or set of books full of facts about many things"],
           "example": "We looked up volcanoes in the encyclopedia.", "picture_query": "encyclopedia"}


def test_registered_with_modes_fields_and_actions() -> None:
    ct = base.card_types()["definition"]
    assert ct.required == ("word",)
    assert set(ct.fields["mode"]["enum"]) == {"meaning", "spell", "translate"}
    assert {"word", "say_it", "part", "meanings", "example", "translation", "language", "picture_query"} <= set(
        ct.fields)
    assert "IPA" in ct.fields["say_it"]["description"]
    assert {"next", "prev", "again"} <= set(ct.actions)


async def test_every_sample_builds_and_draws() -> None:
    for spec in definition.SAMPLES:
        for picture in (Pic(pygame.Surface((330, 220))), Pic(None, ready=False), Pic(None)):
            host = base.RecordingHost(pictures={spec.get("picture_query", ""): picture})
            card = await base.build(spec, None, host)
            assert card is not None, spec
            for seconds in (0.0, 1.0, 12.0):
                assert preview.surface(card, seconds=seconds).get_size() == (base.W, base.H)


async def test_meaning_describe_and_picture() -> None:
    host = base.RecordingHost()
    card = await make(MEANING, host)
    assert host.asked_pictures == [("encyclopedia", "")]
    assert card.describe() == ("encyclopedia, noun (say en-SY-kluh-PEE-dee-uh): A book or set of books full of facts "
                               "about many things")
    assert card.status()["mode"] == "meaning" and card.title == "encyclopedia"


async def test_cards_that_cant_be_made() -> None:
    host = base.RecordingHost()
    assert await base.build({"card": "definition", "word": ""}, None, host) is None
    assert await base.build({"card": "definition", "word": "x"}, None, host) is None  # no meaning, no say-it
    assert await base.build({"card": "definition", "mode": "translate", "word": "apple"}, None, host) is None
    card = await base.build({"card": "definition", "word": "gif", "say_it": "GIF"}, None, host)
    assert card is not None  # "how do you say gif": just the word and how to say it
    odd = await make({"mode": "sing", "word": "la", "meanings": ["a note"]})
    assert odd.mode == "meaning"


def test_say_it_marks_the_stressed_syllables() -> None:
    parts = definition._say_parts("en-SY-kluh-PEE-dee-uh")
    assert [p for p, stressed in parts if stressed] == ["SY", "PEE"]
    assert [p for p, _ in parts if p not in "-"] == ["en", "SY", "kluh", "PEE", "dee", "uh"]
    assert not any(s for _, s in definition._say_parts("GIF"))  # one syllable: nothing to point out
    assert definition.say_it_sprite("rih-ZIL-yuhnt").get_height() > 20


async def test_spelling_pops_letters_in_one_by_one() -> None:
    host = base.RecordingHost()
    card = await make({"mode": "spell", "word": "cat"}, host)
    assert host.asked_pictures == []  # no picture for spelling
    assert card.letters_shown() == 0 and card.describe() == "Spelling cat: starting so far"
    advance(card, definition.LETTER_FIRST + 0.2)
    assert card.letters_shown() == 1 and card.describe() == "Spelling cat: C so far"
    advance(card, definition.LETTER_S)
    assert card.describe() == "Spelling cat: C-A so far"
    advance(card, 2.0)
    assert card.describe() == "Spelling cat: C-A-T"
    assert host.sounds == ["sparkle"]
    assert card.command("again", {}) and card.letters_shown() == 0
    advance(card, definition.LETTER_FIRST + 0.2)
    assert card.letters_shown() == 1
    assert card.command("next", {}) and card.letters_shown() == 3  # show them all now
    assert card.tap(10, 300) and card.letters_shown() == 0  # a tap spells it again


async def test_long_spelling_fits_the_panel() -> None:
    word = "supercalifragilisticexpialidocious"  # 34 letters
    card = await make({"mode": "spell", "word": word})
    assert "".join(card.letters) == word
    tile, rows = card.tile_layout()
    assert tile < definition.TILE_SIZES[0]  # smaller tiles so the whole word fits
    per_row = (kit.PANEL.w - 2 * kit.PAD + definition.TILE_GAP) // (tile + definition.TILE_GAP)
    assert all(len(r) <= per_row for r in rows)
    assert "".join(rows) == word
    assert len(rows) * (tile + 18) + 100 <= kit.PANEL.h - 2 * kit.PAD
    short = await make({"mode": "spell", "word": "cat"})
    assert short.tile_layout() == (definition.TILE_SIZES[0], ["cat"])
    capped = await make({"mode": "spell", "word": "x" * 60})
    assert len(capped.letters) == definition.MAX_LETTERS
    assert sum(len(r) for r in capped.tile_layout()[1]) == definition.MAX_LETTERS
    two = await make({"mode": "spell", "word": "ice cream"})
    assert two._tile_rows(["ice", "cream"]) == ["ice cream"] and two.letters == list("icecream")
    preview.surface(card, seconds=30)


def test_a_long_respelling_wraps_at_hyphens() -> None:
    say = "an-tee-dis-uh-STAB-lish-muhn-TAIR-ee-uh-niz-uhm-and-some-more"
    img = definition.fit_say_it(say, 400)
    assert img.get_width() <= 400 + 40  # wrapped (a token may finish just past the edge)
    assert img.get_height() > definition.art.font(base.BODY_PX).get_linesize() * 1.5
    one = definition.fit_say_it("juh-RAF", 400)
    assert one.get_height() == definition.art.font(definition.SAY_PX).get_height()


async def test_translation() -> None:
    card = await make({"mode": "translate", "word": "apple", "translation": "manzana", "language": "Spanish",
                       "say_it": "mahn-SAH-nah", "example": "Me gusta la manzana."})
    assert card.describe() == "apple in Spanish: manzana (say mahn-SAH-nah)"
    assert [kind for kind, _ in card.pages()[0]] == ["example"]
    preview.surface(card, seconds=2)


async def test_long_meanings_are_paged_and_turn_on_their_own() -> None:
    long = "a meaning that goes on and on with many plain words so that it needs several lines on the screen"
    card = await make({"word": "long", "say_it": "LONG", "meanings": [long, long + " again"],
                       "example": "This example sentence is also fairly long, just to be sure."})
    pages = card.pages()
    assert len(pages) >= 2
    for page in pages:
        assert sum(len(lines) for _, lines in page) <= base.MAX_LINES
        for kind, lines in page:
            size = definition.MEANING_PX if kind == "meaning" else definition.EXAMPLE_PX
            assert all(kit.text_w(s, size) <= card._text_width(card.slot.wanted) for s in lines)
    assert card.page == 0
    advance(card, definition.PAGE_S + 0.2)
    assert card.page == 1 and card.status()["page"] == 2
    assert card.command("prev", {}) and card.page == 0
    advance(card, definition.PAGE_S - 1)
    assert card.page == 0  # a manual turn holds the page a while
    assert card.command("next", {}) and card.page == 1
    assert card.command("again", {}) and card.page == 0
    assert not card.command("jump", {})
    assert card.key("right") and card.page == 1


async def test_failed_picture_gives_the_text_the_whole_width() -> None:
    pic = Pic(None, ready=False)
    card = await make(MEANING, base.RecordingHost(pictures={"encyclopedia": pic}))
    narrow = card._text_width(card.slot.wanted)
    pic.ready = True
    card.update(0.1)
    assert not card.slot.wanted and card._text_width(card.slot.wanted) > narrow
    preview.surface(card, seconds=1)


async def test_presenter_box_stays_free_and_text_sizes_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in definition.SAMPLES:
        sizes: list[int] = []
        real = art.font
        monkeypatch.setattr(art, "font", lambda size, real=real, sizes=sizes: (sizes.append(size), real(size))[1])
        art.clear_caches()
        kit.wrap.cache_clear()
        host = base.RecordingHost(pictures={spec.get("picture_query", ""): Pic(pygame.Surface((330, 220)))})
        card = await base.build(spec, None, host)
        advance(card, 15)
        surf = pygame.Surface((base.W, base.H))
        card.draw(surf)
        box = pygame.Rect(*base.PRESENTER_BOX)
        bg = art.background((base.W, base.H), seed=31)
        assert pygame.image.tobytes(surf.subsurface(box), "RGB") == pygame.image.tobytes(bg.subsurface(box), "RGB")
        assert sizes and min(sizes) >= base.SMALL_PX
        monkeypatch.setattr(art, "font", real)
