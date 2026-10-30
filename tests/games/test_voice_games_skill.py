"""The VoiceGames skill (littlebot/games/voice/skill.py): the tools (validated, never raising, exact words to say),
the fast path (handle_utterance: moves, starts, play again), the brain's notes, the model's gating, the hub, and
the card on the screen with taps that answer and are announced."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import SecretStr

from littlebot.brain import text_model
from littlebot.games.voice.base import Reply
from littlebot.games.voice.skill import GAMES, VoiceGames, game_id, parse_start
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context
from voice_game_helpers import FakeModel, right_letter, rng, wrong_letter


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sk(ctx, clock):
    s = VoiceGames(ctx, now=clock, rng=rng(), model=False)
    yield s


def said(result: str) -> str:
    assert result.startswith("ok: say exactly: "), result
    return result[len("ok: say exactly: "):]


# ---------------------------------------------------------------- recognising a start
@pytest.mark.parametrize("text, want", [
    ("let's play trivia", ("trivia", "", "")), ("play hard science trivia", ("trivia", "science", "hard")),
    ("start a quiz about dinosaurs", ("trivia", "dinosaurs", "")), ("quiz me on history", ("trivia", "history", "")),
    ("Quiz me", ("trivia", "", "")), ("give me a riddle", ("riddles", "", "")), ("trivia time", ("trivia", "", "")),
    ("how about twenty questions", ("twenty_questions", "", "")), ("hangman", ("hangman", "", "")),
    ("let's play hangman with animals", ("hangman", "animals", "")),
    ("can we play would you rather please", ("would_you_rather", "", "")),
    ("let's play 20 questions", ("twenty_questions", "", "")), ("I want to play an easy quiz", ("trivia", "", "easy")),
])
def test_starts_are_recognised(text, want):
    assert parse_start(text) == want


@pytest.mark.parametrize("text", ["play baby shark", "do you like trivia", "play a trivia song", "play a game",
                                  "have you ever played hangman", "what's the weather", "", None, "x" * 500,
                                  "play the riddle video on youtube"])
def test_not_a_start(text):
    assert parse_start(text) is None


def test_game_names():
    assert game_id("Trivia") == "trivia" and game_id("20 questions") == "twenty_questions"
    assert game_id("would_you_rather") == "would_you_rather" and game_id("word game") == "hangman"
    assert game_id("chess") is None and game_id(None) is None and game_id({"x": 1}) is None


# ---------------------------------------------------------------- tools
def test_the_tools_are_well_formed(sk):
    specs = sk.tools()
    names = [t["name"] for t in specs]
    assert names == ["start_voice_game", "game_answer", "game_hint", "game_skip", "stop_voice_game", "game_scores"]
    for t in specs:
        p = t["parameters"]
        assert p["type"] == "object" and p["additionalProperties"] is False and t["description"]
        assert set(p.get("required", [])) <= set(p["properties"])
    start = specs[0]["parameters"]["properties"]
    assert start["game"]["enum"] == list(GAMES) and start["difficulty"]["enum"] == ["easy", "medium", "hard"]
    json.dumps(specs)


async def test_a_game_by_tools(sk):
    out = await sk.run("start_voice_game", {"game": "trivia", "topic": "science", "difficulty": "easy"})
    words = said(out)
    assert words.startswith("Trivia time! 10 science questions, easy. Say A, B, C or D. Question 1:")
    g = sk.active
    assert g is not None and g.id == "trivia" and g.category == "science" and g.level == "easy"
    assert sk.context_note() == g.describe()
    assert said(await sk.run("game_answer", {"answer": right_letter(g)})).split()[0] in (
        "Yes!", "Correct!", "That's", "You", "Nailed", "Spot")
    out = await sk.run("game_answer", {"answer": "what a lovely day"})
    assert out.startswith("error: that isn't a move in Trivia: ask them to answer A, B, C or D")
    assert said(await sk.run("game_hint", {})).startswith("Fifty-fifty")
    assert said(await sk.run("game_skip", {})).startswith("Skipping!")
    assert said(await sk.run("stop_voice_game", {})).startswith("Okay, we'll stop there. You got 1 of 2 right")
    assert sk.active is None
    assert (await sk.run("game_scores", {"game": "trivia"})).startswith("ok: Your best trivia: 10 points")


@pytest.mark.parametrize("name, args", [
    ("start_voice_game", {"game": "chess"}), ("start_voice_game", {}), ("start_voice_game", {"game": None}),
    ("start_voice_game", {"game": ["trivia"]}), ("game_answer", {"answer": "B"}), ("game_hint", {}),
    ("game_skip", {}), ("stop_voice_game", {}), ("game_scores", {"game": "chess"}), ("no_such_tool", {}),
])
async def test_bad_calls_are_errors_not_crashes(sk, name, args):
    out = await sk.run(name, args)
    assert out.startswith("error: ")


async def test_odd_arguments_are_cleaned(sk):
    out = await sk.run("start_voice_game", {"game": "TRIVIA", "topic": {"x": 1}, "difficulty": "tough"})
    assert out.startswith("ok: say exactly: Trivia time!")
    assert sk.active.level == "hard" and sk.active.category == "mixed"
    assert (await sk.run("game_answer", {"answer": ""})).startswith("error: ")
    assert (await sk.run("game_answer", {"answer": {"a": 1}})).startswith("error: ")
    assert (await sk.run("game_answer", {})).startswith("error: ")
    assert (await sk.run("start_voice_game", "not a dict")).startswith("error: ")  # type: ignore[arg-type]


async def test_twenty_questions_by_tools(sk):
    said(await sk.run("start_voice_game", {"game": "twenty_questions"}))
    g = sk.active
    secret = g.secret["name"]
    out = await sk.run("game_answer", {"answer": "is it alive?"})
    assert out.startswith("ok: question 1 of 20 counted. You are thinking of " + secret)
    assert said(await sk.run("game_hint", {})).startswith("Here's a hint:")
    out = await sk.run("game_answer", {"answer": f"is it {secret}?"})
    assert said(out).startswith(f"Yes! It's {secret}!") and sk.active is None


# ---------------------------------------------------------------- the fast path
async def test_moves_go_to_the_game_first(sk):
    assert await sk.handle_utterance("hello there") is None  # no game: nothing to do
    r = await sk.handle_utterance("let's play trivia")
    assert isinstance(r, Reply) and r.say.startswith("Trivia time!") and r.card["card"] == "quiz" and r.sound
    g = sk.active
    r = await sk.handle_utterance(right_letter(g))
    assert r is not None and g.score == 10
    assert await sk.handle_utterance("what a hard question") is None  # for the brain
    r = await sk.handle_utterance("let's play hangman instead")
    assert r.say.startswith("Hangman!") and sk.active.id == "hangman" and g.over


async def test_play_again_and_yes_to_another_round(ctx, clock):
    sk = VoiceGames(ctx, now=clock, rng=rng(), model=False)
    ctx.config.voice_games.trivia_questions = 3
    await sk.handle_utterance("play science trivia")
    g = sk.active
    for _ in range(3):
        r = await sk.handle_utterance(wrong_letter(g))
    assert r.done and "Want another round?" in r.say and sk.active is None
    assert sk.context_note().startswith("Voice game: Trivia just ended")
    r = await sk.handle_utterance("yes please")
    assert r is not None and r.say.startswith("Trivia time! 3 science questions") and sk.active is not g
    g2 = sk.active
    await sk.handle_utterance("stop the game")
    assert g2.over and await sk.handle_utterance("yes") is None  # no offer after a stop
    clock.t += 30
    r = await sk.handle_utterance("play again")
    assert r is not None and r.say.startswith("Trivia time!")
    await sk.handle_utterance("stop the game")
    clock.t += 1000
    assert await sk.handle_utterance("play again") is None  # too long ago
    assert sk.context_note() == ""


async def test_only_the_next_words_answer_the_offer(sk, ctx):
    ctx.config.voice_games.trivia_questions = 3
    await sk.handle_utterance("let's play trivia")
    g = sk.active
    while not g.over:
        await sk.handle_utterance(right_letter(g))
    assert await sk.handle_utterance("what's the weather") is None
    assert await sk.handle_utterance("yes") is None


async def test_switched_off(ctx):
    ctx.config.voice_games.enabled = False
    sk = VoiceGames(ctx, model=False)
    assert not sk.available()
    assert await sk.handle_utterance("let's play trivia") is None


def test_the_notes(sk):
    note = sk.prompt_note()
    assert "start_voice_game" in note and "game_answer" in note and "let's play trivia" in note
    assert sk.context_note() == ""
    st = sk.status()
    assert st["name"] == "voice_games" and st["active"] is None and len(st["games"]) == 5


async def test_lifecycle(sk):
    assert await sk.start() is None  # the hub's start
    r = await sk.start("riddles")  # the assistant's start
    assert isinstance(r, Reply) and r.say.startswith("Riddle time!")
    g = sk.active
    await sk.stop()
    assert g.over and sk.active is None


# ---------------------------------------------------------------- the text model
def _claude_ctx(**kw: Any):
    ctx = make_test_context(claude={"model": "claude-haiku-5-5"}, **kw)
    ctx.config.anthropic_api_key = SecretStr("sk-test")
    ctx.privacy.set(cloud_enabled=True)
    return ctx


class _FakeClient:
    """An Anthropic client whose messages.create answers with model questions as JSON."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        from voice_game_helpers import model_questions  # noqa: PLC0415

        self.calls.append(kw)

        class Msg:
            stop_reason = "end_turn"
            usage = None
            content = [{"type": "text", "text": json.dumps({"questions": model_questions(10)})}]

        return Msg()


async def test_trivia_asks_claude_only_when_allowed(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(text_model, "client_factory", lambda: client)
    ctx = _claude_ctx()
    try:
        sk = VoiceGames(ctx, rng=rng())
        model = sk._model()
        assert model is not None and model.purpose == "games"
        r = await sk.start_game("trivia", topic="dinosaurs")
        assert "dinosaur number" in r.say and len(client.calls) == 1
        req = client.calls[0]
        assert req["output_config"]["format"]["type"] == "json_schema" and req["thinking"] == {"type": "disabled"}
        ctx.privacy.set(cloud_enabled=False)
        assert sk._model() is None
        ctx.privacy.set(cloud_enabled=True)
        ctx.config.voice_games.use_model_questions = False
        assert sk._model() is None
    finally:
        ctx.db.close()


async def test_a_start_cancelled_while_waiting_for_the_model_leaves_no_game(ctx):
    model = FakeModel(delay=5.0)
    sk = VoiceGames(ctx, rng=rng(), model=model)
    task = asyncio.ensure_future(sk.handle_utterance("quiz me on dinosaurs"))
    await asyncio.sleep(0.05)
    assert len(model.calls) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sk.active is None
    assert (await sk.handle_utterance("let's play riddles")).say.startswith("Riddle time!")


def test_no_key_means_the_bank(ctx):
    sk = VoiceGames(ctx)
    assert sk._model() is None  # tests have no Claude model or key


async def test_an_injected_model_is_used(ctx):
    model = FakeModel()
    sk = VoiceGames(ctx, rng=rng(), model=model)
    await sk.start_game("trivia", topic="history")
    assert len(model.calls) == 1


# ---------------------------------------------------------------- the hub
async def test_the_hub_offers_the_skill(ctx):
    hub = SkillHub(ctx)
    ctx.skills = hub
    sk = hub.get("voice_games")
    assert isinstance(sk, VoiceGames)
    sk._model_override = False
    assert "start_voice_game" in [s["name"] for s in hub.specs()] and hub.owns("game_answer")
    await hub.start()
    out = await hub.run("start_voice_game", '{"game": "riddles"}')
    assert out.startswith("ok: say exactly: Riddle time!")
    assert "Voice games:" in hub.prompt_notes() and "Voice game on: Riddles" in hub.context_notes()
    await hub.stop()
    assert sk.active is None


# ---------------------------------------------------------------- the screen
@pytest.fixture
def screens(ctx):
    from littlebot.screens.service import ScreenService

    s = ScreenService(ctx)
    ctx.screens = s
    yield s
    s.detach()


async def test_the_card_follows_the_game_and_a_tap_answers(ctx, screens):
    from littlebot.screens.cards.quiz import QuizCard, tile_rect

    sk = VoiceGames(ctx, rng=rng(), model=False)
    ctx.skills = type("Hub", (), {"get": lambda self, name: sk if name == "voice_games" else None})()
    announced: list[Any] = []
    ctx.bus.subscribe(ANNOUNCE, announced.append)
    await sk.handle_utterance("let's play trivia")
    g = sk.active
    card = screens.current
    assert isinstance(card, QuizCard) and card.game is g and card.session == g.session
    await sk.handle_utterance(right_letter(g))
    assert screens.current is card  # the same card, not a new one
    card.update(0.1)
    assert card.view["number"] == 2 and card.flashing
    card.update(5.0)
    assert not card.flashing
    target = tile_rect(g.current.answer).center
    assert card.tap(*target)
    assert card.pressed == g.current.answer
    for _ in range(5):
        await asyncio.sleep(0)
    assert g.score == 20 and len(announced) == 1
    ev = announced[0].data
    assert ev["kind"] == "game" and ev["card"]["card"] == "quiz" and ev["text"].split()[0] in (
        "Yes!", "Correct!", "That's", "You", "Nailed", "Spot")
    assert ctx.audio.sounds[-1] == "success"  # the skill plays the sound when it speaks for itself
    # showing the reply's card again (as the assistant may) keeps the same card
    await screens.show(dict(g.view))
    assert screens.current is card


async def test_a_card_closed_by_hand_comes_back_on_the_next_move(ctx, screens):
    sk = VoiceGames(ctx, rng=rng(), model=False)
    await sk.handle_utterance("let's play hangman")
    first = screens.current
    screens.close("tap")
    assert screens.current is None
    await sk.handle_utterance("e")
    assert screens.current is not None and screens.current is not first and screens.current.kind == "word_game"
