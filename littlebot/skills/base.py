"""Skills: LittleBot's abilities that plug into whichever voice is talking.

A skill is a small module with tools for the text model (the new assistant's brain, littlebot/assistant, and
GPT-Live's hand-offs through ``chatgpt.tools.RobotToolbox``), a line or two for the system prompt, and
optionally a note about what it holds right now ("Shopping list: 3 items"). It never talks to another subsystem
except through ``ctx`` (the screen: ``ctx.screens.show``; speaking out of the blue: :func:`announce`).

Writing one::

    class Lists(Skill):
        name = "lists"
        def tools(self) -> list[ToolSpec]:
            return [tool("add_to_list", "Add items to a list ...", {"list": STR, "items": {...}}, ("items",))]
        async def run(self, tool: str, args: dict[str, Any]) -> str:
            ...
            return ok("added milk to the shopping list")
        def prompt_note(self) -> str:
            return "Lists: keep the user's lists with add_to_list / ..."

Results are short text for the model (``ok(...)``, ``err(...)``, or JSON from ``as_json``), never a secret, and a
tool never raises: :class:`SkillHub` turns a crash into an error result. Tool names are unique across skills.

Tests: ``make_test_context()`` gives an in-memory database; pass ``now=`` to a skill that needs the time.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

ToolSpec = dict[str, Any]  # {"name", "description", "parameters": JSON schema}

ANNOUNCE = "assistant.announce"  # {text, kind, card?: spec, urgent: bool} say this now (a reminder rang, ...)

STR: dict[str, Any] = {"type": "string"}
INT: dict[str, Any] = {"type": "integer"}
BOOL: dict[str, Any] = {"type": "boolean"}


def tool(name: str, description: str, props: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> ToolSpec:
    params: dict[str, Any] = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        params["required"] = list(required)
    return {"name": name, "description": description, "parameters": params}


def ok(text: str) -> str:
    return f"ok: {text}"


def err(text: str) -> str:
    return f"error: {text}"


def as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


class Skill:
    """Base class: override what the skill has. Every method is cheap except ``run`` (async)."""

    name = "skill"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        self.now = now  # wall-clock seconds (time.time); tests pass their own

    def available(self) -> bool:
        """False hides the skill's tools and notes (not set up, switched off)."""
        return True

    def tools(self) -> list[ToolSpec]:
        return []

    async def run(self, tool: str, args: dict[str, Any]) -> str:
        return err(f"{self.name} has no tool called {tool}")

    def prompt_note(self) -> str:
        """Fixed instructions for the system prompt (cached): when to use the tools. "" for none."""
        return ""

    def context_note(self) -> str:
        """What the skill holds right now, for this turn ("Timers: ...", "Next reminder: 6 pm call mom")."""
        return ""

    async def start(self) -> None:
        """Background work (a reminder clock). Runs on the robot's asyncio loop."""

    async def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available()}

    # ------------------------------------------------------------ helpers for skills
    def announce(self, text: str, *, kind: str | None = None, card: dict[str, Any] | None = None,
                 urgent: bool = False) -> None:
        """Say ``text`` now through whichever voice is on (or plain text-to-speech), with an optional card."""
        announce(self.ctx, text, kind=kind or self.name, card=card, urgent=urgent)

    async def show(self, spec: dict[str, Any]) -> bool:
        """Put a card on the screen (``spec["card"]`` is its kind). False when the screen can't show it."""
        screens = getattr(self.ctx, "screens", None)
        if screens is None:
            return False
        try:
            res = await screens.show(spec, source=self.name)
        except Exception:  # noqa: BLE001 - a card is a nice-to-have
            return False
        return bool(res.get("ok"))


def announce(ctx: Any, text: str, *, kind: str = "note", card: dict[str, Any] | None = None,
             urgent: bool = False) -> None:
    data: dict[str, Any] = {"text": str(text), "kind": kind, "urgent": bool(urgent)}
    if card:
        data["card"] = card
    ctx.bus.publish(ANNOUNCE, data, source=kind)


__all__ = ["ANNOUNCE", "BOOL", "INT", "STR", "Skill", "ToolSpec", "announce", "as_json", "err", "ok", "tool"]
