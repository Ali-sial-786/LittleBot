"""Central application state machine.

There is exactly one top-level RobotState. Behaviours (activities, media,
conversation) move it through validated transitions; the face, dashboard and
other modules react to ``state.changed`` events rather than polling.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from littlebot.core.events import EventBus, Topics

log = logging.getLogger(__name__)


class RobotState(str, enum.Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    PLAYING_ACTIVITY = "PLAYING_ACTIVITY"
    PLAYING_MEDIA = "PLAYING_MEDIA"
    SLEEPING = "SLEEPING"
    ERROR = "ERROR"


S = RobotState

# Allowed transitions (to IDLE and to ERROR are always allowed).
_TRANSITIONS: dict[RobotState, set[RobotState]] = {
    S.IDLE: {S.LISTENING, S.THINKING, S.SPEAKING, S.PLAYING_ACTIVITY, S.PLAYING_MEDIA, S.SLEEPING},
    S.LISTENING: {S.THINKING, S.SPEAKING, S.PLAYING_ACTIVITY, S.PLAYING_MEDIA, S.SLEEPING},
    S.THINKING: {S.SPEAKING, S.LISTENING, S.PLAYING_ACTIVITY, S.PLAYING_MEDIA},
    S.SPEAKING: {S.LISTENING, S.THINKING, S.PLAYING_ACTIVITY, S.PLAYING_MEDIA, S.SLEEPING},
    S.PLAYING_ACTIVITY: {S.PLAYING_MEDIA, S.SLEEPING, S.LISTENING},
    S.PLAYING_MEDIA: {S.PLAYING_ACTIVITY, S.SLEEPING},
    S.SLEEPING: {S.LISTENING, S.PLAYING_ACTIVITY, S.PLAYING_MEDIA},
    S.ERROR: {S.SLEEPING},
}

# Face "state" the animated face should adopt for each robot state.
FACE_STATE_FOR: dict[RobotState, str] = {
    S.IDLE: "idle",
    S.LISTENING: "listening",
    S.THINKING: "thinking",
    S.SPEAKING: "speaking",
    S.PLAYING_ACTIVITY: "idle",  # activities drive expressions explicitly
    S.PLAYING_MEDIA: "hidden",  # video owns the screen
    S.SLEEPING: "sleeping",
    S.ERROR: "error",
}


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class StateRecord:
    state: RobotState
    since: float
    reason: str
    detail: dict[str, Any]


class StateManager:
    def __init__(self, bus: EventBus, history: int = 50):
        self._bus = bus
        self._lock = threading.RLock()
        self._state = RobotState.IDLE
        self._since = time.monotonic()
        self._reason = "startup"
        self._detail: dict[str, Any] = {}
        self._history: list[StateRecord] = []
        self._history_max = history
        self.last_error: str | None = None

    @property
    def state(self) -> RobotState:
        return self._state

    def is_(self, *states: RobotState) -> bool:
        return self._state in states

    def can_transition(self, new: RobotState) -> bool:
        cur = self._state
        return new == cur or new in (S.IDLE, S.ERROR) or new in _TRANSITIONS.get(cur, set())

    def transition(self, new: RobotState | str, reason: str = "", *, force: bool = False, **detail: Any) -> bool:
        """Move to ``new``. Returns False (and logs) if the transition is not allowed.

        ``force=True`` is reserved for shutdown / emergency paths.
        """
        new = RobotState(new)
        with self._lock:
            old = self._state
            if new == old and not detail:
                return True
            if not force and not self.can_transition(new):
                log.warning("rejected state transition %s -> %s (%s)", old.value, new.value, reason)
                return False
            self._history.append(StateRecord(old, self._since, self._reason, self._detail))
            del self._history[: -self._history_max]
            self._state, self._since, self._reason, self._detail = new, time.monotonic(), reason, dict(detail)
            if new == S.ERROR:
                self.last_error = reason
        log.info("state %s -> %s (%s)", old.value, new.value, reason)
        self._bus.publish(
            Topics.STATE_CHANGED,
            {"old": old.value, "new": new.value, "reason": reason, "face_state": FACE_STATE_FOR[new], **detail},
            source="state",
        )
        return True

    def to_idle(self, reason: str = "") -> bool:
        return self.transition(S.IDLE, reason)

    def release(self, expected: RobotState, reason: str = "") -> bool:
        """Return to IDLE only if we are still in ``expected`` (avoids clobbering a newer state)."""
        with self._lock:
            if self._state != expected:
                return False
            return self.transition(S.IDLE, reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "since_seconds": round(time.monotonic() - self._since, 1),
                "reason": self._reason,
                "face_state": FACE_STATE_FOR[self._state],
                "last_error": self.last_error,
            }

    def history(self) -> list[StateRecord]:
        with self._lock:
            return list(self._history)
