"""Would You Rather: two fun choices, the person picks, LittleBot reacts with its own pick.

Pairs come from assets/games/would_you_rather.json (45+ family-friendly ones); a round is :data:`QUESTIONS`
(``voice_games.would_you_rather_per_round``). The pick is understood locally ("fly", "the first one", "B",
"being invisible"); "both" or "neither" gets a playful "you have to pick one!". Every pick is added to a tally
kept on the robot (``voice_game_votes``), so LittleBot can say "most people here pick flying, 3 to 1". The card
shows the two choices (tap one to pick it) and the tally. No score is kept: there are no wrong answers.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from littlebot.games.voice import answers, data
from littlebot.games.voice.base import Reply, VoiceGame, sentence

QUESTIONS = 5
LIKE = ("Ooh, good pick!", "Nice choice!", "Ha, love it!", "Great answer!", "Interesting!")
SAME = ("Me too!", "Same here!", "Great minds think alike!")
_BOTH = re.compile(r"^(?:both|both of them|i want both|why not both|either|either one|neither|neither of them|none|"
                   r"i dont know|i cant choose|i cant decide|thats hard|tough one|hmm+|hard to say)\b")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


class WouldYouRather(VoiceGame):
    id: ClassVar[str] = "would_you_rather"
    title: ClassVar[str] = "Would You Rather"
    card_kind: ClassVar[str] = "game_board"
    records_scores: ClassVar[bool] = False

    def __init__(self, *, questions: int = QUESTIONS, **kw: Any) -> None:
        super().__init__(**kw)
        self.total = max(1, min(20, int(questions or QUESTIONS)))
        self.queue: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.number = 0
        self.picked: int | None = None  # what they picked for the last question (the card highlights it)
        self.tally: list[int] = [0, 0]
        self.agreed = 0  # how often LittleBot picked the same
        self.result: dict[str, Any] | None = None  # the last pick, for the card's flash

    def _load(self) -> list[dict[str, Any]]:
        out = []
        for p in data.items("would_you_rather", "pairs"):
            a, b = str(p.get("a") or "").strip(), str(p.get("b") or "").strip()
            if not a or not b:
                continue
            pick = p.get("pick")
            out.append({"a": a, "b": b, "a_short": str(p.get("a_short") or a), "b_short": str(p.get("b_short") or b),
                        "pick": pick if pick in (0, 1) else 0, "why": str(p.get("why") or ""),
                        "key": data.key_of(a, b)})
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
        pool.sort(key=lambda p: p["key"] in seen)
        self.queue = pool[: self.total]
        self.total = len(self.queue)
        if not self._advance():
            self.state = "over"
            return Reply("Sorry, I can't find my questions right now.", done=True)
        return Reply(f"Would you rather! No wrong answers. {self._ask()}", sound="notes")

    def _advance(self) -> bool:
        if not self.queue:
            self.current = None
            return False
        self.current = self.queue.pop(0)
        self.number += 1
        self.picked = None
        self.tally = self._tally(self.current)
        if self.store is not None:
            try:
                self.store.mark_seen(self.id, [(self.current["key"], self.current["a"])])
            except Exception:  # noqa: BLE001
                pass
        return True

    def _tally(self, p: dict[str, Any]) -> list[int]:
        if self.store is None:
            return [0, 0]
        try:
            return self.store.tally(p["key"])
        except Exception:  # noqa: BLE001
            return [0, 0]

    def _ask(self) -> str:
        p = self.current
        if p is None:
            return ""
        return f"Would you rather {p['a']}, or {p['b']}?"

    def _choices(self, p: dict[str, Any]) -> list[str]:
        return [p["a"], p["b"]]

    def _pick_of(self, text: str) -> int | None:
        p = self.current
        if p is None:
            return None
        for options in (self._choices(p), [p["a_short"], p["b_short"]]):
            i = answers.match_choice(text, options)
            if i is not None:
                return i
        return None

    def move(self, text: str) -> Reply | None:
        p = self.current
        if p is None:
            return None
        i = self._pick_of(text)
        if i is None:
            if _BOTH.match(answers.normalize(text)):
                return Reply(f"Ha, you have to pick one! {_cap(p['a_short'])}, or {p['b_short']}?")
            return None
        return self._picked(i)

    def tap(self, index: int) -> Reply | None:
        if self.over or self.current is None or index not in (0, 1):
            return None
        return self._out(self._picked(index))

    def _picked(self, i: int) -> Reply:
        p = self.current
        assert p is not None
        self.turns += 1
        self.picked = i
        mine = p["pick"]
        if self.store is not None:
            try:
                self.tally = self.store.vote(p["key"], i)
            except Exception:  # noqa: BLE001
                self.tally[i] += 1
        else:
            self.tally[i] += 1
        names = [p["a_short"], p["b_short"]]
        if mine == i:
            self.agreed += 1
            said = f"{_cap(names[i])}! {self.rng.choice(SAME)} {sentence(p['why'])}"
        else:
            said = f"{_cap(names[i])}! {self.rng.choice(LIKE)} I'd pick {names[mine]}. {sentence(p['why'])}"
        total = sum(self.tally)
        if total >= 3:
            a, b = self.tally
            if a == b:
                said += f" It's a tie here, {a} to {b}."
            else:
                top = 0 if a > b else 1
                said += f" Most people here pick {names[top]}, {max(a, b)} to {min(a, b)}."
        self.out_of = self.number
        self.score = self.agreed
        self.result = {"id": self.number, "options": [_cap(p["a"]), _cap(p["b"])], "picked": i,
                       "tally": list(self.tally), "mine": mine}
        if self._advance():
            label = "Last one" if self.number == self.total else "Next"
            return Reply(f"{said} {label}: {self._ask()}", sound="pop")
        end = self.finish("done")
        return Reply(f"{said} {end}", done=True, sound="tada")

    def hint(self) -> Reply:
        p = self.current
        if p is None:
            return self._out(Reply("No question right now."), remember=False)
        return self._out(Reply(f"No wrong answers here! Just pick one: {p['a_short']}, or {p['b_short']}?"),
                         remember=False)

    def skip(self) -> Reply:
        if self.current is None:
            return self._out(Reply("No question right now."), remember=False)
        self.picked = None
        if self._advance():
            return self._out(Reply(f"Okay, a different one. {self._ask()}"))
        return self._out(Reply(self.finish("done"), done=True))

    def repeat(self) -> str:
        return self._ask() if self.current is not None else super().repeat()

    def score_line(self) -> str:
        return f"We've agreed on {self.agreed} of {max(0, self.number - 1)} so far!" if self.number > 1 else \
            "No scores in this one, just fun!"

    def summary(self) -> str:
        if self.end_reason == "stopped" and self.turns == 0:
            return "Okay, maybe later!"
        if self.turns and self.agreed * 2 >= self.turns:
            mood = f"We agreed on {self.agreed} of {self.turns}. Great minds!"
        elif self.turns:
            mood = f"We agreed on {self.agreed} of {self.turns}. You're full of surprises!"
        else:
            mood = ""
        tail = "" if self.end_reason == "stopped" else " Want more?"
        return f"That was fun! {mood}{tail}".replace("  ", " ").strip()

    def describe(self) -> str:
        if self.over:
            return f"Voice game: Would You Rather just ended ({self.turns} answered)."
        p = self.current
        if p is None:
            return "Voice game on: Would You Rather."
        return (f"Voice game on: Would You Rather, question {self.number} of {self.total}: would you rather "
                f"{p['a']}, or {p['b']}? Their pick is understood automatically; if they say something else, "
                "chat in a few words and ask which one they'd pick.")

    def card_spec(self) -> dict[str, Any]:
        p = self.current
        status = ""
        if p is not None:
            a, b = self.tally
            if a + b:
                status = f"Picked here: {_cap(p['a_short'])} {a}  ·  {_cap(p['b_short'])} {b}"
            else:
                status = "Tap one, or say it!"
        return {"card": "game_board", "title": "Would you rather...", "game": self.id, "session": self.session,
                "text": "", "options": [_cap(p["a"]), _cap(p["b"])] if p is not None else [],
                "status": status if not self.over else f"We agreed on {self.agreed} of {self.turns}!",
                "picked": self.picked, "tally": list(self.tally), "number": self.number, "total": self.total,
                "over": self.over, "result": dict(self.result) if self.result else None}

    def status(self) -> dict[str, Any]:
        return {**super().status(), "number": self.number, "total": self.total, "agreed": self.agreed}


__all__ = ["QUESTIONS", "WouldYouRather"]
