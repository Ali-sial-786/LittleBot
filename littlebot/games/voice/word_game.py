"""Hangman (a word guess): LittleBot picks a word, the screen shows its blanks, the person says letters.

"E", "is there an S", "the letter T", "T as in Tom" guess a letter; "is it elephant", "the word is elephant"
(or just a word of the right length) guesses the whole word. Six lives; a wrong letter or a wrong word costs
one. A round is :data:`WORDS` words (``voice_games.hangman_words``); the score is the words solved. Words come
from assets/games/words.json (180+ words in ten categories, a category per word unless one was asked for),
and words used lately are not picked again soon.

Spoken feedback is short ("Yes, two E's!", "No S. 4 lives left.") because the screen shows the rest.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from littlebot.games.voice import answers, data
from littlebot.games.voice.base import Reply, VoiceGame, plural

LIVES = 6
WORDS = 5
HINTS_PER_WORD = 2
VOWEL_SOUND = set("AEFHILMNORSX")  # letters whose names start with a vowel sound: "an S"
TOPIC_ALIASES = {"animal": "animals", "foods": "food", "sport": "sports", "planets": "space",
                 "house": "around the house", "home": "around the house", "household": "around the house",
                 "instruments": "music",
                 "job": "jobs", "work": "jobs", "country": "countries", "places": "countries", "vehicle": "vehicles",
                 "transport": "vehicles", "cars": "vehicles", "canadian": "canada", "weather": "nature"}
_GUESS_CUE = re.compile(
    r"^(?:(?:um+|uh+|hmm+|ok|okay|so|well|oh|wait|yes|yeah)\s+)*"
    r"(?:is it|is the word|is it the word|the word is|its|it is|i think its|i think it is|i think the word is|"
    r"my guess is|i guess|i know|i know its|i know it|i got it|i got it its|could it be|the answer is|"
    r"is the answer|i want to guess|i want to solve|let me solve|i will solve|ill solve|solve|"
    r"i want to guess the word|i guess the word|ill guess the word|guess)\s+(?:the\s+word\s+)?(?:an?\s+|the\s+)?"
    r"(?P<w>[a-z][a-z ]*)$")
_NOT_GUESSES = frozenset(
    "yes no yeah nope ok okay hmm um uh what wait hello hi thanks thank you hey sure maybe why how cool nice wow "
    "oops really great good fine sorry please again help hint skip stop letter word".split())


def pick_category(topic: Any, categories: list[str]) -> str:
    t = answers.strip_article(answers.normalize(topic))
    t = t.removesuffix(" words").removesuffix(" hangman").strip()
    t = TOPIC_ALIASES.get(t, t)
    return t if t in categories else ""


def letter_name(letter: str) -> str:
    """ "an S", "a T"."""
    return f"{'an' if letter in VOWEL_SOUND else 'a'} {letter}"


class Hangman(VoiceGame):
    id: ClassVar[str] = "hangman"
    title: ClassVar[str] = "Hangman"
    card_kind: ClassVar[str] = "word_game"

    def __init__(self, *, words: int = WORDS, lives: int = LIVES, **kw: Any) -> None:
        super().__init__(**kw)
        cats = data.load("words").get("categories")
        self.bank: dict[str, list[str]] = {str(k): [str(w).upper() for w in v if str(w).isalpha()]
                                           for k, v in (cats.items() if isinstance(cats, dict) else [])
                                           if isinstance(v, list)}
        self.bank = {k: v for k, v in self.bank.items() if v}
        self.fixed = pick_category(self.topic, list(self.bank))
        self.total = max(1, min(20, int(words or WORDS)))
        self.max_lives = max(1, int(lives or LIVES))
        self.word = ""
        self.category = ""
        self.guessed: list[str] = []
        self.wrong_letters: list[str] = []
        self.lives = self.max_lives
        self.hints = 0
        self.number = 0
        self.used: list[str] = []
        self.result: dict[str, Any] | None = None  # the last word's end, for the card
        self.last: dict[str, Any] | None = None  # the last letter, for the card

    # ------------------------------------------------------------ words
    def _pick(self) -> bool:
        seen = set()
        if self.store is not None:
            try:
                seen = {k for k, _ in self.store.seen(self.id)}
            except Exception:  # noqa: BLE001
                seen = set()
        cats = [self.fixed] if self.fixed else list(self.bank)
        if not cats:
            return False
        pool = [(c, w) for c in cats for w in self.bank[c] if w not in self.used]
        fresh = [p for p in pool if data.key_of(p[1]) not in seen]
        pool = fresh or pool
        if not pool:
            return False
        if not self.fixed:  # a category at random first (they differ in size), then a word in it
            cat = self.rng.choice(sorted({c for c, _ in pool}))
            pool = [p for p in pool if p[0] == cat]
        self.category, self.word = self.rng.choice(pool)
        self.used.append(self.word)
        self.guessed, self.wrong_letters, self.lives, self.hints = [], [], self.max_lives, 0
        self.number += 1
        self.last = None
        if self.store is not None:
            try:
                self.store.mark_seen(self.id, [(data.key_of(self.word), self.word)])
            except Exception:  # noqa: BLE001
                pass
        return True

    @property
    def solved(self) -> bool:
        return bool(self.word) and all(c in self.guessed for c in self.word)

    def pattern(self) -> list[str]:
        return [c if c in self.guessed else "" for c in self.word]

    def spoken_pattern(self) -> str:
        return " ".join(c if c in self.guessed else "blank" for c in self.word)

    def _intro_word(self) -> str:
        cat = self.category
        what = {"around the house": "something around the house", "canada": "something Canadian",
                "countries": "a country", "jobs": "a job", "vehicles": "a vehicle", "animals": "an animal",
                "space": "something in space", "nature": "something in nature", "music": "something musical",
                "sports": "a sport", "food": "a food"}.get(cat, cat)
        return f"It's {what}, with {len(self.word)} letters."

    async def _begin(self) -> Reply:
        if not self._pick():
            self.state = "over"
            return Reply("Sorry, I can't find my word list right now.", done=True)
        n = f"{self.total} words" if self.total > 1 else "one word"
        return Reply(f"Hangman! {n}, {self.max_lives} lives each. {self._intro_word()} Guess a letter, or the "
                     "whole word.", sound="notes")

    # ------------------------------------------------------------ moves
    def move(self, text: str) -> Reply | None:
        if not self.word:
            return None
        letter = answers.spoken_letter(text)
        if letter:
            return self._letter(letter.upper())
        word = self._word_guess(text)
        if word:
            return self._guess_word(word)
        return None

    def _word_guess(self, text: str) -> str:
        s = answers.normalize(text)
        m = _GUESS_CUE.match(s)
        if m:
            w = answers.compact(m.group("w"))
            # "is it an animal?" asks about the category: only a word about the right length is a guess
            if w.isalpha() and len(w) >= 2 and (abs(len(w) - len(self.word)) <= 1 or self._is_word(w)):
                return w
            return ""
        s = answers.strip_article(answers.strip_filler(s))
        w = answers.compact(s)
        if not w.isalpha() or len(s.split()) > 2 or s in _NOT_GUESSES or w in _NOT_GUESSES:
            return ""
        if len(w) == len(self.word) or self._is_word(w):  # a bare word of the right length is a guess
            return w
        return ""

    def _is_word(self, w: str) -> bool:
        target = self.word.lower()
        return w == target or (len(target) >= 6 and answers.edit_distance(w, target, 1) <= 1)

    def _letter(self, letter: str) -> Reply:
        if letter in self.guessed or letter in self.wrong_letters:
            return Reply(f"You already tried {letter}.")
        self.last = {"id": len(self.guessed) + len(self.wrong_letters) + 1, "letter": letter,
                     "hit": letter in self.word}
        if letter in self.word:
            self.guessed.append(letter)
            n = self.word.count(letter)
            if self.solved:
                return self._word_done(True, f"Yes! You got it: {self.word.capitalize()}!")
            said = f"Yes, {'two' if n == 2 else 'three' if n == 3 else n} {letter}'s!" if n > 1 else \
                f"Yes, there's {letter_name(letter)}."
            return Reply(said, sound="pop")
        self.wrong_letters.append(letter)
        self.lives -= 1
        if self.lives <= 0:
            return self._word_done(False, f"No {letter}. Out of lives! The word was {self.word.capitalize()}.")
        return Reply(f"No {letter}. {plural(self.lives, 'life', 'lives')} left.", sound="boop")

    def _guess_word(self, w: str) -> Reply:
        if self._is_word(w):
            for c in self.word:
                if c not in self.guessed:
                    self.guessed.append(c)
            return self._word_done(True, f"Yes! It's {self.word.capitalize()}!")
        self.lives -= 1
        if self.lives <= 0:
            return self._word_done(False, f"Nope, not {w.capitalize()}. Out of lives! The word was "
                                          f"{self.word.capitalize()}.")
        return Reply(f"Nope, it's not {w.capitalize()}. {plural(self.lives, 'life', 'lives')} left.", sound="boop")

    def _word_done(self, won: bool, said: str) -> Reply:
        if won:
            self.right()
        else:
            self.wrong()
        self.out_of = self.number
        self.result = {"id": self.number, "word": self.word, "won": won}
        if self.number >= self.total or not self._pick():
            end = self.finish("done")
            return Reply(f"{said} {end}", done=True, sound="tada" if self.score else ("success" if won else "uhoh"))
        return Reply(f"{said} Word {self.number}: {self._intro_word()}", sound="success" if won else "uhoh")

    def hint(self) -> Reply:
        if not self.word:
            return self._out(Reply("No word right now."), remember=False)
        missing = [c for c in dict.fromkeys(self.word) if c not in self.guessed]
        if self.hints >= HINTS_PER_WORD or len(missing) <= 1:
            return self._out(Reply("No more hints for this word. You can do it!"), remember=False)
        self.hints += 1
        letter = self.rng.choice(missing)
        self.guessed.append(letter)
        self.last = {"id": len(self.guessed) + len(self.wrong_letters), "letter": letter, "hit": True}
        return self._out(Reply(f"Here's a letter: there's {letter_name(letter)}."), remember=False)

    def skip(self) -> Reply:
        if not self.word:
            return self._out(Reply("No word right now."), remember=False)
        return self._out(self._word_done(False, f"The word was {self.word.capitalize()}."))

    def repeat(self) -> str:
        if not self.word:
            return super().repeat()
        tried = ", ".join(self.guessed + self.wrong_letters)
        return (f"{self._intro_word()} So far: {self.spoken_pattern()}." + (f" You've tried {tried}." if tried else "")
                + f" {plural(self.lives, 'life', 'lives')} left.")

    def score_line(self) -> str:
        done = self.number - 1
        lives = plural(self.lives, "life", "lives")
        return f"You've solved {self.score} of {done} so far, and you have {lives} left." if done else \
            f"First word! {lives} left."

    def summary(self) -> str:
        if self.end_reason == "stopped":
            self.out_of = max(0, self.number - (0 if self.result and self.result["id"] == self.number else 1))
            if self.out_of == 0:
                return f"Okay, we'll stop. The word was {self.word.capitalize()}."
            return (f"Okay, we'll stop. The word was {self.word.capitalize()}. You solved {self.score} of "
                    f"{plural(self.out_of, 'word')}.{self.best_line()}")
        return f"That's the round! You solved {self.score} of {plural(self.out_of, 'word')}.{self.best_line()} " \
               "Want another round?"

    def finish(self, reason: str = "done") -> str:
        if not self.over and reason == "stopped":
            self.out_of = max(0, self.number - (0 if self.result and self.result["id"] == self.number else 1))
        return super().finish(reason)

    def record_detail(self) -> Any:
        return {"category": self.fixed or "mixed"}

    # ------------------------------------------------------------ for the AI and the screen
    def describe(self) -> str:
        if self.over:
            return f"Voice game: Hangman just ended: {self.score} of {self.out_of} words solved."
        shown = " ".join(c or "_" for c in self.pattern())
        tried = ", ".join(self.guessed + self.wrong_letters) or "none yet"
        return (f"Voice game on: Hangman ({self.category}), word {self.number} of {self.total}: {shown} "
                f"({len(self.word)} letters); tried: {tried}; {self.lives} of {self.max_lives} lives left; "
                f"{self.score} solved. Letters and whole-word guesses are judged automatically; never give the "
                "word away. If they say something else, answer in a few words and ask for a letter.")

    def card_spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "card": "word_game", "title": "Hangman", "game": self.id, "session": self.session,
            "category": self.category.title() if self.category != "around the house" else "Around the house",
            "pattern": self.pattern(), "guessed": list(self.guessed), "wrong": list(self.wrong_letters),
            "lives": self.lives, "max_lives": self.max_lives, "number": self.number, "total": self.total,
            "score": self.score, "over": self.over, "result": dict(self.result) if self.result else None,
            "last": dict(self.last) if self.last else None,
        }
        return spec

    def status(self) -> dict[str, Any]:
        return {**super().status(), "number": self.number, "total": self.total, "lives": self.lives,
                "letters": len(self.word), "category": self.category}


__all__ = ["Hangman", "LIVES", "WORDS", "letter_name", "pick_category"]
