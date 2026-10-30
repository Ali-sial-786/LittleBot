"""Riddles: a round of :data:`RIDDLES` riddles (``voice_games.riddles_per_round``) from assets/games/riddles.json.

Any short answer is a guess ("a towel?", "is it a candle"), judged against the riddle's accepted answers
(littlebot/games/voice/answers.py ``matches``: forgiving about slips, articles and filler). A wrong guess gets
"try again"; the second brings the hint by itself; the third reveals the answer and moves on. "hint", "I give up",
"skip" work any time. One point per riddle solved, the hint costs nothing (it's for fun).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from littlebot.games.voice import answers, data
from littlebot.games.voice.base import Reply, VoiceGame, plural, sentence

RIDDLES = 5
MAX_TRIES = 3
MAX_GUESS_WORDS = 7  # longer than this (after the filler) is chat, not an answer
# Questions and remarks that are not answers: the AI gets them ("what do you mean?", "that's a tough one").
_NOT_ANSWER = re.compile(
    r"^(?:what|why|how|who|where|when|can you|could you|do you|did you|will you|would you|are you|tell me more|"
    r"hmm+|um+|uh+|let me think|wait|hold on|thats (?:hard|tough|easy|a tough one|a hard one|funny|clever|good)|"
    r"this is (?:hard|tough|easy|fun)|i like (?:it|this|that)|good one|oh|ok|okay|yes|yeah|no|nope|cool|nice|wow|"
    r"haha|ha ha|lol)\b")
_ALLOWED_QUESTION = re.compile(
    r"^(?:is it|is that|could it be|would it be|is the answer|are they|is she|is he|was it)\b")
NICE = ("Yes!", "You got it!", "Correct!", "Clever!", "That's it!")


class Riddles(VoiceGame):
    id: ClassVar[str] = "riddles"
    title: ClassVar[str] = "Riddles"
    card_kind: ClassVar[str] = "game_board"

    def __init__(self, *, riddles: int = RIDDLES, **kw: Any) -> None:
        super().__init__(**kw)
        self.total = max(1, min(20, int(riddles or RIDDLES)))
        self.queue: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.number = 0
        self.tries = 0
        self.hinted = False
        self.result: dict[str, Any] | None = None

    def _load(self) -> list[dict[str, Any]]:
        out = []
        for r in data.items("riddles", "riddles"):
            q, say, accept = str(r.get("q") or ""), str(r.get("say") or ""), r.get("accept")
            if q and say and isinstance(accept, list) and accept:
                out.append({"q": q, "say": say, "accept": [str(a) for a in accept] + [say],
                            "hint": str(r.get("hint") or ""), "key": data.key_of(q)})
        return out

    async def _begin(self) -> Reply:
        pool = self._load()
        seen: set[str] = set()
        if self.store is not None:
            try:
                seen = {k for k, _ in self.store.seen(self.id)}
            except Exception:  # noqa: BLE001
                seen = set()
        self.rng.shuffle(pool)
        pool.sort(key=lambda r: r["key"] in seen)  # fresh ones first
        self.queue = pool[: self.total]
        self.total = len(self.queue)
        if not self._advance():
            self.state = "over"
            return Reply("Sorry, I can't find my riddles right now.", done=True)
        n = f"{self.total} riddles" if self.total > 1 else "one riddle"
        return Reply(f"Riddle time! {n}. Here's the first: {self.current['q']}", sound="notes")

    def _advance(self) -> bool:
        self.tries, self.hinted = 0, False
        if not self.queue:
            self.current = None
            return False
        self.current = self.queue.pop(0)
        self.number += 1
        if self.store is not None:
            try:
                self.store.mark_seen(self.id, [(self.current["key"], self.current["q"])])
            except Exception:  # noqa: BLE001
                pass
        return True

    def _then(self, said: str, sound: str | None) -> Reply:
        self.out_of = self.number
        if self._advance():
            label = "Last one" if self.number == self.total else "Next one"
            return Reply(f"{said} {label}: {self.current['q']}", sound=sound)  # type: ignore[index]
        end = self.finish("done")
        return Reply(f"{said} {end}", done=True, sound="tada" if self.score else sound)

    def move(self, text: str) -> Reply | None:
        r = self.current
        if r is None:
            return None
        if answers.matches(text, r["accept"]):
            self.right()
            self.result = {"id": self.number, "answer": r["say"], "right": True}
            return self._then(f"{self.rng.choice(NICE)} {sentence(r['say'])}", "success")
        s = answers.normalize(text)
        if not s or (_NOT_ANSWER.match(s) and not _ALLOWED_QUESTION.match(s)):
            return None
        if len(answers.strip_filler(s).split()) > MAX_GUESS_WORDS:
            return None
        self.tries += 1
        if self.tries >= MAX_TRIES:
            self.wrong()
            self.result = {"id": self.number, "answer": r["say"], "right": False}
            return self._then(f"Good tries! The answer was: {sentence(r['say'])}", "uhoh")
        if self.tries == MAX_TRIES - 1 and r["hint"] and not self.hinted:
            self.hinted = True
            return Reply(f"Not quite. Here's a hint: {sentence(r['hint'])}")
        return Reply("Nope! Try again, or say hint.")

    def hint(self) -> Reply:
        r = self.current
        if r is None:
            return self._out(Reply("No riddle right now."), remember=False)
        self.hinted = True
        return self._out(Reply(f"Hint: {sentence(r['hint'] or 'think simple!')}"), remember=False)

    def skip(self) -> Reply:
        r = self.current
        if r is None:
            return self._out(Reply("No riddle right now."), remember=False)
        self.wrong()
        self.result = {"id": self.number, "answer": r["say"], "right": False}
        return self._out(self._then(f"The answer was: {sentence(r['say'])}", None))

    def repeat(self) -> str:
        return f"Riddle {self.number}: {self.current['q']}" if self.current else super().repeat()

    def score_line(self) -> str:
        return f"You've solved {self.score} of {self.number - 1} so far." if self.number > 1 else \
            "This is the first riddle!"

    def finish(self, reason: str = "done") -> str:
        if not self.over and reason == "stopped":
            self.out_of = max(0, self.number - 1)
        return super().finish(reason)

    def summary(self) -> str:
        if self.end_reason == "stopped":
            if self.current is not None:
                head = f"Okay! The answer to that one was: {sentence(self.current['say'])}"
            else:
                head = "Okay!"
            if self.out_of == 0:
                return head
            return f"{head} You solved {self.score} of {plural(self.out_of, 'riddle')}.{self.best_line()}"
        solved = f"You solved {self.score} of {plural(self.out_of, 'riddle')}.{self.best_line()}"
        return f"That's all my riddles! {solved} Want another round?"

    def describe(self) -> str:
        if self.over:
            return f"Voice game: Riddles just ended: {self.score} of {self.out_of} solved."
        r = self.current
        if r is None:
            return "Voice game on: Riddles."
        return (f"Voice game on: Riddles, riddle {self.number} of {self.total}, {self.score} solved. The riddle: "
                f"\"{r['q']}\" Their answers are judged automatically; never give the answer away (they can say "
                "hint or I give up). If they say something else, answer in a few words and steer back.")

    def card_spec(self) -> dict[str, Any]:
        r = self.current
        status = f"Score {self.score}  ·  say your answer, \"hint\" or \"give up\"" if not self.over else \
            f"You solved {self.score} of {self.out_of}!"
        return {"card": "game_board", "title": f"Riddle {self.number} of {self.total}" if not self.over else "Riddles",
                "game": self.id, "session": self.session, "text": r["q"] if r and not self.over else "",
                "options": [], "status": status, "result": dict(self.result) if self.result else None,
                "over": self.over, "score": self.score}

    def status(self) -> dict[str, Any]:
        return {**super().status(), "number": self.number, "total": self.total, "tries": self.tries}


__all__ = ["RIDDLES", "Riddles"]
