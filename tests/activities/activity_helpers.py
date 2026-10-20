"""Helpers for activity tests: engines from inline YAML, a shared call timeline, auto-answering prompts."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from littlebot.activities.engine import ActivityEngine
from littlebot.activities.model import ActivityLibrary, load_directories
from littlebot.core.events import Topics
from littlebot.core.paths import ACTIVITY_SCRIPTS_DIR
from littlebot.testing import FakeAudio, FakeFace, make_test_context

HEADER = """
id: {id}
name: Test {id}
description: A test activity.
min_age_months: {min_age}
max_age_months: {max_age}
category: {category}
parent_instructions: Play along.
safety_notes: [Supervise.]
duration_seconds: 60
pausable: {pausable}
"""


def activity_yaml(steps: str, *, id: str = "test_act", min_age: int = 6, max_age: int = 24, category: str = "social",
                  pausable: bool = True, extra: str = "") -> str:
    """Build one activity's YAML from an indented ``steps:`` block."""
    head = HEADER.format(id=id, min_age=min_age, max_age=max_age, category=category,
                         pausable=str(pausable).lower())
    return head + extra + "steps:\n" + steps


def make_engine(ctx: Any, *yamls: str, seed: int = 1) -> ActivityEngine:
    """Engine over inline activities (joined into one ``activities:`` list)."""
    if len(yamls) == 1:
        lib = ActivityLibrary.from_yaml(yamls[0], shared_data=shared_data())
    else:
        items = []
        for y in yamls:
            items.append("  - " + "\n    ".join(y.strip("\n").splitlines()))
        lib = ActivityLibrary.from_yaml("activities:\n" + "\n".join(items), shared_data=shared_data())
    engine = ActivityEngine(ctx, lib, seed=seed)
    ctx.activities = engine
    return engine


_SHARED: dict[str, Any] | None = None


def shared_data() -> dict[str, Any]:
    global _SHARED
    if _SHARED is None:
        _SHARED = load_directories([ACTIVITY_SCRIPTS_DIR / "_data.yaml"]).shared_data
    return _SHARED


class Timeline:
    """One ordered log across face + audio fakes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


class LoggingFace(FakeFace):
    def __init__(self, bus: Any, timeline: Timeline) -> None:
        super().__init__(bus)
        self.timeline = timeline

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        super()._rec(name, *args, **kwargs)
        self.timeline.events.append((f"face.{name}", args[0] if args else None))


class LoggingAudio(FakeAudio):
    def __init__(self, bus: Any, timeline: Timeline) -> None:
        super().__init__(bus)
        self.timeline = timeline

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        super()._rec(name, *args, **kwargs)
        self.timeline.events.append((f"audio.{name}", args[0] if args else None))






def auto_answer(ctx: Any, choose: Callable[[list[str]], str | None]) -> list[dict[str, Any]]:
    """Answer every caregiver prompt synchronously with ``choose(options)`` (None = don't answer)."""
    seen: list[dict[str, Any]] = []

    def on_prompt(event: Any) -> None:
        seen.append(dict(event.data))
        answer = choose(list(event.get("options") or []))
        if answer is not None:
            ctx.activities.respond(answer, event.get("prompt_id"))

    ctx.bus.subscribe(Topics.ACTIVITY_PROMPT, on_prompt)
    return seen


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()
