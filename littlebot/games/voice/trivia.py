"""Trivia: a round of multiple-choice questions, A to D, judged instantly.

Questions come from the text model in ONE structured call for the whole round (``ask_json``: question, four
options, answer_index, fact) when the skill hands the game a ``model`` (``voice_games.use_model_questions``,
Claude set up, cloud allowed), else from the offline bank (assets/games/trivia.json, 100+ checked questions in
ten categories). The round never waits long for the model: a built-in category starts with a bank question if
the batch isn't back within :data:`QUICK_WAIT` seconds and switches to the model's questions as soon as they
arrive; a free topic ("quiz me on dinosaurs", no bank for it) waits up to :data:`MODEL_TIMEOUT`. Questions asked
lately are remembered (``voice_game_seen``) and not asked again soon, by the bank or the model.

Scoring: 10 points a right answer, +5 from the third right answer in a row (the streak bonus), half points after a
fifty-fifty hint. The result (points, right answers, best streak) is saved when the round ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from littlebot.games.voice import answers, data
from littlebot.games.voice.base import Reply, VoiceGame, sentence

log = logging.getLogger("littlebot.games.voice")

CATEGORIES = ("general", "science", "history", "geography", "sports", "movies", "music", "food", "canada", "tech")
LABELS = {"general": "General knowledge", "science": "Science", "history": "History", "geography": "Geography",
          "sports": "Sports", "movies": "Movies", "music": "Music", "food": "Food", "canada": "Canada",
          "tech": "Tech", "mixed": "Mixed"}
TOPIC_ALIASES = {
    "general knowledge": "general", "anything": "mixed", "everything": "mixed", "random": "mixed", "mix": "mixed",
    "mixed": "mixed", "all": "mixed", "any": "mixed", "a mix": "mixed", "whatever": "mixed", "surprise me": "mixed",
    "film": "movies", "films": "movies", "movie": "movies", "cinema": "movies", "disney": "movies",
    "sport": "sports", "athletics": "sports", "olympics": "sports",
    "technology": "tech", "computers": "tech", "computer": "tech", "computing": "tech", "internet": "tech",
    "canadian": "canada", "oh canada": "canada", "canadian history": "canada",
    "geo": "geography", "countries": "geography", "capitals": "geography", "world": "geography", "places": "geography",
    "maps": "geography", "cooking": "food", "food and drink": "food", "foods": "food", "songs": "music",
    "musicians": "music", "bands": "music", "sciences": "science", "the past": "history", "historical": "history",
}
LEVELS = ("easy", "medium", "hard")
LEVEL_ALIASES = {"simple": "easy", "kids": "easy", "beginner": "easy", "normal": "medium", "regular": "medium",
                 "average": "medium", "difficult": "hard", "tough": "hard", "expert": "hard", "challenging": "hard"}
LETTERS = "ABCD"
POINTS = 10
STREAK_BONUS = 5  # from the third right answer in a row
QUICK_WAIT = 1.5  # s a built-in category waits for the model's questions before starting with a bank one
MODEL_TIMEOUT = 6.0  # s for the model's whole round
MAX_AVOID = 40  # recently asked questions shown to the model ("don't repeat these")

PRAISE = ("Yes!", "Correct!", "That's right!", "You got it!", "Nailed it!", "Spot on!")
OOPS = ("Not quite:", "Nope:", "Close, but no:", "Sorry,", "Not this time:")

SYSTEM = (
    "You write trivia questions for LittleBot, a friendly home robot that hosts a spoken quiz for a family. "
    "Everything is read aloud: each question under 20 words, each option one to four words, no 'all of the "
    "above', no trick wording, no questions that depend on today's date or on recent events. Exactly one option "
    "is right. Only use facts that are well established and that you are certain of; if unsure, pick another "
    "question. The four options are distinct and plausible. 'fact' is one fun, accurate sentence under 20 words "
    "that adds something new. Family friendly: no violence, gore, crime, politics or scary topics. Vary the "
    "questions and don't repeat any listed under 'avoid'.")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer_index": {"type": "integer"},
                    "fact": {"type": "string"},
                },
                "required": ["question", "options", "answer_index", "fact"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}


@dataclass
class Question:
    text: str
    options: list[str]  # in the order they are read out
    answer: int
    fact: str = ""
    category: str = ""
    difficulty: str = ""
    source: str = "bank"  # bank | model
    key: str = field(default="")

    def __post_init__(self) -> None:
        if not self.key:
            self.key = data.key_of(self.text)

    @property
    def right_text(self) -> str:
        return self.options[self.answer]


def pick_topic(topic: Any) -> str:
    """A category id, "mixed", or the free topic itself (lower case, tidy) for the model."""
    t = answers.normalize(topic)
    t = answers.strip_article(t)
    for suffix in (" questions", " trivia", " quiz", " stuff", " facts"):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    if not t or t in ("trivia", "quiz", "questions"):
        return "mixed"
    if t in CATEGORIES:
        return t
    if t in TOPIC_ALIASES:
        return TOPIC_ALIASES[t]
    if t.endswith("s") and t[:-1] in CATEGORIES:
        return t[:-1]
    if t + "s" in CATEGORIES:
        return t + "s"
    return t[:40]


def pick_level(level: Any) -> str:
    t = answers.normalize(level)
    if t in LEVELS:
        return t
    return LEVEL_ALIASES.get(t, "medium")


def bank() -> list[Question]:
    """The offline bank (options not shuffled yet: the right one first)."""
    out = []
    for q in data.items("trivia", "questions"):
        text, right, wrong = str(q.get("q") or "").strip(), str(q.get("a") or "").strip(), q.get("wrong")
        if not text or not right or not isinstance(wrong, list) or len(wrong) != 3:
            continue
        out.append(Question(text, [right, *[str(w) for w in wrong]], 0, str(q.get("fact") or ""),
                            str(q.get("cat") or "general"), str(q.get("lvl") or "medium")))
    return out


def clean_model_questions(raw: Any, *, topic: str, level: str) -> list[Question]:
    """The model's questions that are fit to read out: four distinct short options, a valid answer, family
    friendly (littlebot/safety/content.py). Anything else is dropped."""
    items = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    try:
        from littlebot.safety.content import find_blocked  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        def find_blocked(_text: str) -> list[str]:
            return []
    out: list[Question] = []
    for it in items[:40]:
        if not isinstance(it, dict):
            continue
        text = " ".join(str(it.get("question") or "").split())
        opts = it.get("options")
        idx = it.get("answer_index")
        fact = " ".join(str(it.get("fact") or "").split())
        if not (8 <= len(text) <= 200) or not isinstance(opts, list) or len(opts) != 4:
            continue
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < 4:
            continue
        opts = [" ".join(str(o or "").split()).rstrip(".") for o in opts]
        if any(not o or len(o) > 60 for o in opts) or len({answers.core(o) for o in opts}) != 4:
            continue
        if len(fact) > 200:
            fact = ""
        if find_blocked(" ".join([text, *opts, fact])):
            continue
        out.append(Question(text, opts, idx, fact, topic, level, source="model"))
    return out


class Trivia(VoiceGame):
    id: ClassVar[str] = "trivia"
    title: ClassVar[str] = "Trivia"
    card_kind: ClassVar[str] = "quiz"

    def __init__(self, *, model: Any = None, questions: int = 10, quick_wait: float = QUICK_WAIT,
                 model_timeout: float = MODEL_TIMEOUT, **kw: Any) -> None:
        super().__init__(**kw)
        self.category = pick_topic(self.topic)
        self.level = pick_level(self.difficulty)
        self.free_topic = self.category not in CATEGORIES and self.category != "mixed"
        self.total = max(3, min(30, int(questions or 10)))
        self.model = model
        self.quick_wait = quick_wait
        self.model_timeout = model_timeout
        self.asked: list[Question] = []
        self.current: Question | None = None
        self.right_count = 0
        self.removed: list[int] = []  # options taken away by the fifty-fifty on this question
        self.reveal: dict[str, Any] | None = None  # the last answer, for the card's flash
        self._bank: list[Question] = []
        self._model_qs: list[Question] = []
        self._fetch: asyncio.Task[list[Question]] | None = None
        self.used_model = False

    # ------------------------------------------------------------ questions
    def _seen(self) -> list[tuple[str, str]]:
        try:
            return self.store.seen(self.id) if self.store is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _bank_pool(self, seen_keys: set[str]) -> list[Question]:
        pool = bank()
        if self.category in CATEGORIES:
            pool = [q for q in pool if q.category == self.category] or pool
        level_rank = {lv: i for i, lv in enumerate(LEVELS)}
        want = level_rank.get(self.level, 1)
        self.rng.shuffle(pool)
        # unseen first, then the ones asked longest ago; the asked level first, then its neighbours
        pool.sort(key=lambda q: (q.key in seen_keys, abs(level_rank.get(q.difficulty, 1) - want)))
        return pool

    async def _ask_model(self, avoid: list[str]) -> list[Question]:
        topic = LABELS.get(self.category, self.category) if not self.free_topic else self.category
        if self.category == "mixed":
            topic = "a mix of general knowledge, science, history, geography, sports, movies, music and food"
        user = json.dumps({"topic": topic, "difficulty": self.level, "count": self.total, "avoid": avoid[:MAX_AVOID]},
                          ensure_ascii=False)
        raw = await self.model.ask_json(system=SYSTEM, user=user, schema=SCHEMA, timeout=self.model_timeout,
                                        max_tokens=min(4096, 250 + 130 * self.total))
        qs = clean_model_questions(raw, topic=self.category, level=self.level)
        log.info("trivia: the model wrote %d usable questions of %d asked", len(qs), self.total)
        return qs

    def _take_model(self) -> None:
        """Move the model's questions in once they're back (never blocks)."""
        task = self._fetch
        if task is None or not task.done():
            return
        self._fetch = None
        try:
            qs = task.result()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 - offline, slow, refused: the bank carries on
            log.info("trivia: no model questions (%s)", getattr(e, "kind", type(e).__name__))
            return
        asked = {answers.core(q.text) for q in self.asked} | {answers.core(q.right_text) for q in self.asked}
        seen = {k for k, _ in self._seen()}
        self._model_qs = [q for q in qs if answers.core(q.text) not in asked and q.key not in seen] or \
            [q for q in qs if answers.core(q.text) not in asked]
        self.used_model = self.used_model or bool(self._model_qs)

    def _next(self) -> Question | None:
        self._take_model()
        q = self._model_qs.pop(0) if self._model_qs else (self._bank.pop(0) if self._bank else None)
        if q is None:
            return None
        if q.source == "bank":  # the bank lists the right answer first: shuffle (the model's order is random)
            order = list(range(4))
            self.rng.shuffle(order)
            q = Question(q.text, [q.options[i] for i in order], order.index(q.answer), q.fact, q.category,
                         q.difficulty, q.source, q.key)
        return q

    async def _begin(self) -> Reply:
        seen = self._seen()
        seen_keys = {k for k, _ in seen}
        self._bank = self._bank_pool(seen_keys)
        note = ""
        if self.model is not None:
            self._fetch = asyncio.ensure_future(self._ask_model([t for _, t in seen if t]))
            wait = self.model_timeout + 0.5 if self.free_topic else self.quick_wait
            try:
                await asyncio.wait_for(asyncio.shield(self._fetch), wait)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - handled in _take_model
                pass
            self._take_model()
        if self.free_topic and not self._model_qs:
            note = f"I don't have {self.category} questions handy, so let's do a mix! "
            self._cancel_fetch()
            self.category = "mixed"
            self.free_topic = False
            self._bank = self._bank_pool(seen_keys)
        if not self._bank and not self._model_qs:
            self.state = "over"
            return Reply("Sorry, I can't find my trivia questions right now.", done=True)
        what = LABELS.get(self.category, self.category.title()) if self.category != "mixed" else ""
        intro = f"{note}Trivia time! {self.total} {what.lower() + ' ' if what else ''}questions" + (
            f", {self.level}" if self.level != "medium" else "") + ". Say A, B, C or D."
        intro = intro.replace("general knowledge questions", "general knowledge questions")
        self._advance()
        return Reply(f"{intro} {self._ask_text()}", sound="notes")

    def _cancel_fetch(self) -> None:
        if self._fetch is not None and not self._fetch.done():
            self._fetch.cancel()
        self._fetch = None

    def _advance(self) -> bool:
        """The next question becomes the current one (False: none left or the round is over)."""
        self.removed = []
        if len(self.asked) >= self.total:
            self.current = None
            return False
        q = self._next()
        if q is None:
            self.current = None
            return False
        self.current = q
        self.asked.append(q)
        if self.store is not None:
            try:
                self.store.mark_seen(self.id, [(q.key, q.text)])
            except Exception:  # noqa: BLE001
                pass
        return True

    @property
    def number(self) -> int:
        return len(self.asked)

    def _options_text(self, q: Question) -> str:
        parts = [f"{LETTERS[i]}, {o}" for i, o in enumerate(q.options) if i not in self.removed]
        if len(parts) > 1:
            parts[-1] = "Or " + parts[-1]
        return ". ".join(parts) + "?"

    def _ask_text(self) -> str:
        q = self.current
        if q is None:
            return ""
        label = "Last question" if self.number == self.total else f"Question {self.number}"
        return f"{label}: {sentence(q.text)} {self._options_text(q)}"

    # ------------------------------------------------------------ moves
    def move(self, text: str) -> Reply | None:
        q = self.current
        if q is None:
            return None
        i = answers.match_choice(text, q.options)
        if i is None or i in self.removed:
            return None
        return self._answer(i)

    def tap(self, index: int) -> Reply | None:
        if self.over or self.current is None or not isinstance(index, int) or not 0 <= index < 4:
            return None
        if index in self.removed:
            return None
        return self._out(self._answer(index))

    def _answer(self, i: int) -> Reply:
        q = self.current
        assert q is not None
        good = i == q.answer
        letter = LETTERS[q.answer]
        if good:
            points = (POINTS // 2 if self.removed else POINTS)
            self.right(0)
            self.right_count += 1
            if self.streak >= 3:
                points += STREAK_BONUS
            self.score += points
            streak = {3: " Three in a row!", 5: " Five in a row, you're on fire!",
                      7: " Seven straight!", 10: " Ten in a row, incredible!"}.get(self.streak, "")
            said = f"{self.rng.choice(PRAISE)} {q.right_text}.{streak}"
            sound = "success"
        else:
            self.wrong()
            said = f"{self.rng.choice(OOPS)} it's {letter}, {q.right_text}."
            sound = "uhoh"
        if q.fact:
            said += f" {sentence(q.fact)}"
        self.reveal = {"id": self.number, "question": q.text, "options": list(q.options), "chosen": i,
                       "answer": q.answer, "right": good}
        return self._then(said, sound)

    def _then(self, said: str, sound: str | None) -> Reply:
        """After an answer (or a skip): the next question, or the end of the round."""
        if self._advance():
            return Reply(f"{said} {self._ask_text()}", sound=sound)
        end = self.finish("done")
        return Reply(f"{said} {end}", done=True, sound="tada" if self.right_count * 2 >= self.total else sound)

    def hint(self) -> Reply:
        q = self.current
        if q is None:
            return self._out(Reply("No question right now."), remember=False)
        if self.removed:
            left = [i for i in range(4) if i not in self.removed]
            return self._out(Reply(f"That's all the help I've got: {LETTERS[left[0]]} or {LETTERS[left[1]]}?"),
                             remember=False)
        wrong = [i for i in range(4) if i != q.answer]
        self.rng.shuffle(wrong)
        self.removed = sorted(wrong[:2])
        left = [i for i in range(4) if i not in self.removed]
        a, b = left
        return self._out(Reply(f"Fifty-fifty: it's either {LETTERS[a]}, {q.options[a]}, or {LETTERS[b]}, "
                               f"{q.options[b]}."), remember=False)

    def skip(self) -> Reply:
        q = self.current
        if q is None:
            return self._out(Reply("No question right now."), remember=False)
        self.wrong()
        self.reveal = {"id": self.number, "question": q.text, "options": list(q.options), "chosen": -1,
                       "answer": q.answer, "right": False}
        return self._out(self._then(f"Skipping! It was {LETTERS[q.answer]}, {q.right_text}.", None))

    def repeat(self) -> str:
        return self._ask_text() if self.current is not None else super().repeat()

    def score_line(self) -> str:
        done = self.number - (1 if self.current is not None else 0)
        return f"You have {self.score} points: {self.right_count} of {done} right." if done else \
            "No points yet: this is the first question!"

    def summary(self) -> str:
        n = self.number if self.end_reason == "stopped" and self.current is not None else len(self.asked)
        played = max(0, n - (1 if self.end_reason == "stopped" and self.current is not None else 0))
        self.out_of = played
        if played == 0:
            return "Okay, no trivia for now."
        if self.end_reason == "stopped":
            head = f"Okay, we'll stop there. You got {self.right_count} of {played} right"
        else:
            head = f"That's the end! You got {self.right_count} out of {played}"
        line = f"{head}, for {self.score} points."
        if self.best_streak >= 3:
            line += f" Best streak: {self.best_streak}."
        line += self.best_line()
        if self.end_reason != "stopped":
            line += " Want another round?"
        return line

    def finish(self, reason: str = "done") -> str:
        if not self.over:
            asked = len(self.asked) - (1 if reason == "stopped" and self.current is not None else 0)
            self.out_of = max(0, asked)
            self._cancel_fetch()
        return super().finish(reason)

    def record_detail(self) -> Any:
        return {"topic": self.category, "level": self.level, "right": self.right_count, "model": self.used_model}

    # ------------------------------------------------------------ for the AI and the screen
    def describe(self) -> str:
        cat = LABELS.get(self.category, self.category)
        if self.over:
            return (f"Voice game: Trivia just ended: {self.right_count} of {self.out_of} right, {self.score} points. "
                    "If they want another round, call start_voice_game.")
        q = self.current
        head = (f"Voice game on: Trivia ({cat}, {self.level}), question {self.number} of {self.total}, "
                f"{self.score} points ({self.right_count} right, streak {self.streak}).")
        if q is None:
            return head
        opts = " ".join(f"{LETTERS[i]}) {o}" for i, o in enumerate(q.options) if i not in self.removed)
        return (f"{head} Current question: {q.text} Options: {opts}. Their answers (a letter or an option) are "
                "judged automatically before you hear anything, so never judge or give away the answer; if they "
                "say something else, answer in a few words and steer back to the question. They can say hint, "
                "skip, repeat or stop.")

    def card_spec(self) -> dict[str, Any]:
        q = self.current
        spec: dict[str, Any] = {
            "card": "quiz", "title": "Trivia", "game": self.id, "session": self.session,
            "category": LABELS.get(self.category, self.category.title()), "number": self.number, "total": self.total,
            "score": self.score, "right": self.right_count, "streak": self.streak, "over": self.over,
            "question": q.text if q is not None else "", "options": list(q.options) if q is not None else [],
            "removed": list(self.removed), "reveal": dict(self.reveal) if self.reveal else None,
        }
        if self.over:
            spec["final"] = {"right": self.right_count, "total": self.out_of, "points": self.score,
                             "streak": self.best_streak}
        return spec

    def status(self) -> dict[str, Any]:
        return {**super().status(), "number": self.number, "total": self.total, "right": self.right_count,
                "category": self.category, "level": self.level, "model_questions": self.used_model}


__all__ = ["CATEGORIES", "LABELS", "Question", "SCHEMA", "SYSTEM", "Trivia", "bank", "clean_model_questions",
           "pick_level", "pick_topic"]
