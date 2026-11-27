"""Driving by voice: "go forward 50 centimetres", "back up a bit", "turn left", "turn around", "stop".

The wheels are ``ctx.motion.drive`` (littlebot/motion/drive.py): speed limits, ramps, a watchdog and the
emergency stop live there, and real wheels move only after a parent arms motion on the dashboard. This skill
only checks, starts the move in the background (the answer is spoken while LittleBot drives) and says why when
it can't.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from littlebot.skills.base import Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.wheels")

MAX_DISTANCE_CM = 200.0  # one spoken move never goes further than this
MAX_TURN_DEG = 360.0
DEFAULT_DISTANCE_CM = 30.0  # "go forward" with no distance
DEFAULT_TURN_DEG = 90.0  # "turn left" with no angle

# Why a move can't start, in words for the voice (Drive's refusal reasons).
REFUSED = (
    ("not armed", "my wheels are locked: a grown-up unlocks them on the dashboard (Motion page, arm the motors)."),
    ("emergency stop", "the emergency stop is on: a grown-up resets it on the dashboard."),
    ("disabled", "my wheels are switched off in the settings (motion.drive.enabled)."),
    ("physical motion is switched off", "my wheels are switched off in the settings (motion.physical_enabled)."),
)


def _words(reason: str) -> str:
    low = reason.lower()
    for key, words in REFUSED:
        if key in low:
            return words
    return f"I can't move right now ({reason})."


class Wheels(Skill):
    name = "wheels"

    def __init__(self, ctx: Any, **kwargs: Any) -> None:
        super().__init__(ctx, **kwargs)
        self._task: asyncio.Task[Any] | None = None

    def _drive(self) -> Any:
        motion = getattr(self.ctx, "motion", None)
        return getattr(motion, "drive", None) if motion is not None else None

    def available(self) -> bool:
        drive = self._drive()
        try:
            return drive is not None and bool(drive.cfg.enabled)
        except Exception:  # noqa: BLE001
            return False

    def tools(self) -> list[ToolSpec]:
        return [
            tool("drive_distance", "Drive LittleBot straight forwards (or backwards) on its wheels. Only when someone "
                 "asks LittleBot to move, come closer, back up or drive.",
                 {"centimetres": {"type": "number", "description": "how far, 1-200 cm; leave out for a short move "
                                                                   "(30 cm). \"a bit\" is about 20"},
                  "direction": {"type": "string", "enum": ["forward", "backward"],
                                "description": "forward (default) or backward"}}),
            tool("turn", "Turn LittleBot on the spot, left or right (seen from LittleBot: its own left).",
                 {"degrees": {"type": "number", "description": "how far, 1-360; leave out for a quarter turn (90). "
                                                               "\"turn around\" is 180"},
                  "direction": {"type": "string", "enum": ["left", "right"]}},
                 ("direction",)),
            tool("stop_moving", "Stop LittleBot's wheels now."),
        ]

    def prompt_note(self) -> str:
        return ("Wheels: drive_distance to move forwards or back (\"come here\": about 50 cm; \"back up a bit\": 20 cm "
                "backward), turn to turn on the spot (left / right as LittleBot sees it, \"turn around\" 180), "
                "stop_moving when they say stop. Only move when someone asks. Answer in a few words (\"Here I "
                "come!\"); the move starts as you speak. If a tool says the wheels are locked, say so simply.")

    def context_note(self) -> str:
        drive = self._drive()
        if drive is None:
            return ""
        try:
            st = drive.status()
        except Exception:  # noqa: BLE001
            return ""
        return f"Wheels: moving ({st.get('command') or 'driving'})." if st.get("moving") else ""

    async def run(self, tool_name: str, args: dict[str, Any]) -> str:
        drive = self._drive()
        if drive is None:
            return err("LittleBot has no wheels.")
        if tool_name == "stop_moving":
            self._cancel()
            drive.stop()
            return ok("stopped")
        why = drive.refusal()
        if why:
            return err(_words(why))
        if tool_name == "drive_distance":
            cm = _number(args.get("centimetres"), DEFAULT_DISTANCE_CM, 1.0, MAX_DISTANCE_CM)
            back = str(args.get("direction") or "forward").lower().startswith("back")
            self._start(drive.forward((-cm if back else cm) / 100.0))
            return ok(f"driving {'backward' if back else 'forward'} {cm:g} cm")
        if tool_name == "turn":
            deg = _number(args.get("degrees"), DEFAULT_TURN_DEG, 1.0, MAX_TURN_DEG)
            left = str(args.get("direction") or "left").lower() != "right"
            self._start(drive.turn(deg if left else -deg))  # + = left (counter-clockwise seen from above)
            return ok(f"turning {'left' if left else 'right'} {deg:g} degrees")
        return err(f"wheels has no tool called {tool_name}")

    # ------------------------------------------------------------ helpers
    def _start(self, coro: Any) -> None:
        """The move runs in the background (a newer one replaces it: Drive takes the latest command)."""
        self._task = asyncio.get_running_loop().create_task(coro, name="wheels-skill")

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def stop(self) -> None:
        self._cancel()


def _number(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, abs(v)))
