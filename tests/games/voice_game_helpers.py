"""Helpers for the voice games tests: a fake text model, model-written questions, a store on a test database."""

from __future__ import annotations

import asyncio
import random
from typing import Any

from littlebot.games.voice.scores import ScoreStore


class FakeModel:
    """Stands in for ClaudeProvider.ask_json: records the calls, answers ``data`` after ``delay`` s (or raises)."""

    def __init__(self, data: Any = None, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.data = data if data is not None else {"questions": model_questions(10)}
        self.delay = delay
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def missing(self) -> str | None:
        return None

    async def ask_json(self, *, system: str, user: str, schema: dict[str, Any], timeout: float,
                       max_tokens: int = 1024) -> Any:
        self.calls.append({"system": system, "user": user, "schema": schema, "timeout": timeout,
                           "max_tokens": max_tokens})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.data


def model_questions(n: int, *, start: int = 0) -> list[dict[str, Any]]:
    """``n`` well-formed model questions about made-up dinosaurs (never in the offline bank). The right answer is
    always "Right N" at index 1."""
    return [{"question": f"Which dinosaur number {i} is the right one here?",
             "options": [f"Wrong {i}a", f"Right {i}", f"Wrong {i}b", f"Wrong {i}c"], "answer_index": 1,
             "fact": f"Dinosaur {i} is a made-up fact."} for i in range(start, start + n)]


def store(ctx: Any, t: float = 1_700_000_000.0) -> ScoreStore:
    clock = {"t": t}
    s = ScoreStore(ctx.db, now=lambda: clock["t"])
    s.clock = clock  # type: ignore[attr-defined]
    return s


def rng(seed: int = 1) -> random.Random:
    return random.Random(seed)


def right_letter(game: Any) -> str:
    """The letter of the current trivia question's right answer."""
    return "ABCD"[game.current.answer]


def wrong_letter(game: Any) -> str:
    return "ABCD"[(game.current.answer + 1) % 4]
