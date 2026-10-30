"""Twenty Questions: LittleBot thinks of something; the person asks yes-or-no questions and guesses.

Judging an arbitrary yes/no question ("is it bigger than a cat?") needs the AI, so the work is split:

* a *guess* ("is it a penguin?", "a giraffe!") is judged here, against the secret's name and aliases
  (assets/games/things.json, 120+ animals, foods, objects, vehicles and things in nature): instant;
* a *question* ("does it live in water?") is counted here (:meth:`TwentyQuestions.count_question`) and
  :meth:`handle` returns None, so the AI answers it, honestly, from :meth:`describe` (which holds the secret and
  the count). In GPT-Live the AI passes each question to the ``game_answer`` tool, which counts it and tells it
  the secret to answer from. The same words counted twice in a row (both paths) count once.

Guesses count as questions. After 20 there is one last guess. Winning in fewer questions scores more
(21 minus the questions used).
"""

from __future__ import annotations

import re
import time
from typing import Any, ClassVar

from littlebot.games.voice import answers, data
from littlebot.games.voice.base import Reply, VoiceGame, plural, sentence

LIMIT = 20
RECOUNT_SECONDS = 25.0  # the same question within this long counts once
CATEGORY_NAMES = {"animal": "an animal", "food": "something you can eat", "object": "a thing",
                  "vehicle": "something that takes you places", "nature": "something in nature"}
TOPIC_ALIASES = {"animals": "animal", "foods": "food", "objects": "object", "things": "object", "vehicles": "vehicle",
                 "transport": "vehicle", "nature": "nature", "food": "food", "animal": "animal", "object": "object",
                 "vehicle": "vehicle", "toys": "object", "stuff": "object", "space": "nature"}
_GUESS = re.compile(
    r"^(?:(?:um+|uh+|hmm+|ok|okay|so|well|then|and|oh|wait|i know|i got it|yes|yeah)\s+)*"
    r"(?:is it|it is|its|is that|are you thinking of|you are thinking of|youre thinking of|were you thinking of|"
    r"i guess|my guess is|i think its|i think it is|i think youre thinking of|is the answer|the answer is|"
    r"could it be|would it be|then its|i bet its|it must be|it has to be|my final guess is|final guess|"
    r"is it maybe|maybe its)\s+(?P<x>.+?)$")
_QUESTION = re.compile(
    r"^(?:(?:um+|uh+|hmm+|ok|okay|so|well|and|then|next question|question|my question is|next)\s+)*"
    r"(?:is|are|does|do|did|can|could|would|will|has|have|was|were|should|might|may|if|would i|could you|"
    r"do people|do you|does it|is it|can you|can it|would it)\b")
_CATEGORY_WORDS = frozenset(
    "animal animals mammal bird fish insect reptile bug pet food fruit vegetable drink thing object toy tool "
    "machine vehicle place person plant something living alive dead real natural electronic instrument furniture "
    "building clothing weapon game sport color colour".split())


def pick_category(topic: Any) -> str:
    t = answers.strip_article(answers.normalize(topic))
    return TOPIC_ALIASES.get(t, "")


def things() -> list[dict[str, Any]]:
    out = []
    for t in data.items("things", "things"):
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(a) for a in t.get("aliases") or [] if isinstance(a, str)]
        out.append({"name": name, "aliases": aliases, "category": str(t.get("category") or "object"),
                    "hint": str(t.get("hint") or ""), "key": data.key_of(name)})
    return out


def _bare(name: str) -> str:
    return answers.core(name)


def _singular(s: str) -> str:
    for suffix in ("ies", "es", "s"):
        if s.endswith(suffix) and len(s) > len(suffix) + 2:
            return s[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return s


def _with_article(alias: str, name: str) -> str:
    """How to say ``alias`` in "it's not ...": with an article when the thing's own name has one."""
    first = alias.split()[0].lower()
    if first in ("a", "an", "the", "some"):
        return alias
    if name.split()[0].lower() in ("a", "an"):
        return f"{'an' if alias[:1].lower() in 'aeiou' else 'a'} {alias}"
    return alias


def _same_thing(a: str, b: str) -> bool:
    """Two names of the same thing, give or take a slip or a plural ("penguins", "pengin", "ice-cream")."""
    if not a or not b:
        return False
    return answers.close(a, b) or _singular(a) == _singular(b) or answers.close(_singular(a), _singular(b))


class TwentyQuestions(VoiceGame):
    id: ClassVar[str] = "twenty_questions"
    title: ClassVar[str] = "Twenty Questions"
    card_kind: ClassVar[str] = "game_board"

    def __init__(self, *, limit: int = LIMIT, clock: Any = time.monotonic, **kw: Any) -> None:
        super().__init__(**kw)
        self.limit = max(5, min(30, int(limit or LIMIT)))
        self.clock = clock
        self.category_filter = pick_category(self.topic)
        self.tell_category = self.difficulty != "hard"
        self.secret: dict[str, Any] | None = None
        self.questions = 0
        self.hints = 0
        self.won = False
        self.last_counted = ""
        self.last_counted_at = -1e9
        self.guesses: list[str] = []
        self.known: set[str] = set()
        self.display: dict[str, str] = {}

    # ------------------------------------------------------------ the secret
    async def _begin(self) -> Reply:
        pool = things()
        self.known = {_singular(_bare(n)) for t in pool for n in [t["name"], *t["aliases"]]}
        self.display = {}
        for t in pool:
            for n in [*t["aliases"], t["name"]]:  # the thing's own name last: it wins for its plural
                key = _singular(_bare(n))
                if n.strip() and key:
                    own = key == _singular(_bare(t["name"]))
                    self.display[key] = t["name"] if own else _with_article(n, t["name"])
        if self.category_filter:
            pool = [t for t in pool if t["category"] == self.category_filter] or pool
        if not pool:
            self.state = "over"
            return Reply("Sorry, I can't think of anything right now!", done=True)
        seen: set[str] = set()
        if self.store is not None:
            try:
                seen = {k for k, _ in self.store.seen(self.id)}
            except Exception:  # noqa: BLE001
                seen = set()
        fresh = [t for t in pool if t["key"] not in seen] or pool
        self.secret = self.rng.choice(fresh)
        if self.store is not None:
            try:
                self.store.mark_seen(self.id, [(self.secret["key"], self.secret["name"])])
            except Exception:  # noqa: BLE001
                pass
        what = CATEGORY_NAMES.get(self.secret["category"], "something") if self.tell_category else "something"
        return Reply(f"Twenty Questions! I'm thinking of {what}. Ask me yes-or-no questions, and guess whenever "
                     f"you're ready. You have {self.limit} questions. Go!", sound="notes")

    @property
    def names(self) -> list[str]:
        s = self.secret or {}
        return [s.get("name", ""), *s.get("aliases", [])]

    @property
    def left(self) -> int:
        return max(0, self.limit - self.questions)

    def is_secret(self, text: str) -> bool:
        """``text`` names the secret itself (not just mentions it: "bigger than a cat" is not "a cat")."""
        said = answers.core(text)
        return bool(said) and any(_same_thing(said, _bare(n)) for n in self.names if n)

    # ------------------------------------------------------------ counting questions
    def count_question(self, text: str = "") -> int:
        """Count one question (the same words again within :data:`RECOUNT_SECONDS` count once). Returns how many
        have been asked."""
        key = answers.normalize(text)
        now = self.clock()
        if key and self.last_counted and now - self.last_counted_at <= RECOUNT_SECONDS and (
                key == self.last_counted or answers.edit_distance(key, self.last_counted, 4) <= 4):
            return self.questions
        self.questions += 1
        self.turns += 1
        self.last_counted, self.last_counted_at = key, now
        self.changed()
        return self.questions

    def just_counted(self) -> bool:
        return self.clock() - self.last_counted_at <= RECOUNT_SECONDS

    def classify(self, text: str) -> tuple[str, str]:
        """("guess", thing) / ("question", "") / ("other", "")."""
        s = answers.normalize(text)
        if not s:
            return "other", ""
        m = _GUESS.match(s)
        if m:
            x = answers.strip_article(m.group("x").strip())
            x = re.sub(r"^(?:a|an|the|some)\s+", "", x)
            if self.is_secret(x) or (_singular(x) in self.known and x not in _CATEGORY_WORDS):
                return "guess", x
            return "question", ""
        if _QUESTION.match(s):
            return "question", ""
        bare = answers.core(s)
        if self.is_secret(bare) or (_singular(bare) in self.known and len(bare.split()) <= 3):
            return "guess", bare
        return "other", ""

    # ------------------------------------------------------------ moves
    def move(self, text: str) -> Reply | None:
        if self.secret is None:
            return None
        kind, thing = self.classify(text)
        if kind == "guess":
            return self.guess(thing or text)
        if kind == "question":
            if self.questions >= self.limit:
                return Reply("That's all twenty questions! Time for your final guess: what am I thinking of?")
            self.count_question(text)
            return None  # the AI answers it (describe() holds the secret and the count)
        return None

    def guess(self, thing: str) -> Reply:
        """Judge a guess (it counts as a question)."""
        name = self.secret["name"] if self.secret else ""
        final = self.questions >= self.limit
        self.questions += 1
        self.turns += 1
        self.guesses.append(thing)
        if self.is_secret(thing):
            self.won = True
            self.score = max(1, self.limit + 1 - min(self.questions, self.limit))
            self.streak, self.best_streak = 1, 1
            end = self.finish("won")
            return Reply(f"Yes! It's {name}! You got it in {plural(self.questions, 'question')}. {end}", done=True,
                         sound="tada")
        shown = self.display.get(_singular(answers.core(thing)), thing)  # "a dog", not "dog"
        if final:
            end = self.finish("lost")
            return Reply(f"No, not {shown}. I was thinking of {name}! {end}", done=True, sound="uhoh")
        if self.questions >= self.limit:
            return Reply(f"No, it's not {shown}. That was question {self.limit}: you get one final guess!",
                         sound="boop")
        return Reply(f"No, it's not {shown}. {plural(self.left, 'question')} left.", sound="boop")

    def answer_tool(self, text: str) -> str:
        """For the GPT-Live path (the ``game_answer`` tool): judge a guess, or count a question and tell the AI
        how to answer it. Returns the tool result text."""
        kind, thing = self.classify(text)
        if kind == "guess" or (kind == "other" and len(answers.core(text).split()) <= 3 and answers.core(text)):
            reply = self._out(self.guess(thing or answers.core(text)))
            return f"ok: say exactly: {reply.say}"
        if self.questions >= self.limit:
            return "ok: say exactly: That's all twenty questions! Time for your final guess: what am I thinking of?"
        before = self.questions
        n = self.count_question(text)
        again = " (already counted)" if n == before else ""
        return (f"ok: question {n} of {self.limit} counted{again}. You are thinking of {self.secret['name']}: answer "
                f"their question honestly with yes, no or sometimes, in a few words, without naming it"
                + (f", and remind them {plural(self.left, 'question')} are left." if self.left <= 10 else ".")
                + (" That was their last question: ask for their final guess." if self.left == 0 else ""))

    def hint(self) -> Reply:
        if self.secret is None:
            return self._out(Reply("I'm not thinking of anything yet!"), remember=False)
        self.hints += 1
        if self.hints == 1 and self.secret["hint"]:
            return self._out(Reply(f"Here's a hint: {sentence(self.secret['hint'])}"), remember=False)
        if self.hints <= 2:
            first = _bare(self.secret["name"])[:1].upper()
            return self._out(Reply(f"Another hint: it starts with {first}."), remember=False)
        return self._out(Reply("No more hints! Keep asking."), remember=False)

    def skip(self) -> Reply:
        if self.secret is None:
            return self._out(Reply("Okay."), remember=False)
        end = self.finish("gave up")
        return self._out(Reply(f"I was thinking of {self.secret['name']}! {end}", done=True))

    def stop(self) -> Reply:
        if self.over or self.secret is None:
            return super().stop()
        end = self.finish("stopped")
        return self._out(Reply(f"Okay! I was thinking of {self.secret['name']}. {end}".strip(), done=True))

    def repeat(self) -> str:
        if self.secret is None:
            return super().repeat()
        what = CATEGORY_NAMES.get(self.secret["category"], "something") if self.tell_category else "something"
        return f"I'm thinking of {what}. You've asked {plural(self.questions, 'question')}; {self.left} left."

    def score_line(self) -> str:
        return f"You've asked {plural(self.questions, 'question')}, so {self.left} left."

    def summary(self) -> str:
        if self.end_reason == "won":
            return (self.best_line().strip() + " Want to play again?").strip()
        if self.end_reason == "stopped":
            return ""
        return "Want to play again?"

    def record_detail(self) -> Any:
        return {"questions": self.questions, "won": self.won}

    def record(self) -> None:
        if self.end_reason == "stopped" and not self.won:
            return  # an unfinished game isn't a result
        super().record()

    # ------------------------------------------------------------ for the AI and the screen
    def describe(self) -> str:
        if self.secret is None:
            return "Voice game on: Twenty Questions."
        name = self.secret["name"]
        if self.over:
            return f"Voice game: Twenty Questions just ended; the secret was {name}."
        cat = self.secret["category"]
        counted = f" Their last question (number {self.questions}) is already counted." if self.just_counted() \
            and self.questions else ""
        last = (" That was the last question: after answering, ask for their final guess."
                if self.questions >= self.limit else "")
        return (f"Voice game on: Twenty Questions. You (LittleBot) are thinking of: {name} ({cat}). They have asked "
                f"{self.questions} of {self.limit} questions.{counted}{last} Answer each yes/no question honestly "
                "about "
                f"a typical {_bare(name)}: yes, no, or sometimes, in a few words, and never name it unless they guess "
                "it. Guesses of a specific thing are judged automatically; if they clearly guessed it and the game "
                "missed it, call game_answer with their guess. Mention how many questions are left now and then.")

    def card_spec(self) -> dict[str, Any]:
        what = "something"
        if self.secret is not None and self.tell_category:
            what = CATEGORY_NAMES.get(self.secret["category"], "something")
        if self.over and self.secret is not None:
            text = f"It was {self.secret['name']}!"
            status = (f"Got it in {plural(self.questions, 'question')}!" if self.won else "Good game!")
        else:
            text = f"I'm thinking of {what}..."
            status = f"Question {min(self.questions, self.limit)} of {self.limit}" + (
                "  ·  final guess!" if self.questions >= self.limit else "")
        return {"card": "game_board", "title": "Twenty Questions", "game": self.id, "session": self.session,
                "text": text, "options": [], "status": status, "counter": [min(self.questions, self.limit), self.limit],
                "over": self.over, "won": self.won, "result": None}

    def status(self) -> dict[str, Any]:
        return {**super().status(), "questions": self.questions, "limit": self.limit, "won": self.won}


__all__ = ["LIMIT", "TwentyQuestions", "pick_category", "things"]
