"""The voice game engine: one deterministic game object that both voice paths drive.

The fast path (littlebot/assistant) hands the person's words to the running game *first* and speaks
:attr:`Reply.say` straight away, with no model call: "B", "Paris", "is there an E" are judged here in a
millisecond. :meth:`VoiceGame.handle` returns None for anything that is not a move ("this is hard!"), and only
those words go to the AI, which sees :meth:`VoiceGame.describe` in its context. In GPT-Live the AI plays the
same game through the skill's tools (littlebot/games/voice/skill.py).

Writing a game: subclass :class:`VoiceGame`, set ``id`` / ``title`` / ``card_kind``, and implement
``_begin`` (async: the first words), ``move`` (a possible answer -> Reply or None), ``hint``, ``skip``,
``describe`` and ``card_spec``. The base class handles the words every game understands (:func:`command`:
stop, hint, skip, repeat, score), keeps the score and the best streak, and records the result
(littlebot/games/voice/scores.py) when the game ends.

Threading: a game is only touched on the robot's asyncio loop (the skill routes card taps there). Cards read
:attr:`VoiceGame.view` (a fresh dict on every change, so reading it from the display thread is safe) and
:attr:`VoiceGame.version` to see that something changed.
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

from littlebot.games.voice import answers

log = logging.getLogger("littlebot.games.voice")

_LIVE: weakref.WeakValueDictionary[str, VoiceGame] = weakref.WeakValueDictionary()


def find(session: Any) -> VoiceGame | None:
    """The game a card spec's ``session`` names (while anything still holds it), or None."""
    return _LIVE.get(str(session or "")) if session else None


@dataclass
class Reply:
    """What the game says back. ``say``: the exact words (short, speakable); ``done``: the game just ended;
    ``card``: the game's card spec (JSON-safe; the skill keeps it on the screen); ``sound``: a sound effect
    name (``core.interfaces.SOUND_NAMES``) to play before the words, or None."""

    say: str
    done: bool = False
    card: dict[str, Any] | None = None
    sound: str | None = None


# ---------------------------------------------------------------- words every game understands
_POLITE = r"(?:(?:please|ok|okay|um+|uh+|hmm+|so|well|hey|littlebot|little bot|can you|could you|can we|could we|" \
          r"lets|let us|i want to|i wanna|i would like to|id like to|i need to|we need to|we should|time to|" \
          r"just|actually|now)\s+)*"
_TAIL = r"(?:\s+(?:please|now|then|thanks|thank you|littlebot|little bot|for now|for today|i think))*"
_GAME_NOUN = r"(?:game|games|playing|trivia|quiz|hangman|riddles?|round|questions|twenty questions|20 questions|" \
             r"would you rather|this|it|that)"
COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stop", re.compile(
        rf"^{_POLITE}(?:(?:stop|end|quit|finish|exit|cancel|leave|close)\s+(?:the\s+|this\s+|our\s+|my\s+)?{_GAME_NOUN}"
        rf"(?:\s+game)?|i quit|i want to quit|quit|no more {_GAME_NOUN}|game over|im done(?: playing)?|"
        rf"im finished(?: playing)?|we are done|were done(?: playing)?|thats enough(?: {_GAME_NOUN})?|"
        rf"enough {_GAME_NOUN}|i dont want to play(?: anymore| any more)?|lets do something else|"
        rf"lets play something else|stop playing){_TAIL}$")),
    ("hint", re.compile(
        rf"^{_POLITE}(?:(?:give me|can i have|can i get|could i have|i need|i want|got|have you got|any|"
        rf"how about|what about)\s+)?(?:a\s+|another\s+|one more\s+|some\s+|a little\s+)?"
        rf"(?:hint|clue|help|tip)s?(?:\s+please)?(?:\s+me)?{_TAIL}$|^{_POLITE}help me(?: out)?{_TAIL}$")),
    ("skip", re.compile(
        rf"^{_POLITE}(?:skip(?: it| this| that| this one| that one| the question| this question| this word|"
        rf" this riddle)?|pass|i pass|next(?: one| question| word| riddle| please)?|"
        rf"(?:i\s+)?(?:dont|do not|have no|got no) (?:know|idea|clue)(?: the answer)?|no idea|i have no idea|"
        rf"no clue|i give up|give up|i surrender|you got me|tell me(?: the answer)?|what is the answer|"
        rf"whats the answer|what was the answer|what is it|whats the word|what is the word|reveal(?: it| the answer)?|"
        rf"show me(?: the answer)?|just tell me){_TAIL}$")),
    ("repeat", re.compile(
        rf"^{_POLITE}(?:repeat(?: that| it| the question| the options| the riddle| please)?|"
        rf"say (?:that|it) again|(?:read|ask) (?:that|it|the question|the options|the riddle) again|again|"
        rf"come again|pardon(?: me)?|sorry|sorry what|what|huh|what did you say|i didnt (?:hear|catch) (?:that|it)|"
        rf"what (?:was|is) the (?:question|riddle)|what (?:were|are) the (?:options|choices|answers)|"
        rf"one more time|what was that){_TAIL}$")),
    ("score", re.compile(
        rf"^{_POLITE}(?:whats|what is|tell me) (?:my|the|our) score(?: so far)?|score|how am i doing|"
        rf"how (?:are we|am i) doing(?: so far)?|"
        rf"how many (?:points|have i got|do i have|did i get)(?: right)?(?: so far)?|"
        rf"how many lives(?: do i have)?(?: left)?|how many questions(?: are)?(?: left)?{_TAIL}$")),
)


def command(text: Any) -> str | None:
    """"stop", "hint", "skip", "repeat" or "score" when ``text`` asks for it, else None. A bare "stop" is not
    one: the voice's own stop handles it (it may mean "stop talking")."""
    s = answers.normalize(text)
    if not s or len(s) > 80:
        return None
    for name, pattern in COMMANDS:
        if pattern.match(s):
            return name
    return None


# ---------------------------------------------------------------- the game
class VoiceGame:
    """A voice game. See the module docstring."""

    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    card_kind: ClassVar[str] = "game_board"
    records_scores: ClassVar[bool] = True

    def __init__(self, *, store: Any = None, rng: random.Random | None = None, topic: str = "",
                 difficulty: str = "", now: Callable[[], float] = time.time, **options: Any) -> None:
        self.store = store  # a ScoreStore (scores, recently seen items) or None
        self.rng = rng or random.Random()
        self.topic = str(topic or "")
        self.difficulty = str(difficulty or "")
        self.now = now
        self.options = options
        self.session = uuid.uuid4().hex[:12]  # names this game to its card
        self.state = "new"  # new | playing | over
        self.score = 0
        self.out_of = 0  # what the score is out of (rounds played)
        self.streak = 0
        self.best_streak = 0
        self.turns = 0  # moves judged (a game nobody played isn't recorded)
        self.started_at = 0.0
        self.ended_at = 0.0
        self.end_reason = ""
        self.version = 0
        self.view: dict[str, Any] = {}
        self.last_say = ""  # for "repeat"
        self.recorded = False
        # Set by the skill: called from the display thread when a card option is tapped (it must only schedule).
        self.on_tap: Callable[[int], Any] | None = None
        _LIVE[self.session] = self

    def request_tap(self, index: int) -> bool:
        """A card's tap on option ``index`` (display thread): handed to the skill, which plays it on its loop."""
        fn = self.on_tap
        if fn is None or self.over:
            return False
        try:
            fn(int(index))
        except Exception:  # noqa: BLE001
            log.debug("voice game %s: the tap could not be passed on", self.id, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ lifecycle
    @property
    def over(self) -> bool:
        return self.state == "over"

    async def start(self) -> Reply:
        self.state = "playing"
        self.started_at = self.now()
        reply = await self._begin()
        return self._out(reply)

    async def _begin(self) -> Reply:
        raise NotImplementedError

    def handle(self, text: str) -> Reply | None:
        """The person's words: a Reply when they were a move (or a command), None when the AI should answer."""
        if self.over:
            return None
        try:
            cmd = command(text)
            if cmd == "stop":
                return self.stop()
            if cmd == "hint":
                return self.hint()
            if cmd == "skip":
                return self.skip()
            if cmd == "repeat":
                return self._out(Reply(self.repeat()), remember=False)
            if cmd == "score":
                return self._out(Reply(self.score_line()), remember=False)
            reply = self.move(str(text or ""))
        except Exception:  # noqa: BLE001 - a bug in one game must not break the conversation
            log.exception("voice game %s failed on a move", self.id)
            return None
        return self._out(reply) if reply is not None else None

    def move(self, text: str) -> Reply | None:
        return None

    def hint(self) -> Reply:
        return self._out(Reply("No hints in this one, sorry!"), remember=False)

    def skip(self) -> Reply:
        return self._out(Reply("We can't skip in this one."), remember=False)

    def tap(self, index: int) -> Reply | None:
        """A tap on option ``index`` of the card (a quiz tile, a would-you-rather side)."""
        return None

    def repeat(self) -> str:
        return self.last_say or f"We're playing {self.title}."

    def score_line(self) -> str:
        return f"You have {self.score} so far."

    def stop(self) -> Reply:
        if self.over:
            return Reply(f"{self.title} is over.", done=True, card=self.card_spec())
        return self._out(Reply(self.finish("stopped"), done=True, sound="tada" if self.score else None))

    def finish(self, reason: str = "done") -> str:
        """End the game (once): record the result and return the closing words."""
        if not self.over:
            self.state = "over"
            self.ended_at = self.now()
            self.end_reason = reason
            self.record()
            self.changed()
        return self.summary()

    def summary(self) -> str:
        return f"That's the end of {self.title}."

    def record(self) -> None:
        if self.recorded or not self.records_scores or self.store is None or self.turns <= 0:
            return
        self.recorded = True
        try:
            self.store.record(self.id, score=self.score, out_of=self.out_of, best_streak=self.best_streak,
                              detail=self.record_detail())
        except Exception:  # noqa: BLE001 - a score that can't be saved is not worth a crash
            log.warning("voice game %s: saving the score failed", self.id, exc_info=True)

    def record_detail(self) -> str:
        return self.topic

    def best_line(self) -> str:
        """ "That's a new high score!" when this game beat the best before it, else ""."""
        if self.store is None or not self.records_scores or self.turns <= 0:
            return ""
        try:
            best = self.store.best(self.id, before=self.started_at)
        except Exception:  # noqa: BLE001
            return ""
        if best is None:
            return ""
        if self.score > int(best.get("score") or 0):
            return " That's a new high score!"
        return ""

    # ------------------------------------------------------------ scoring
    def right(self, points: int = 1) -> None:
        self.turns += 1
        self.streak += 1
        self.best_streak = max(self.best_streak, self.streak)
        self.score += points

    def wrong(self) -> None:
        self.turns += 1
        self.streak = 0

    # ------------------------------------------------------------ for the AI and the screen
    def describe(self) -> str:
        return f"{self.title} is on."

    def card_spec(self) -> dict[str, Any]:
        return {"card": self.card_kind, "title": self.title, "game": self.id, "session": self.session}

    def changed(self) -> None:
        """The game moved on: rebuild the card's view (a new dict) and bump the version."""
        try:
            self.view = self.card_spec()
        except Exception:  # noqa: BLE001
            log.exception("voice game %s: card spec failed", self.id)
        self.version += 1

    def _out(self, reply: Reply, *, remember: bool = True) -> Reply:
        """Every reply leaves through here: remember the words (for "repeat"), refresh the card."""
        if reply.say and remember:
            self.last_say = reply.say
        self.changed()
        reply.card = dict(self.view)
        if self.over:
            reply.done = True
        return reply

    def status(self) -> dict[str, Any]:
        return {"game": self.id, "title": self.title, "state": self.state, "score": self.score,
                "out_of": self.out_of, "streak": self.streak, "best_streak": self.best_streak,
                "topic": self.topic, "difficulty": self.difficulty, "session": self.session}


# ---------------------------------------------------------------- speech helpers
def spoken_list(items: list[str], last: str = "or") -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def sentence(text: str) -> str:
    """Capitalised, ending in a full stop / ! / ?."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    end = t.rstrip("\"'”’)")
    return t if end and end[-1] in ".!?" else t + "."


__all__ = ["COMMANDS", "Reply", "VoiceGame", "command", "find", "plural", "sentence", "spoken_list"]
