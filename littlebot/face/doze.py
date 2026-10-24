"""When LittleBot's face dozes: a small pure state machine (no threads, no clock of its own).

Dozing is only a *look* (droopy eyes that slowly close, a floating "zzz"): the robot stays IDLE and the
wake word, the camera, touch and the dashboard keep working. The deeper sleep (the SLEEPING state after
``system.sleep_after_idle_minutes``) is separate and unchanged.

The face is awake while something is happening and dozes once it has all been quiet for a while:

* ``begin(key)`` / ``end(key)``: something that lasts (a voice chat, LittleBot talking or playing a sound,
  a card, a game, an activity or a video on the screen, a busy robot state). The face never dozes while
  one is on; when it ends the quiet wait starts again. A hold can have a safety limit in case its end
  never arrives.
* ``activity()``: something just happened (the wake word, something said, a touch): awake now, and it
  dozes again ``doze_after`` seconds later unless more happens.
* ``arrival()``: someone new walked in (or someone moved): awake for at least ``awake_on_arrival``
  seconds. Deliberately *not* "for as long as they are there": a person who stays and keeps still
  lets it doze again.
* ``glance(seconds)``: the face was asked to show something (an expression, an animation, a look):
  awake at least while it plays.

Feed it events with a timestamp and call ``update(now)``; it returns ``("doze" | "wake", reason)`` when
the decision changes. Every method takes ``now`` so tests can use a fake clock. Not thread-safe on its own:
the face controller calls it under its lock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DOZE_AFTER_S = 30.0  # quiet for this long -> doze
AWAKE_ON_ARRIVAL_S = 20.0  # someone walked in -> awake for at least this long
MIN_DOZE_AFTER_S = 5.0
MAX_SECONDS = 3600.0  # settings are clamped into a sane range


@dataclass
class _Hold:
    reason: str
    until: float | None  # safety limit (None: until end())


def _seconds(value: object, default: float, lo: float) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return max(lo, min(MAX_SECONDS, v))


class DozeTimer:
    """Decides whether the face should be awake or dozing. See the module docstring."""

    def __init__(self, *, enabled: bool = True, doze_after: float = DOZE_AFTER_S,
                 awake_on_arrival: float = AWAKE_ON_ARRIVAL_S, now: float = 0.0) -> None:
        self.enabled = bool(enabled)
        self.doze_after = _seconds(doze_after, DOZE_AFTER_S, MIN_DOZE_AFTER_S)
        self.awake_on_arrival = _seconds(awake_on_arrival, AWAKE_ON_ARRIVAL_S, 0.0)
        self._last_activity = now  # starts awake: it dozes after the first quiet spell
        self._awake_until = now  # arrivals and glances
        self._holds: dict[str, _Hold] = {}
        self._reason = "starting up"
        self.dozing = False

    # ------------------------------------------------------------ settings
    def configure(self, *, enabled: bool | None = None, doze_after: float | None = None,
                  awake_on_arrival: float | None = None) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if doze_after is not None:
            self.doze_after = _seconds(doze_after, self.doze_after, MIN_DOZE_AFTER_S)
        if awake_on_arrival is not None:
            self.awake_on_arrival = _seconds(awake_on_arrival, self.awake_on_arrival, 0.0)

    # ------------------------------------------------------------ inputs
    def activity(self, now: float, reason: str) -> None:
        """Something just happened: awake, and the quiet wait starts again."""
        self._last_activity = max(self._last_activity, now)
        self._reason = reason

    def arrival(self, now: float, reason: str = "someone walked in") -> None:
        """Someone new is here: awake for at least ``awake_on_arrival`` seconds."""
        self.glance(now, self.awake_on_arrival, reason)

    def glance(self, now: float, seconds: float, reason: str) -> None:
        """Awake for at least ``seconds`` (without restarting the quiet wait)."""
        self._awake_until = max(self._awake_until, now + max(0.0, seconds))
        self._reason = reason

    def begin(self, key: str, now: float, reason: str, max_seconds: float | None = None) -> None:
        """Something that lasts started (``end(key)`` when it is over). Never dozes meanwhile."""
        self._holds[key] = _Hold(reason, None if max_seconds is None else now + max_seconds)
        self.activity(now, reason)

    def end(self, key: str, now: float) -> None:
        hold = self._holds.pop(key, None)
        if hold is not None:
            self._last_activity = max(self._last_activity, now)

    def clear(self, now: float) -> None:
        """Forget every hold (STOP ALL): whatever was on has been stopped."""
        if self._holds:
            self._holds.clear()
            self._last_activity = max(self._last_activity, now)

    # ------------------------------------------------------------ queries
    def holding(self, now: float) -> list[str]:
        """Keys of the holds that are still on (expired safety limits are dropped)."""
        for key, hold in list(self._holds.items()):
            if hold.until is not None and now >= hold.until:
                del self._holds[key]
                self._last_activity = max(self._last_activity, hold.until)
        return list(self._holds)

    def should_doze(self, now: float) -> bool:
        if not self.enabled or self.holding(now):
            return False
        return now >= max(self._last_activity + self.doze_after, self._awake_until)

    def quiet_for(self, now: float) -> float:
        return max(0.0, now - self._last_activity)

    def update(self, now: float) -> tuple[str, str] | None:
        """Re-decide; returns ``("doze", why)`` or ``("wake", why)`` when it changed, else None."""
        doze = self.should_doze(now)
        if doze == self.dozing:
            return None
        self.dozing = doze
        if doze:
            return "doze", f"nothing happening for {self.doze_after:.0f} s"
        if not self.enabled:
            return "wake", "dozing switched off"
        return "wake", self._reason

    def status(self, now: float) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "dozing": self.dozing,
            "doze_after": self.doze_after,
            "awake_on_arrival": self.awake_on_arrival,
            "holding": self.holding(now),
            "quiet_seconds": round(self.quiet_for(now), 1),
            "awake_for": round(max(0.0, max(self._last_activity + self.doze_after, self._awake_until) - now), 1),
        }
