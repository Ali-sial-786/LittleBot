"""The voice games' cards (littlebot/screens/cards/quiz.py, word_game.py, game_board.py): internal, drawn without
errors in every state, following the live game (flashes, the next question, the end), taps answering."""

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
from littlebot.screens.cards import game_board, quiz, word_game
from voice_game_helpers import right_letter, rng, store, wrong_letter

KINDS = ("quiz", "word_game", "game_board")


@pytest.fixture
def surf():
    return pygame.Surface((720, 480))


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
async def test_every_sample_draws(module, ctx, surf):
    for spec in module.SAMPLES:
        card = await base.build(dict(spec), ctx, RecordingHost())
        assert card is not None and card.game is None
        for dt in (0.0, 0.2, 1.0):
            card.update(dt)
            card.draw(surf)
        assert distinct_colors(surf) > 8
        assert card.describe() and card.status()["kind"] == spec["card"]


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
    assert not card.tap(5, 470)  # not on a tile
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
    assert card.view["text"] == g.current["q"] and not card.tap(360, 240)  # no options to tap
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
