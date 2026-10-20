"""MockProvider: deterministic, warm, age-aware scripted replies (offline)."""

from __future__ import annotations

import pytest

from littlebot.brain.providers import MockProvider, ProviderReply
from littlebot.brain.providers.mock import ANIMAL_SOUNDS, MOCK_MODEL, tier_for_age
from littlebot.safety.content import validate_reply, word_count
from littlebot.safety.policy import AgePolicy

AGES = (None, 10, 20, 30, 48, 58)

PROMPTS = [
    "hello", "hi there", "good morning", "what does the cow say", "I like dogs", "duck!", "the lion",
    "what colour is this? red", "blue", "can we count", "how many fingers", "I like circles", "a triangle",
    "sing a song", "tell me a story", "tell me a story about a cow", "I am happy", "I'm sad", "I'm tired",
    "I am grumpy", "why is the sky blue", "bye bye", "good night", "what's your name", "who are you",
    "are you real", "are you alive", "I love you", "banana", "where is your nose", "thank you", "let's play",
    "how are you", "blorp zing wug", "", "   ", "!!!", "🦄🦄",
]


@pytest.mark.parametrize("age", AGES)
@pytest.mark.parametrize("text", PROMPTS)
def test_every_scripted_reply_is_safe_short_and_unmodified(text, age):
    reply, expression, _topic = MockProvider().respond(text, age)
    assert reply
    result = validate_reply(reply, AgePolicy(age), 30)
    # Our own scripted lines must never trip the safety rules or need trimming.
    assert result.ok and result.text == reply, (reply, result.reasons)
    cap = {"baby": 6, "toddler": 15, "preschool": 25}[tier_for_age(age)]
    assert word_count(reply) <= cap, reply
    from littlebot.core.interfaces import EXPRESSIONS
    assert expression in EXPRESSIONS


@pytest.mark.parametrize(("text", "age", "expected"), [
    ("what does the cow say", 30, "moo"),
    ("what does the cow say", 10, "Moo"),
    ("I like dogs", 48, "woof"),
    ("the duck", 30, "quack"),
    ("a sheep", 30, "baa"),
    ("show me a red ball", 30, "Red"),
    ("yellow", 48, "sun"),
    ("let's count", 30, "One, two, three"),
    ("I see a square", 30, "square"),
    ("sing a song", 30, "Twinkle"),
    ("tell me a story", 48, "Once upon a time"),
    ("I'm sad", 30, "cuddle"),
    ("I am happy", 30, "happy dance"),
    ("why do birds fly", 30, "question"),
    ("bye", 30, "Bye bye"),
    ("good night", 30, "Night night"),
    ("hello", 30, "Hello"),
    ("are you a robot", 30, "robot"),
    ("what is your name", 30, "LittleBot"),
    ("I love you", 30, "hug"),
])
def test_topics(text, age, expected):
    reply, _expr, _topic = MockProvider().respond(text, age)
    assert expected.lower() in reply.lower(), reply


def test_animals_have_sounds():
    m = MockProvider()
    for animal, sound in ANIMAL_SOUNDS.items():
        reply, expr, topic = m.respond(f"what does the {animal} say", 30)
        assert topic == "animal" and sound in reply.lower() and expr == "playful"


def test_story_is_two_or_three_short_sentences_and_uses_favourite_animal():
    m = MockProvider(favourite_animals=lambda: ["Elephants", "rockets"])
    reply, _e, topic = m.respond("tell me a story", 48)
    assert topic == "story"
    assert 2 <= reply.count(".") + reply.count("!") <= 3
    assert "elephant" in reply
    # A story request mentioning an animal uses that animal instead.
    assert "cow" in m.respond("tell me a story about a cow", 48)[0]


def test_unknown_gets_gentle_curious_reply_and_is_deterministic():
    m = MockProvider()
    a = m.respond("blorp zing wug", 30)
    b = m.respond("blorp zing wug", 30)
    assert a == b and a[1] == "curious" and a[2] == "unknown"


def test_replies_grow_with_age():
    m = MockProvider()
    for text in ("what does the cow say", "tell me a story", "hello", "sing a song"):
        baby, toddler, pre = (word_count(m.respond(text, age)[0]) for age in (10, 30, 50))
        assert baby <= toddler <= pre and baby < pre


def test_unknown_age_is_treated_as_baby():
    m = MockProvider()
    assert m.respond("tell me a story", None) == m.respond("tell me a story", 12)


async def test_reply_interface_uses_age_callable_and_needs_no_network():
    m = MockProvider(age_months=lambda: 30, robot_name="Robo")
    r = await m.reply([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"},
                       {"role": "user", "content": "what is your name"}], system="ignored", max_tokens=50, timeout=1)
    assert isinstance(r, ProviderReply)
    assert "Robo" in r.text and r.model == MOCK_MODEL and r.stop_reason == "scripted"
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert m.configured() and not m.is_cloud


async def test_reply_survives_broken_profile_callables():
    def boom():
        raise RuntimeError("profile broken")

    m = MockProvider(age_months=boom, favourite_animals=boom)
    r = await m.reply([{"role": "user", "content": "tell me a story"}], system="", max_tokens=50, timeout=1)
    assert "duck" in r.text
