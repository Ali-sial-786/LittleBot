"""Deterministic, offline, rule-based conversation provider.

Used whenever cloud AI is not allowed (infants and unknown ages always, offline mode,
parent has not opted in, no key, budget reached) and as the safe default. Replies are
warm, playful and short, and get a little longer as the child grows:

* ``baby``      - under 24 months or age unknown: 1-6 words, sounds and repetition
* ``toddler``   - 24-36 months: one short sentence plus an invitation to play
* ``preschool`` - 36 months and up: two short sentences

Nothing here needs the network; nothing the child says leaves the robot.
"""

from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Iterable

from littlebot.brain.providers.base import BaseProvider, Message, ProviderReply
from littlebot.safety.content import normalise

MOCK_MODEL = "littlebot-scripted-1"

Tier = str  # "baby" | "toddler" | "preschool"


def tier_for_age(age_months: int | None) -> Tier:
    if age_months is None or age_months < 24:
        return "baby"
    if age_months < 36:
        return "toddler"
    return "preschool"


# --- vocabularies (extend freely; keep words simple and cheerful) -----------------------

ANIMAL_SOUNDS: dict[str, str] = {
    "dog": "woof", "doggy": "woof", "puppy": "woof", "cat": "meow", "kitty": "meow", "kitten": "meow",
    "cow": "moo", "duck": "quack", "sheep": "baa", "lamb": "baa", "pig": "oink", "horse": "neigh",
    "lion": "roar", "tiger": "roar", "bird": "tweet", "chick": "cheep", "chicken": "cluck", "hen": "cluck",
    "rooster": "cock a doodle doo", "frog": "ribbit", "owl": "hoo hoo", "monkey": "ooh ooh ah ah",
    "elephant": "toot", "snake": "hiss", "bee": "buzz", "mouse": "squeak", "goat": "maa",
    "donkey": "hee haw", "bear": "grr", "turkey": "gobble", "dinosaur": "rawr", "fish": "blub blub",
    "seal": "arf", "zebra": "neigh", "owlet": "hoo",
}

COLOURS: dict[str, str] = {
    "red": "a shiny apple", "blue": "the big sky", "yellow": "the sun", "green": "the grass",
    "orange": "an orange", "purple": "juicy grapes", "pink": "a pretty flower", "white": "fluffy clouds",
    "black": "the night sky", "brown": "a teddy bear", "grey": "a rain cloud", "gray": "a rain cloud",
}

SHAPES: dict[str, str] = {
    "circle": "round like a ball", "square": "four sides like a box", "triangle": "three sides like a slice of pizza",
    "star": "pointy and twinkly", "heart": "a love shape", "rectangle": "long like a door",
    "oval": "like an egg", "diamond": "like a kite",
}

FOODS: dict[str, str] = {
    "apple": "crunchy", "banana": "yellow and sweet", "carrot": "orange and crunchy", "cookie": "a yummy treat",
    "milk": "good for growing", "cheese": "yummy", "pizza": "yummy", "grapes": "juicy", "strawberry": "red and sweet",
    "pasta": "yummy", "bread": "soft", "egg": "round", "yogurt": "smooth and yummy",
}

BODY_PARTS = ("nose", "ears", "ear", "eyes", "toes", "fingers", "hands", "feet", "head", "knees", "shoulders",
              "tummy", "belly")


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    replies: dict[Tier, tuple[str, ...]]
    expression: str


def _r(name: str, pattern: str, expression: str, baby: Iterable[str], toddler: Iterable[str],
       preschool: Iterable[str]) -> _Rule:
    return _Rule(name, re.compile(pattern), {"baby": tuple(baby), "toddler": tuple(toddler),
                                             "preschool": tuple(preschool)}, expression)


# Simple topics with fixed lines (checked after the vocabulary topics below).
_RULES: tuple[_Rule, ...] = (
    _r("story", r"\b(?:story|stories|tale)\b", "curious",
       ["Little duck says quack. The end!"],
       ["A little duck found a red ball. Roll, roll, roll! The end!"],
       ["Once upon a time, a little {animal} found a shiny red ball. She rolled it to her friend. "
        "They played and giggled all day!"]),
    _r("song", r"\b(?:sing|song|songs|music|singing)\b", "happy",
       ["La la la! Twinkle twinkle!"],
       ["La la la! Twinkle, twinkle, little star! Sing with me!"],
       ["Twinkle, twinkle, little star, how I wonder what you are! Now you sing!"]),
    _r("why", r"^(?:but )?why\b|\bwhy (?:do|does|is|are|can|did)\b", "curious",
       ["Hmm! Good question!"],
       ["Ooh, good question! What do you think?"],
       ["What a great question! Let's wonder about it together. What do you think?"]),
    _r("counting", r"\b(?:count|counting|numbers?|how many)\b|\b(?:one two|1 2)\b", "excited",
       ["One, two, three! Yay!"],
       ["Let's count! One, two, three! Clap clap clap!"],
       ["Let's count together! One, two, three, four, five! Can you count to ten?"]),
    _r("love", r"\b(?:i )?love (?:you|u)\b|\blike you\b", "affectionate",
       ["Aww! Big hugs!"],
       ["Aww, thank you! Give your grown-up a big hug!"],
       ["Aww, that's so kind! Let's share a big hug with your grown-up too!"]),
    _r("happy", r"\b(?:happy|glad|excited|yay)\b", "celebrating",
       ["Yay! Happy dance!"],
       ["Yay! Let's do a happy dance!"],
       ["Hooray! That makes me want to wiggle. Let's do a happy dance together!"]),
    _r("sad", r"\b(?:sad|crying|cry|unhappy|lonely)\b", "affectionate",
       ["Aww. Hugs!"],
       ["Aww. A cuddle with your grown-up can help."],
       ["Aww, I'm sorry you feel sad. A big cuddle with your grown-up can really help."]),
    _r("tired", r"\b(?:tired|sleepy|yawn|nap|bedtime)\b", "sleepy",
       ["Yawn! Night night."],
       ["Yawn! Maybe it's time for a little rest."],
       ["Yawn! Even robots get sleepy. Maybe it's time for a cosy rest with your grown-up."]),
    _r("angry", r"\b(?:angry|mad|grumpy|cross|frustrated)\b", "affectionate",
       ["Big breath. In, out."],
       ["Let's take a big breath together. In and out."],
       ["Feeling grumpy is okay. Let's take a big breath together. In, and out. Ahh!"]),
    _r("robot", r"\bare you (?:a )?(?:real|alive|robot|person|human|boy|girl)\b|\bwhat are you\b", "playful",
       ["Robot! Beep boop!"],
       ["I'm a robot! Beep boop!"],
       ["I'm a robot, not a person. Robots like me love to play games!"]),
    _r("name", r"\b(?:what'?s|what is) your name\b|\bwho are you\b|\byour name\b", "happy",
       ["{robot}! Beep boop!"],
       ["I'm {robot}, a little robot!"],
       ["I'm {robot}, a little robot who loves to play and sing!"]),
    _r("how_are_you", r"\bhow are you\b|\bhow'?re you\b|\bhow do you feel\b", "happy",
       ["Bouncy! Yay!"],
       ["I'm feeling bouncy! How about you?"],
       ["I'm feeling bouncy and ready to play! How are you today?"]),
    _r("play", r"\b(?:play|game|games|peekaboo|peek a boo|hide and seek)\b", "playful",
       ["Peekaboo! I see you!"],
       ["Let's play peekaboo! Cover your eyes!"],
       ["Let's play peekaboo! Cover your eyes, count to three, and then peek!"]),
    _r("thanks", r"\b(?:thank you|thanks|thank u|ta)\b", "happy",
       ["Yay! Welcome!"],
       ["You're welcome!"],
       ["You're very welcome! That was kind of you."]),
    _r("goodnight", r"\b(?:good ?night|night night|nighty night|sweet dreams)\b", "sleepy",
       ["Night night!"],
       ["Night night! Sweet dreams!"],
       ["Night night! Sweet dreams and a cosy sleep!"]),
    _r("goodbye", r"\b(?:bye|goodbye|good bye|see you|see ya|bye bye)\b", "happy",
       ["Bye bye! Wave wave!"],
       ["Bye bye! See you soon!"],
       ["Bye bye! Have fun with your grown-up. See you soon!"]),
    _r("greeting", r"\b(?:hi|hello|hey|hiya|howdy|good morning|good afternoon|good evening)\b", "happy",
       ["Hi hi! Wave wave!"],
       ["Hello, friend! Can you wave hello?"],
       ["Hello there! I'm so happy to see you. Can you wave hello?"]),
)

_UNKNOWN: dict[Tier, tuple[str, ...]] = {
    "baby": ("Ooh! Wow!", "Hee hee! Yay!", "Wow! Clap clap!"),
    "toddler": ("Ooh, tell me more!", "Wow! Can you show me?", "Hmm! What a fun idea!"),
    "preschool": ("Ooh, that sounds interesting! Can you tell me more about it?",
                  "Wow, what a fun idea! Can you show me?",
                  "Hmm, I wonder about that too! What do you think?"),
}

_WORD = re.compile(r"[a-z]+")


def _pick(options: tuple[str, ...], seed: str) -> str:
    return options[zlib.crc32(seed.encode("utf-8", "ignore")) % len(options)]


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:]


class MockProvider(BaseProvider):
    name = "mock"
    is_cloud = False
    model = MOCK_MODEL

    def __init__(
        self,
        *,
        age_months: Callable[[], int | None] | None = None,
        robot_name: str = "LittleBot",
        favourite_animals: Callable[[], list[str]] | None = None,
    ) -> None:
        self._age = age_months or (lambda: None)
        self.robot_name = robot_name
        self._favourites = favourite_animals or (lambda: [])

    def respond(self, text: str, age_months: int | None = None) -> tuple[str, str, str]:
        """Pure function used by ``reply``: returns (reply, expression, topic)."""
        tier = tier_for_age(age_months)
        norm = normalise(text)
        words = _WORD.findall(norm)
        if not words:
            return _pick(_RULES[-1].replies[tier], norm), "happy", "greeting"
        # Rule topics that win over a mentioned noun ("tell me a story about a cow", "why do birds fly").
        for rule in _RULES[:3]:
            if rule.pattern.search(norm):
                return self._fill(_pick(rule.replies[tier], norm), words), rule.expression, rule.name
        animal = next((w for w in words if w in ANIMAL_SOUNDS), None)
        if animal is None:
            animal = next((w[:-1] for w in words if w.endswith("s") and w[:-1] in ANIMAL_SOUNDS), None)
        if animal is not None:
            return self._animal(animal, tier), "playful", "animal"
        colour = next((w for w in words if w in COLOURS), None)
        if colour is not None:
            return self._colour(colour, tier), "happy", "colour"
        shape = next((w for w in words if w in SHAPES), None)
        if shape is None:
            shape = next((w[:-1] for w in words if w.endswith("s") and w[:-1] in SHAPES), None)
        if shape is not None:
            return self._shape(shape, tier), "curious", "shape"
        for rule in _RULES[3:]:
            if rule.pattern.search(norm):
                return self._fill(_pick(rule.replies[tier], norm), words), rule.expression, rule.name
        food = next((w for w in words if w in FOODS), None)
        if food is not None:
            return self._food(food, tier), "happy", "food"
        part = next((w for w in words if w in BODY_PARTS), None)
        if part is not None:
            return self._body(part, tier), "playful", "body"
        return _pick(_UNKNOWN[tier], norm), "curious", "unknown"

    # -- templated topics -------------------------------------------------
    def _fill(self, template: str, words: list[str]) -> str:
        animal = next((w for w in words if w in ANIMAL_SOUNDS), None) or self._favourite_animal() or "duck"
        return template.replace("{robot}", self.robot_name).replace("{animal}", animal)

    def _favourite_animal(self) -> str | None:
        try:
            for item in self._favourites() or []:
                w = normalise(str(item)).rstrip("s")
                if w in ANIMAL_SOUNDS:
                    return w
        except Exception:  # noqa: BLE001 - never let profile data break a reply
            return None
        return None

    @staticmethod
    def _animal(animal: str, tier: Tier) -> str:
        sound = ANIMAL_SOUNDS[animal]
        if tier == "baby":
            return f"{_cap(sound)}! {_cap(sound)}!"
        if tier == "toddler":
            return f"The {animal} says {sound}! Can you say {sound}?"
        return f"The {animal} says {sound}, {sound}! Can you make a {animal} sound with me?"

    @staticmethod
    def _colour(colour: str, tier: Tier) -> str:
        thing = COLOURS[colour]
        if tier == "baby":
            return f"{_cap(colour)}! Pretty {colour}!"
        if tier == "toddler":
            return f"{_cap(colour)} like {thing}! Can you find something {colour}?"
        return f"{_cap(colour)}, like {thing}! I spy something {colour}. Can you find something {colour} near you?"

    @staticmethod
    def _shape(shape: str, tier: Tier) -> str:
        desc = SHAPES[shape]
        if tier == "baby":
            return f"A {shape}! Wow!"
        if tier == "toddler":
            return f"A {shape} is {desc}!"
        return f"A {shape} is {desc}! Can you draw a {shape} in the air with your finger?"

    @staticmethod
    def _food(food: str, tier: Tier) -> str:
        desc = FOODS[food]
        if tier == "baby":
            return f"Yum yum! {_cap(food)}!"
        if tier == "toddler":
            return f"Yum! {_cap(food)} is {desc}!"
        return f"Yum yum! {_cap(food)} is {desc}. What is your favourite snack?"

    @staticmethod
    def _body(part: str, tier: Tier) -> str:
        if tier == "baby":
            return f"Boop! {_cap(part)}!"
        if tier == "toddler":
            return f"Can you touch your {part}? Boop!"
        return f"Let's play a game! Can you touch your {part}? Now wiggle your toes!"

    # -- provider interface ----------------------------------------------
    async def reply(self, messages: list[Message], *, system: str = "", max_tokens: int = 120,
                    timeout: float = 5.0) -> ProviderReply:
        t0 = time.monotonic()
        last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        try:
            age = self._age()
        except Exception:  # noqa: BLE001
            age = None
        text, expression, _topic = self.respond(str(last), age)
        return ProviderReply(text=text, input_tokens=0, output_tokens=0, model=MOCK_MODEL,
                             latency_ms=int((time.monotonic() - t0) * 1000), stop_reason="scripted",
                             expression=expression)
