"""Shared state between the MediaController and the player web page.

The page polls ``desired()`` (~2 Hz) and applies any new command; it reports what the
YouTube player actually does as events. Every load of an item gets a fresh ``load_id``
so late events from a previous item (e.g. an ``ended`` arriving after a skip) are ignored.

Access is protected by a random per-process token (``secrets.token_urlsafe``) compared in
constant time; the dashboard mounts the player routes without parent authentication.
All methods are thread-safe.

Several pages may poll at once (e.g. a desktop browser tab left open after a stop, plus a new
one). Each page sends a random ``page_id``: the first page that polls a new load **while it is
visible** claims it (``owner``); only the owner creates a player, and events of a claimed load
from any other page (or without a page id) are recorded but ignored. Polls without a page id
never claim (tests, older pages); while a load is unclaimed every event for it counts.
"""

from __future__ import annotations

import hmac
import itertools
import re
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
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# How long the page lets a video play while it can't confirm it yet (no video id, or a playlist's
# video list not loaded yet); then it stops with ``unexpected_video``.
VERIFY_GRACE_MS = 4000


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
        self._last_visible: float | None = None  # last poll from a visible page (or one without a page id)
        self._owner: str | None = None  # page_id that claimed the current load
        self._owner_seen: float | None = None
        self._last_event: dict[str, Any] | None = None
        self._player: dict[str, Any] = {}
        self._player_at: float | None = None  # when the page last reported on the current load's player
        self.verify_grace_ms = VERIFY_GRACE_MS

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
            if item.get("presentation") == "music":  # the page's now-playing screen (otherwise a plain video)
                self._item["presentation"] = "music"
                self._item["channel"] = str(item.get("channel") or "")[:80]
            self._action, self._reason = "play", None
            self._player, self._player_at = {}, None
            self._owner = None  # a new load is claimed afresh
            self._owner_seen = None
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
    def desired(self, page_id: str | None = None, visible: bool = True) -> dict[str, Any]:
        """Desired state for a polling page. A visible page with a ``page_id`` claims an unclaimed
        load that is to play (this bumps ``seq`` so every page re-reads the state)."""
        page_id = valid_page_id(page_id)
        with self._lock:
            if page_id is not None:
                if (visible and self._owner is None and self._load_id is not None and self._item is not None
                        and self._action in ("play", "pause")):
                    self._owner = page_id
                    self._seq += 1
                if page_id == self._owner:
                    self._owner_seen = self._clock()
            return {
                "seq": self._seq, "action": self._action, "reason": self._reason, "load_id": self._load_id,
                "item": dict(self._item) if self._item else None, "volume": self._volume,
                "youtube_host": self._host, "poll_ms": POLL_MS, "heartbeat_ms": HEARTBEAT_MS,
                "verify_grace_ms": self.verify_grace_ms, "owner": self._owner,
            }

    def touch(self, visible: bool = True) -> None:
        with self._lock:
            self._last_seen = self._clock()
            if visible:
                self._last_visible = self._last_seen

    def record_event(self, event: dict[str, Any]) -> bool:
        """Store an event from the page. Returns True if it belongs to the current load (and, once the
        load is claimed, comes from the page that claimed it)."""
        with self._lock:
            self._last_seen = self._clock()
            current = bool(event.get("load_id")) and event.get("load_id") == self._load_id
            if current and self._owner is not None:
                current = event.get("page_id") == self._owner
                if current:
                    self._owner_seen = self._last_seen
            if event.get("type") != "heartbeat":
                # (a playlist's id list is checked by the controller; no need to keep it here)
                self._last_event = {k: v for k, v in event.items() if v is not None and k != "playlist_ids"}
                self._last_event["current"] = current
            if current:
                for key in ("player_state", "current_time", "duration", "video_id"):
                    if event.get(key) is not None:
                        self._player[key] = event[key]
                if event.get("player_state") is not None or event.get("current_time") is not None:
                    self._player_at = self._last_seen
            return current

    # -- introspection ------------------------------------------------------
    @property
    def load_id(self) -> str | None:
        with self._lock:
            return self._load_id

    @property
    def owner(self) -> str | None:
        with self._lock:
            return self._owner

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._last_seen is not None and self._clock() - self._last_seen <= self._connected_window

    @property
    def visible_connected(self) -> bool:
        """A page that could play right now is polling (hidden background tabs don't count)."""
        with self._lock:
            return self._last_visible is not None and self._clock() - self._last_visible <= self._connected_window

    def player_report(self) -> tuple[dict[str, Any], float | None]:
        """What the page last reported about the current load's player (``player_state``, ``current_time``,
        ``duration``, ``video_id``) and when (clock time; None before any report)."""
        with self._lock:
            return dict(self._player), self._player_at

    def seconds_since_seen(self) -> float | None:
        """Since the page playing the current load (its owner, once claimed) was last heard from."""
        with self._lock:
            seen = self._owner_seen if self._owner is not None else self._last_seen
            return None if seen is None else self._clock() - seen

    def status(self) -> dict[str, Any]:
        connected = self.connected
        with self._lock:
            return {"connected": connected, "last_event": dict(self._last_event) if self._last_event else None,
                    "player": dict(self._player), "action": self._action, "seq": self._seq}


def valid_page_id(value: object) -> str | None:
    """``value`` if it is a well-formed page id, else None (treated as "no page id")."""
    return value if isinstance(value, str) and PAGE_ID_RE.match(value) else None
