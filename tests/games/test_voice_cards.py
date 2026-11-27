"""The voice games' cards (littlebot/screens/cards/quiz.py, word_game.py, game_board.py): internal, drawn without
errors in every state over the whole 1024x600 canvas, laid out for the 7" screen (big tap targets, readable sizes,
the presenter's box free, long text fitted), following the live game (flashes, the next question, the end), taps
answering."""

from __future__ import annotations

from typing import Any

import pygame
import pytest

from games_helpers import distinct_colors
from littlebot.games.voice.riddles import Riddles
from littlebot.games.voice.trivia import Trivia
from littlebot.games.voice.twenty_questions import TwentyQuestions
from littlebot.games.voice.word_game import Hangman
from littlebot.games.voice.would_you_rather import WouldYouRather
from littlebot.screens import base
from littlebot.screens.base import RecordingHost
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import game_board, quiz, word_game
from voice_game_helpers import right_letter, rng, store, wrong_letter

KINDS = ("quiz", "word_game", "game_board")


SENTINEL = (255, 0, 255)


@pytest.fixture
def surf():
    return pygame.Surface((base.W, base.H))


def unpainted(surface: pygame.Surface) -> int:
    """How many pixels still have the colour the surface was filled with before the card drew."""
    return pygame.mask.from_threshold(surface, SENTINEL, (1, 1, 1, 255)).count()


def presenter() -> pygame.Rect:
    return pygame.Rect(base.PRESENTER_BOX)


def on_canvas(r: pygame.Rect) -> bool:
    return pygame.Rect(0, 0, base.W, base.H).contains(r)


class Screens:
    """Just enough of ctx.screens for build_for: the card showing now."""

    def __init__(self) -> None:
        self.current: Any = None


def test_the_cards_are_registered_as_internal():
    types = base.card_types()
    offered = base.card_types(offered_only=True)
    for kind in KINDS:
        assert kind in types and not types[kind].offered and kind not in offered


@pytest.mark.parametrize("module", [quiz, word_game, game_board])
async def test_every_sample_draws_over_the_whole_canvas(module, ctx, surf):
    for spec in module.SAMPLES:
        card = await base.build(dict(spec), ctx, RecordingHost())
        assert card is not None and card.game is None
        for dt in (0.0, 0.2, 1.0):
            surf.fill(SENTINEL)
            card.update(dt)
            card.draw(surf)
            assert unpainted(surf) == 0  # the night sky covers all 1024x600, not the old 720x480 corner
        assert distinct_colors(surf) > 8
        assert card.describe() and card.status()["kind"] == spec["card"]


def test_the_layout_fills_the_7_inch_screen():
    area = gk.AREA
    assert area.w >= base.W - 40 and area.bottom >= base.H - 20  # the game area spans the canvas
    assert not area.colliderect(presenter()) and not gk.HEADER.colliderect(presenter())
    assert gk.HEADER.x >= presenter().right and gk.HEADER.right <= base.W
    # quiz: the question over the width, four big answer tiles filling the rest
    tiles = [quiz.tile_rect(i) for i in range(4)]
    assert quiz.Q_RECT.w == area.w and on_canvas(quiz.Q_RECT)
    for i, r in enumerate(tiles):
        assert on_canvas(r) and area.contains(r) and not r.colliderect(presenter())
        assert r.w >= 400 and r.h >= 100  # easy taps (at least ~90 px) with room for two lines of an answer
        assert not any(r.colliderect(o) for o in tiles[i + 1:]) and not r.colliderect(quiz.Q_RECT)
    assert max(r.bottom for r in tiles) >= area.bottom - 4 and max(r.right for r in tiles) >= area.right - 4
    # game board: two tall choices side by side, the status line under them
    left, right = game_board.option_rects(2)
    for r in (left, right):
        assert area.contains(r) and r.w >= 380 and r.h >= 300
    assert right.left - left.right >= 2 * game_board.OR_R and area.contains(game_board.STATUS_RECT)
    assert game_board.BODY_RECT.bottom < game_board.STATUS_RECT.top
    # hangman: the word, then the whole alphabet under it
    keys = [word_game.key_rect(i) for i in range(26)]
    assert word_game.WORD_RECT.w == area.w and area.contains(word_game.TRIED_RECT)
    assert all(word_game.TRIED_RECT.contains(k) for k in keys) and len({k.topleft for k in keys}) == 26
    assert min(k.w for k in keys) >= 60 and min(k.h for k in keys) >= 60


def test_text_is_readable_on_the_7_inch_screen():
    sizes = [*quiz.Q_SIZES, *quiz.OPT_SIZES, *game_board.TEXT_SIZES, *game_board.OPTION_SIZES,
             *game_board.ANSWER_SIZES, *game_board.STATUS_SIZES, *game_board.END_SIZES, word_game.KEY_PX]
    assert min(sizes) >= base.BODY_PX
    assert max(quiz.Q_SIZES) >= base.TITLE_PX and max(game_board.TEXT_SIZES) >= 2 * base.BODY_PX
    assert gk.CHIP_PX >= base.SMALL_PX and gk.BIG_PX >= 2 * base.TITLE_PX
    assert max(quiz.Q_LINES, quiz.OPT_LINES, game_board.TEXT_LINES, game_board.OPTION_LINES) <= base.MAX_LINES


@pytest.mark.parametrize("text, sizes, rect, lines", [
    ("Which famous scientist, born in 1879 in the German city of Ulm, developed the theory of relativity and won "
     "the Nobel Prize in Physics in 1921 for the photoelectric effect? " * 2, quiz.Q_SIZES,
     quiz.Q_RECT.inflate(-56, -60), quiz.Q_LINES),
    ("The International Space Station orbiting Earth every 90 min", quiz.OPT_SIZES,
     quiz.text_area(quiz.tile_rect(0)), quiz.OPT_LINES),
    ("A one-storey house is all yellow: yellow walls, yellow doors, yellow furniture. What colour are its stairs?",
     game_board.TEXT_SIZES, game_board.BODY_RECT.inflate(-64, -48), game_board.TEXT_LINES),
    ("Have a paintbrush whose paintings come to life", game_board.OPTION_SIZES,
     game_board.option_rects(2)[0].inflate(-48, -120), game_board.OPTION_LINES),
])
def test_long_text_is_fitted_never_tiny(text, sizes, rect, lines):
    size, out = gk.fitted(text, sizes, rect.w, lines, rect.h, balance=True)
    assert size >= base.BODY_PX and 1 <= len(out) <= lines
    assert gk.block_h(size, len(out)) <= rect.h or size == sizes[-1]
    assert all(kit.text_w(line, size) <= rect.w for line in out)
    assert gk.fitted("What gets wetter the more it dries?", game_board.TEXT_SIZES, 928, 5, 300)[0] >= 80  # big


def test_a_round_end_and_the_letters_tried(surf):
    card = word_game.WordGameCard(dict(word_game.SAMPLES[0]))
    card.update(0.1)
    surf.fill(SENTINEL)
    card.draw(surf)
    assert unpainted(surf) == 0
    keys = [word_game.key_rect(ord(c) - ord("A")) for c in "ESB"]  # E in the word, S not, B not tried
    hit, miss, fresh = (surf.get_at((k.x + 8, k.centery))[:3] for k in keys)
    assert len({hit, miss, fresh}) == 3  # right letters, wrong ones and untried ones look different
    over = word_game.WordGameCard({**word_game.SAMPLES[0], "over": True, "score": 3, "total": 5})
    over.update(1.0)
    surf.fill(SENTINEL)
    over.draw(surf)
    assert unpainted(surf) == 0 and over.describe() == "Hangman over: 3 of 5 words solved"


async def _bound(ctx, game, cls):
    host = RecordingHost()
    card = await gk.build_for(cls, dict(game.view), ctx, host)
    assert card.game is game  # found through littlebot.games.voice.base.find (no skills hub here)
    return card, host


async def test_the_quiz_card_follows_a_round(ctx, surf):
    g = Trivia(store=store(ctx), rng=rng(), questions=3)
    await g.start()
    card, host = await _bound(ctx, g, quiz.QuizCard)
    card.update(0.1)
    card.draw(surf)
    assert card.mood == "curious" and card.view["number"] == 1
    first = g.current.text
    g.handle(wrong_letter(g))
    card.update(0.1)
    assert card.flashing and card.flash["question"] == first and card.mood == "surprised" and card.animating
    card.draw(surf)
    card.update(gk.REVEAL_S + 0.1)
    assert not card.flashing and card.view["number"] == 2
    g.handle("hint")
    card.update(0.1)
    card.draw(surf)
    assert len(card.view["removed"]) == 2
    removed = card.view["removed"][0]
    assert not card.tap(*quiz.tile_rect(removed).center)  # a dimmed tile can't be picked
    taps: list[int] = []
    g.on_tap = taps.append
    assert card.tap(*quiz.tile_rect(g.current.answer).center) and taps == [g.current.answer]
    assert host.sounds == ["click"]
    assert not card.tap(*quiz.Q_RECT.center) and not card.tap(base.W - 4, 4)  # not on a tile
    g.handle(right_letter(g))
    g.handle(right_letter(g))
    card.update(0.1)
    assert card.flashing and card.mood == "happy"
    card.update(gk.REVEAL_S + 0.1)
    card.draw(surf)
    assert card.view["over"] and card.describe().startswith("Trivia over: 2 of 3 right")
    assert not card.done
    card.update(gk.OVER_LINGER_S + 1)
    assert card.done


async def test_build_keeps_the_card_already_showing(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=3)
    await g.start()
    screens = Screens()
    ctx.screens = screens
    card, _ = await _bound(ctx, g, quiz.QuizCard)
    screens.current = card
    again = await gk.build_for(quiz.QuizCard, dict(g.view), ctx, RecordingHost())
    assert again is card
    other = Trivia(store=store(ctx), rng=rng(), questions=3)
    await other.start()
    new = await gk.build_for(quiz.QuizCard, dict(other.view), ctx, RecordingHost())
    assert new is not card and new.game is other
    still = await gk.build_for(quiz.QuizCard, {"card": "quiz", "question": "?", "options": ["a", "b", "c", "d"]},
                               ctx, RecordingHost())
    assert still.game is None and not still.tap(*quiz.tile_rect(0).center)  # no game: nothing to answer


async def test_the_word_card(ctx, surf):
    g = Hangman(store=store(ctx), rng=rng(), words=2)
    await g.start()
    g.word, g.category = "ELEPHANT", "animals"
    g.changed()
    card, _ = await _bound(ctx, g, word_game.WordGameCard)
    card.update(0.1)
    g.handle("e")
    card.update(0.1)
    assert card.pop_letter == "E" and card.mood == "happy" and card.animating
    card.draw(surf)
    g.handle("z")
    card.update(0.1)
    assert card.mood == "surprised" and card.view["lives"] == 5
    card.draw(surf)
    g.handle("elephant")
    card.update(0.1)
    assert card.flashing and card.flash["word"] == "ELEPHANT" and card.flash["won"]
    card.draw(surf)
    card.update(gk.REVEAL_S + 0.1)
    card.draw(surf)
    assert card.view["number"] == 2 and "Hangman (" in card.describe()
    g.handle("stop the game")
    card.update(0.1)
    card.draw(surf)
    assert card.view["over"]


async def test_the_board_for_riddles(ctx, surf):
    g = Riddles(store=store(ctx), rng=rng(), riddles=2)
    await g.start()
    card, _ = await _bound(ctx, g, game_board.GameBoardCard)
    card.update(0.1)
    card.draw(surf)
    assert card.view["text"] == g.current["q"] and not card.tap(*game_board.BODY_RECT.center)  # nothing to tap
    g.handle("I give up")
    card.update(0.1)
    assert card.flashing and card.flash["right"] is False
    card.draw(surf)
    g.handle(g.current["accept"][0])
    card.update(0.1)
    assert card.flash["right"] is True and card.mood == "happy"
    card.draw(surf)


async def test_the_board_for_would_you_rather(ctx, surf):
    g = WouldYouRather(store=store(ctx), rng=rng(), questions=2)
    await g.start()
    card, _ = await _bound(ctx, g, game_board.GameBoardCard)
    taps: list[int] = []
    g.on_tap = taps.append
    left, right = game_board.option_rects(2)
    assert card.tap(*right.center) and taps == [1]
    card.draw(surf)
    g.tap(1)
    card.update(0.1)
    assert card.flashing and card.flash["picked"] == 1 and "options" in card.flash
    card.draw(surf)
    card.update(gk.REVEAL_S + 0.1)
    card.draw(surf)
    assert card.key("left") and taps == [1, 0]


async def test_the_board_for_twenty_questions(ctx, surf):
    g = TwentyQuestions(store=store(ctx), rng=rng())
    await g.start()
    card, _ = await _bound(ctx, g, game_board.GameBoardCard)
    g.handle("does it have legs")
    card.update(0.1)
    assert card.view["counter"] == [1, 20]
    card.draw(surf)
    g.handle("I give up")
    card.update(0.1)
    card.draw(surf)
    assert card.view["over"] and card.view["text"].startswith("It was ")
