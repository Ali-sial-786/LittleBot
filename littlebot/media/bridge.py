"""Shared state between the MediaController and the player web page.

The page polls ``desired()`` (~2 Hz) and applies any new command; it reports what the
YouTube player actually does as events. Every load of an item gets a fresh ``load_id``
so late events from a previous item (e.g. an ``ended`` arriving after a skip) are ignored.

Access is protected by a random per-process token (``secrets.token_urlsafe``) compared in
constant time; the dashboard mounts the player routes without parent authentication.
All methods are thread-safe.
"""

from __future__ import annotations

import hmac
import itertools
import secrets
import threading
import time
from typing import Any, Callable

YOUTUBE_HOSTS = {
    "nocookie": "https://www.youtube-nocookie.com",
    "standard": "https://www.youtube.com",
}

POLL_MS = 500
HEARTBEAT_MS = 2000


class PlayerBridge:
    def __init__(
        self,
        *,
        token: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        connected_window: float = 6.0,
        youtube_host: str = "nocookie",
    ) -> None:
        self.token = token or secrets.token_urlsafe(32)
        self._clock = clock
        self._connected_window = connected_window
        self._lock = threading.Lock()
        self._seq = 0
        self._loads = itertools.count(1)
        self._nonce = secrets.token_hex(3)
        self._action = "idle"  # idle | play | pause | stop
        self._reason: str | None = None
        self._load_id: str | None = None
        self._item: dict[str, Any] | None = None
        self._volume = 0.5
        self._host = YOUTUBE_HOSTS.get(youtube_host, YOUTUBE_HOSTS["nocookie"])
        self._last_seen: float | None = None
        self._last_event: dict[str, Any] | None = None
        self._player: dict[str, Any] = {}

    # -- auth ---------------------------------------------------------------
    def check_token(self, candidate: str | None) -> bool:
        if not candidate or not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate.encode("utf-8", "ignore"), self.token.encode("utf-8"))

    # -- commands from the controller --------------------------------------
    def load(self, item: dict[str, Any]) -> str:
        """Ask the page to load and play ``item``; returns the new load id."""
        with self._lock:
            self._load_id = f"{next(self._loads)}-{self._nonce}"
            self._item = {
                "id": item["id"], "kind": item["kind"], "video_id": item.get("youtube_id"),
                "list_id": item.get("list_id"), "start_seconds": int(item.get("start_seconds") or 0),
                "title": item.get("title", ""),
            }
            self._action, self._reason = "play", None
            self._player = {}
            self._seq += 1
            return self._load_id

    def command(self, action: str, reason: str | None = None) -> None:
        if action not in ("play", "pause", "stop", "idle"):
            raise ValueError(f"unknown player action {action!r}")
        with self._lock:
            self._action, self._reason = action, reason
            if action in ("stop", "idle"):
                self._item = None
            self._seq += 1

    def set_volume(self, volume: float) -> None:
        with self._lock:
            if volume != self._volume:
                self._volume = volume
                self._seq += 1

    # -- page side ----------------------------------------------------------
    def desired(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seq": self._seq, "action": self._action, "reason": self._reason, "load_id": self._load_id,
                "item": dict(self._item) if self._item else None, "volume": self._volume,
                "youtube_host": self._host, "poll_ms": POLL_MS, "heartbeat_ms": HEARTBEAT_MS,
            }

    def touch(self) -> None:
        with self._lock:
            self._last_seen = self._clock()

    def record_event(self, event: dict[str, Any]) -> bool:
        """Store an event from the page. Returns True if it belongs to the current load."""
        with self._lock:
            self._last_seen = self._clock()
            current = bool(event.get("load_id")) and event.get("load_id") == self._load_id
            if event.get("type") != "heartbeat":
                self._last_event = {k: v for k, v in event.items() if v is not None}
                self._last_event["current"] = current
            if current:
                for key in ("player_state", "current_time", "duration", "video_id"):
                    if event.get(key) is not None:
                        self._player[key] = event[key]
            return current

    # -- introspection ------------------------------------------------------
    @property
    def load_id(self) -> str | None:
        with self._lock:
            return self._load_id

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._last_seen is not None and self._clock() - self._last_seen <= self._connected_window

    def seconds_since_seen(self) -> float | None:
        with self._lock:
            return None if self._last_seen is None else self._clock() - self._last_seen

    def status(self) -> dict[str, Any]:
        connected = self.connected
        with self._lock:
            return {"connected": connected, "last_event": dict(self._last_event) if self._last_event else None,
                    "player": dict(self._player), "action": self._action, "seq": self._seq}
