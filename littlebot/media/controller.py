"""MediaController: parent-approved YouTube playback through the official IFrame Player API.

Safety rules enforced here (not just in the UI):

* Only items on the parent's allowlist can play; ``skip`` only moves within enabled items.
* Video is off by default under ``media.min_age_months_for_video`` (18 months) and for an
  unknown age; a parent may authorise it (``parent_override`` / ``media.allow_under_min_age``).
* Session and daily time limits (daily usage persisted per calendar day) with a gentle stop.
* Offline mode refuses playback; ``media.enabled`` can switch video off entirely.
* When a video ends, errors or is stopped, the player is torn down and the animated face returns
  (``face.visible`` true) - YouTube's end screen / related videos never take over.

Playback itself happens in a web page (``player_routes``) shown by a display backend
(``display``). The page polls the desired state from :class:`PlayerBridge` and reports
what the YouTube player does; this controller turns those reports into bus events.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.clock import Clock
from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.runtime import cancel_and_wait
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.media.allowlist import parse_youtube_url
from littlebot.media.bridge import PlayerBridge
from littlebot.media.display import DisplayBackend, PlayerBackendError, select_backend
from littlebot.media.oembed import OEmbedResult, lookup_oembed
from littlebot.media.repository import AllowlistRepository, UsageRepository

log = logging.getLogger("littlebot.media")

OWNER = "media"
RESOURCES = (Resource.SCREEN, Resource.AUDIO_OUT, Resource.FACE)
STOP_REASONS = ("ended", "stopped", "limit", "error")
MIN_REMAINING_SECONDS = 15.0  # don't start a video with less daily time than this left
PAGE_FADE_SECONDS = 1.5  # player.js fades the volume out this long on a limit stop (FADE_MS)
USAGE_FLUSH_SECONDS = 10.0
MAX_SESSION_LIMIT_MINUTES = 120
MAX_DAILY_LIMIT_MINUTES = 240

# YouTube IFrame API onError codes -> (short code, parent-facing message)
PLAYER_ERRORS: dict[Any, tuple[str, str]] = {
    2: ("invalid_parameter", "YouTube rejected the video id (error 2). Check the link."),
    5: ("html5_error", "The video couldn't be played by the browser on this device (YouTube error 5)."),
    100: ("not_found", "YouTube says this video was not found - it may have been removed or made private (error 100)."),
    # YouTube also uses 101/150 when it refuses playback for other reasons (region, age-restricted,
    # "content isn't available" for suspected automated traffic), so the message says "usually".
    101: ("embedding_disabled",
          "Unsupported video: YouTube won't play it in an embedded player (error 101 - usually the owner "
          "disabled playback on other sites). Choose a different video."),
    150: ("embedding_disabled",
          "Unsupported video: YouTube won't play it in an embedded player (error 150 - usually the owner "
          "disabled playback on other sites). Choose a different video."),
    153: ("missing_referrer", "YouTube refused the embedded player (error 153: player configuration / referrer)."),
    "api_load_failed": ("api_load_failed", "Couldn't load the YouTube player. Is LittleBot connected to the internet?"),
    "unexpected_video": ("unexpected_video",
                         "The player tried to show a video that is not on the approved list, so it was stopped."),
    "bad_item": ("bad_item", "The player received an invalid item."),
    "player_create_failed": ("player_create_failed", "The YouTube player could not be created."),
}

PLAYER_HIDDEN_MESSAGE = ("The video was stopped because the player was hidden or left "
                         "(another window or tab opened over it, or the page was closed).")

TitleLookup = Callable[[str], Awaitable[OEmbedResult]]


def describe_player_error(code: Any) -> tuple[str, str]:
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if code in PLAYER_ERRORS:
        return PLAYER_ERRORS[code]
    return ("unknown", f"The YouTube player reported an error ({code}).")


@dataclass
class _Session:
    id: int
    item: dict[str, Any]
    lease: Lease
    load_id: str
    started: float
    load_started: float
    last_accrual: float
    override: bool = False
    state: str = "loading"  # loading | playing | paused
    seconds: float = 0.0  # active (not paused) seconds in this session
    paused_at: float | None = None
    ever_connected: bool = False
    backend_opened: bool = False
    has_played: bool = False  # the player reported "playing" for the current load


class MediaController(Service):
    """Implements ``core.interfaces.MediaAPI`` (+ ``Service`` lifecycle)."""

    name = "media"

    def __init__(
        self,
        ctx: RobotContext,
        *,
        backend: DisplayBackend | None = None,
        clock: Clock | None = None,
        today: Callable[[], _dt.date] | None = None,
        tick_seconds: float | None = None,
        title_lookup: TitleLookup | None = None,
        token: str | None = None,
    ) -> None:
        super().__init__()  # lifecycle state lives in ``service_status``; ``status()`` is MediaAPI's
        self.ctx = ctx
        self.cfg = ctx.config.media
        self._clock = clock or ctx.clock
        self._today = today or _dt.date.today
        self._tick_seconds = max(0.001, float(tick_seconds if tick_seconds is not None else self.cfg.limit_check_seconds))
        self._title_lookup = title_lookup or lookup_oembed
        self.repo = AllowlistRepository(ctx.db, max_items=self.cfg.max_items)
        self.usage = UsageRepository(ctx.db)
        self.backend = backend or select_backend(ctx.config)
        self.bridge = PlayerBridge(token=token, clock=self._clock.now, youtube_host=self.cfg.youtube_host)
        self._lock = threading.RLock()  # guards session/usage fields read by status() from other threads
        self._op_lock = asyncio.Lock()  # serialises play/stop/skip/pause/resume/player events
        self._session: _Session | None = None
        self._session_ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._subs: list[Subscription] = []
        self._shutting_down = False
        self._usage_day: _dt.date | None = None
        self._usage_persisted = 0.0
        self._usage_pending = 0.0
        self._last_flush = self._clock.now()
        self.last_error: dict[str, Any] | None = None
        self._register_validators()
        self.bridge.set_volume(self._volume())

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings),
                bus.subscribe(Topics.SYSTEM_SHUTDOWN, self._on_shutdown),
            ]
        try:
            await asyncio.to_thread(self.usage.prune, self._today())
        except Exception:  # noqa: BLE001
            log.exception("could not prune media usage")
        if self.ctx.config.is_offline:
            self.set_status(ServiceStatus.DISABLED, "offline mode: YouTube needs the internet")
        elif not self.backend.available:
            self.set_status(ServiceStatus.DEGRADED, self.backend.detail)
        else:
            self.set_status(ServiceStatus.RUNNING, f"player: {self.backend.name}")

    async def stop(self, reason: str = "stopped") -> None:
        """End any playback (``MediaAPI.stop``). ``Service.stop`` shares this name: during app
        shutdown (after ``system.shutdown``) it also releases subscriptions, tasks and the browser."""
        self._bind_loop()
        async with self._op_lock:
            await self._end_session(reason)
        if self._shutting_down:
            await self.close()

    async def close(self) -> None:
        """Lifecycle teardown (idempotent): end playback, cancel tasks, close the browser window."""
        if self._session is not None:
            async with self._op_lock:
                await self._end_session("stopped", detail="shutdown")
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        await cancel_and_wait(self._watchdog)
        self._watchdog = None
        await cancel_and_wait(self._close_task)
        self._close_task = None
        for t in list(self._tasks):
            await cancel_and_wait(t)
        try:
            await self.backend.close()
        except Exception:  # noqa: BLE001
            log.exception("player backend close failed")
        with self._lock:
            self._flush_usage(force=True)
        self.set_status(ServiceStatus.STOPPED)

    # ================================================================ allowlist
    def list_items(self) -> list[dict[str, Any]]:
        return self.repo.list()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        return self.repo.get(item_id)

    def add_item(self, url: str, title: str | None = None, *, kind: str | None = None) -> dict[str, Any]:
        """Validate + add. ``kind="playlist"`` keeps the playlist of a ``watch?v=..&list=..`` link.
        Raises ValueError subclasses (MediaURLError, DuplicateItemError, AllowlistFullError)."""
        parsed = parse_youtube_url(url, kind=kind)
        item = self.repo.add(parsed, title)
        log.info("allowlist: added %s %s", item["kind"], item["id"])
        self._maybe_lookup_oembed(item)
        return item

    def remove_item(self, item_id: int) -> bool:
        removed = self.repo.remove(item_id)
        if removed:
            self._stop_if_playing_item(item_id, "removed from the approved list")
        return removed

    def update_item(self, item_id: int, *, title: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
        """Rename / enable / disable an item. Raises KeyError if unknown, ValueError for an empty title."""
        item = self.repo.update(item_id, title=title, enabled=enabled)
        if item is None:
            raise KeyError(f"unknown media item {item_id}")
        if enabled is False:
            self._stop_if_playing_item(item_id, "switched off in the approved list")
        return item

    def reorder_items(self, ids: list[int]) -> list[dict[str, Any]]:
        return self.repo.reorder(ids)

    # ================================================================ playback
    async def play(self, item_id: int, *, parent_override: bool = False) -> dict[str, Any]:
        self._bind_loop()
        async with self._op_lock:
            item = self.repo.get(item_id)
            if item is None:
                raise KeyError(f"unknown media item {item_id}")
            sess = self._session
            override = bool(parent_override or (sess is not None and sess.override))
            self._check_allowed(item, override, sess)
            if sess is None:
                holder = self.ctx.arbiter.holder(Resource.SCREEN)
                lease = self.ctx.arbiter.acquire(OWNER, RESOURCES, Priority.MEDIA, on_preempt=self._on_preempt)
                if lease is None:
                    who = holder.owner if holder is not None else "something more important"
                    raise PermissionError(f"LittleBot is busy right now ({who}). Try again in a moment.")
                if not self.ctx.state.transition(RobotState.PLAYING_MEDIA, f"media: {item['title']}"):
                    lease.release()
                    raise PermissionError(
                        f"LittleBot can't start a video right now (state {self.ctx.state.state.value}).")
                now = self._clock.now()
                sess = _Session(id=next(self._session_ids), item=item, lease=lease, load_id="", started=now,
                                load_started=now, last_accrual=now, override=bool(parent_override))
                with self._lock:
                    self._session = sess
                self.ctx.bus.publish(Topics.FACE_VISIBLE, {"visible": False}, source="media")
            else:
                with self._lock:
                    self._accrue()
            self._cancel_close_task()
            self._load(sess, item)
            if not self._player_alive():  # kiosk not running / desktop tab not polling
                try:
                    await self.backend.open(self.player_url())
                except PlayerBackendError as e:
                    msg = f"Couldn't open the video player on LittleBot's screen: {e}"
                    self._record_error(item, "backend", msg)
                    await self._end_session("error", detail=msg)
                    raise PermissionError(msg) from e
            if self.backend.is_running():
                sess.backend_opened = True  # from now on, the window disappearing is an error
            self.ctx.bus.publish(Topics.MEDIA_STARTED, {
                "item_id": item["id"], "title": item["title"], "kind": item["kind"], "session_id": sess.id,
                "skipped": False}, source="media")
            self._ensure_watchdog()
            log.info("media: playing item %s (session %s)", item["id"], sess.id)
        return self.status()

    async def pause(self) -> bool:
        self._bind_loop()
        async with self._op_lock:
            sess = self._session
            if sess is None or sess.state == "paused":
                return False
            with self._lock:
                self._accrue()
                sess.state = "paused"
                sess.paused_at = self._clock.now()
            self.bridge.command("pause")
            self.ctx.bus.publish(Topics.MEDIA_PAUSED, {"item_id": sess.item["id"]}, source="media")
            return True

    async def resume(self) -> bool:
        self._bind_loop()
        async with self._op_lock:
            sess = self._session
            if sess is None or sess.state != "paused":
                return False
            with self._lock:
                self._accrue()
            limit = self._limit_reason(sess)
            if limit:
                await self._end_session("limit", detail=limit)
                return False
            with self._lock:
                sess.state = "playing" if sess.has_played else "loading"
                sess.paused_at = None
                sess.last_accrual = self._clock.now()
            self.bridge.command("play")
            self.ctx.bus.publish(Topics.MEDIA_RESUMED, {"item_id": sess.item["id"]}, source="media")
            return True

    async def skip(self) -> dict[str, Any]:
        """Next enabled approved item (wrapping). Never anything that is not on the allowlist."""
        self._bind_loop()
        async with self._op_lock:
            sess = self._session
            if sess is None:
                return self.status()
            with self._lock:
                self._accrue()
            limit = self._limit_reason(sess)
            if limit:
                await self._end_session("limit", detail=limit)
                return self.status()
            nxt = self.repo.next_enabled(sess.item["id"])
            if nxt is None:
                await self._end_session("stopped", detail="no approved videos left")
                return self.status()
            self._load(sess, nxt)
            if not self._player_alive():
                try:
                    await self.backend.open(self.player_url())
                except PlayerBackendError as e:
                    await self._end_session("error", detail=str(e))
                    return self.status()
            if self.backend.is_running():
                sess.backend_opened = True
            self.ctx.bus.publish(Topics.MEDIA_STARTED, {
                "item_id": nxt["id"], "title": nxt["title"], "kind": nxt["kind"], "session_id": sess.id,
                "skipped": True}, source="media")
        return self.status()

    def set_volume(self, volume: float) -> float:
        value = self._clamp_volume(volume)
        self.ctx.settings.set("media.volume", value, source="media")
        self.bridge.set_volume(value)
        return value

    async def tick(self) -> None:
        """One watchdog pass (limits, pause timeout, player health). Public for tests."""
        async with self._op_lock:
            await self._tick()

    # ================================================================ player bridge
    def check_player_token(self, token: str | None) -> bool:
        return self.bridge.check_token(token)

    @property
    def player_token(self) -> str:
        return self.bridge.token

    def player_path(self) -> str:
        """Relative player link (with token), e.g. for an "open player on this device" button."""
        return f"/player?token={self.bridge.token}"

    def player_url(self) -> str:
        base = self.cfg.player_base_url or f"http://127.0.0.1:{self.ctx.config.dashboard.port}"
        return base.rstrip("/") + self.player_path()

    def player_state(self) -> dict[str, Any]:
        """Desired state for the player page (called by its 2 Hz poll; marks it connected)."""
        self.bridge.touch()
        sess = self._session
        if sess is not None and not sess.ever_connected:
            sess.ever_connected = True
        return self.bridge.desired()

    async def handle_player_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Event from the player page (any thread / loop); processed on the controller's loop."""
        return await self._on_controller_loop(self._handle_player_event(dict(event)))

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        with self._lock:
            sess = self._session
            now = self._clock.now()
            live = 0.0
            if sess is not None and sess.state in ("loading", "playing"):
                live = max(0.0, now - sess.last_accrual)
            state = sess.state if sess is not None else "idle"
            item = dict(sess.item) if sess is not None else None
            session_seconds = (sess.seconds + live) if sess is not None else 0.0
            today = self._today()
            if self._usage_day == today:
                daily_used = self._usage_persisted + self._usage_pending + live
            else:
                daily_used = self.usage.get(today) + live
            paused_for = (now - sess.paused_at) if sess is not None and sess.paused_at is not None else None
        session_limit = self._session_limit_seconds()
        daily_limit = self._daily_limit_seconds()
        policy = self.ctx.age_policy()
        allow_under = bool(self.ctx.settings.get("media.allow_under_min_age", False))
        bridge = self.bridge.status()
        return {
            "state": state,
            "item": item,
            "session_id": sess.id if sess is not None else None,
            "session_seconds": round(session_seconds, 1),
            "session_limit_seconds": session_limit,
            "daily_used_seconds": round(daily_used, 1),
            "daily_limit_seconds": daily_limit,
            "daily_remaining_seconds": round(max(0.0, daily_limit - daily_used), 1),
            "paused_seconds": round(paused_for, 1) if paused_for is not None else None,
            "volume": self._volume(),
            "enabled": bool(self.ctx.settings.get("media.enabled", True)),
            "offline": self.ctx.config.is_offline,
            "age_gate": {
                "video_allowed": policy.video_allowed(allow_under),
                "min_age_months": self.cfg.min_age_months_for_video,
                "age_months": policy.age_months,
                "parent_allowed_under_min_age": allow_under,
            },
            "player": {
                "backend": self.backend.name,
                "available": self.backend.available,
                "detail": self.backend.detail,
                "running": self.backend.is_running(),
                "url": self.player_url(),
                "path": self.player_path(),
                "connected": bridge["connected"],
                "last_event": bridge["last_event"],
                "info": bridge["player"],
            },
            "last_error": dict(self.last_error) if self.last_error else None,
            "items": self.repo.count(enabled_only=True),
        }

    # ================================================================ internals: gating
    def _check_allowed(self, item: dict[str, Any], override: bool, sess: _Session | None) -> None:
        settings = self.ctx.settings
        if not item.get("enabled", True):
            raise PermissionError(f"“{item['title']}” is switched off in the approved list.")
        if not settings.get("media.enabled", True):
            raise PermissionError("Videos are turned off in LittleBot's settings.")
        if self.ctx.config.is_offline:
            raise PermissionError("Videos need the internet, and LittleBot is in offline mode.")
        policy = self.ctx.age_policy()
        if not policy.video_allowed(override or bool(settings.get("media.allow_under_min_age", False))):
            min_age = self.cfg.min_age_months_for_video
            if policy.age_months is None:
                raise PermissionError(
                    f"Videos are off until the child's age is set in the profile (they're off by default under "
                    f"{min_age} months). A parent can still choose 'play anyway'.")
            raise PermissionError(
                f"Videos are off for children under {min_age} months (your child is {policy.age_months} months). "
                "A parent can allow them in settings or choose 'play anyway'.")
        if self._session_limit_seconds() <= 0 or self._daily_limit_seconds() <= 0:
            raise PermissionError("The video time limit is set to 0 minutes in settings.")
        if self._daily_remaining() < MIN_REMAINING_SECONDS:
            minutes = self._daily_limit_seconds() // 60
            raise PermissionError(f"Today's video time ({minutes} minutes) is used up. It resets tomorrow.")
        if sess is not None and sess.seconds >= self._session_limit_seconds():
            raise PermissionError("This video session has reached its time limit.")

    def _limit_reason(self, sess: _Session) -> str | None:
        if sess.seconds >= self._session_limit_seconds():
            return "session limit reached"
        if self._daily_remaining() <= 0:
            return "daily limit reached"
        return None

    def _session_limit_seconds(self) -> int:
        return max(0, int(self.ctx.settings.get("media.session_limit_minutes", self.cfg.session_limit_minutes) or 0)) * 60

    def _daily_limit_seconds(self) -> int:
        return max(0, int(self.ctx.settings.get("media.daily_limit_minutes", self.cfg.daily_limit_minutes) or 0)) * 60

    def _daily_used(self) -> float:
        with self._lock:
            today = self._today()
            self._roll_day(today)
            return self._usage_persisted + self._usage_pending

    def _daily_remaining(self) -> float:
        return self._daily_limit_seconds() - self._daily_used()

    # ================================================================ internals: session
    def _load(self, sess: _Session, item: dict[str, Any]) -> None:
        now = self._clock.now()
        with self._lock:
            sess.item = item
            sess.state = "loading"
            sess.paused_at = None
            sess.load_started = now
            sess.last_accrual = now
            sess.has_played = False
        self.bridge.set_volume(self._volume())
        sess.load_id = self.bridge.load(item)
        self.repo.record_play(item["id"])

    async def _end_session(self, reason: str, *, detail: str | None = None, session_id: int | None = None) -> bool:
        """Stop playback and give the screen back to the face. Caller holds ``_op_lock``."""
        sess = self._session
        if sess is None or (session_id is not None and sess.id != session_id):
            return False
        reason = reason if reason in STOP_REASONS else "stopped"
        with self._lock:
            self._accrue()
            self._flush_usage(force=True)
            self._session = None
        self.bridge.command("stop", reason)
        sess.lease.release()
        self.ctx.state.release(RobotState.PLAYING_MEDIA, f"media {reason}")
        payload: dict[str, Any] = {"item_id": sess.item["id"], "reason": reason, "title": sess.item["title"],
                                   "session_id": sess.id, "session_seconds": round(sess.seconds, 1)}
        if detail:
            payload["detail"] = detail
        self.ctx.bus.publish(Topics.MEDIA_STOPPED, payload, source="media")
        self.ctx.bus.publish(Topics.FACE_VISIBLE, {"visible": True}, source="media")
        log.info("media: stopped (%s%s) after %.0fs", reason, f": {detail}" if detail else "", sess.seconds)
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None
        if self.backend.can_close:
            delay = self.cfg.kiosk_close_delay_seconds  # let the child see "All done!"
            if reason == "limit" and delay > 0:
                delay += PAGE_FADE_SECONDS
            elif reason not in ("ended", "limit"):  # parent stop / error: close almost at once
                delay = min(0.2, delay)
            self._cancel_close_task()
            self._close_task = asyncio.get_running_loop().create_task(self._close_backend_later(delay))
        return True

    async def _close_backend_later(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if self._session is None:
            try:
                await self.backend.close()
            except Exception:  # noqa: BLE001
                log.exception("player backend close failed")

    def _cancel_close_task(self) -> None:
        task, self._close_task = self._close_task, None
        if task is not None and not task.done():
            task.cancel()

    def _player_alive(self) -> bool:
        running = self.backend.is_running()
        if running is not None:
            return running
        return self.bridge.connected

    def _ensure_watchdog(self) -> None:
        sess = self._session
        if sess is not None and (self._watchdog is None or self._watchdog.done()):
            self._watchdog = asyncio.get_running_loop().create_task(self._watch(sess.id))

    async def _watch(self, session_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick_seconds)
                async with self._op_lock:
                    if self._session is None or self._session.id != session_id:
                        return
                    await self._tick()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("media watchdog failed; stopping playback")
            async with self._op_lock:
                await self._end_session("error", detail="internal error")

    async def _tick(self) -> None:
        sess = self._session
        if sess is None:
            return
        now = self._clock.now()
        with self._lock:
            self._accrue()
            self._flush_usage()
        if self.bridge.connected:
            sess.ever_connected = True
        limit = self._limit_reason(sess)
        if limit:
            await self._end_session("limit", detail=limit)
            return
        if sess.state == "paused" and sess.paused_at is not None \
                and now - sess.paused_at >= self.cfg.max_pause_minutes * 60:
            await self._end_session("stopped", detail="paused for too long")
            return
        if self.backend.can_close and sess.backend_opened and self.backend.is_running() is False:
            await self._fail(sess, "player_closed", "The player window closed unexpectedly.")
            return
        if not sess.ever_connected and now - sess.load_started >= self.cfg.connect_timeout_seconds:
            await self._fail(sess, "player_not_connected",
                             "The video player page didn't open. Check the dashboard is running.")
            return
        since = self.bridge.seconds_since_seen()
        if sess.ever_connected and since is not None and since >= self.cfg.heartbeat_timeout_seconds:
            await self._fail(sess, "player_lost", "The video player stopped responding.")

    async def _fail(self, sess: _Session, code: str, message: str) -> None:
        self._record_error(sess.item, code, message)
        await self._end_session("error", detail=message)

    def _record_error(self, item: dict[str, Any], code: Any, message: str) -> None:
        self.last_error = {"item_id": item["id"], "title": item.get("title"), "code": code, "message": message,
                           "at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}
        try:
            self.repo.set_error(item["id"], message)
        except Exception:  # noqa: BLE001
            log.exception("could not store media error")
        self.ctx.bus.publish(Topics.MEDIA_ERROR, {"item_id": item["id"], "error": message, "code": code},
                             source="media")
        log.warning("media error on item %s: %s", item["id"], message)

    async def _handle_player_event(self, event: dict[str, Any]) -> dict[str, Any]:
        etype = str(event.get("type") or "")
        current = self.bridge.record_event(event)
        if etype == "heartbeat" or not current:
            return {"ok": True, "current": current}
        async with self._op_lock:
            sess = self._session
            if sess is None or sess.load_id != event.get("load_id"):
                return {"ok": True, "current": False}
            sess.ever_connected = True
            if etype == "playing":
                sess.has_played = True
                if sess.state == "loading":
                    with self._lock:
                        self._accrue()
                        sess.state = "playing"
                    if sess.item.get("last_error"):
                        self.repo.set_error(sess.item["id"], None)
                        sess.item = dict(sess.item, last_error=None)
            elif etype == "ended":
                await self._end_session("ended")
            elif etype == "error":
                code = event.get("code")
                short, message = describe_player_error(code)
                self._record_error(sess.item, code if code is not None else short, message)
                await self._end_session("error", detail=message)
            elif etype in ("hidden", "navigated_away"):
                # Something covered or replaced the player page (e.g. a YouTube link opened a new
                # tab/window in the kiosk): stop, and closing the kiosk closes any popup with it.
                await self._fail(sess, f"player_{etype}", PLAYER_HIDDEN_MESSAGE)
        return {"ok": True, "current": True}

    # ================================================================ internals: usage
    def _accrue(self) -> None:
        """Add active time since the last accrual. Caller holds ``_lock``."""
        sess = self._session
        now = self._clock.now()
        if sess is None:
            return
        delta = max(0.0, now - sess.last_accrual)
        sess.last_accrual = now
        if sess.state in ("loading", "playing") and delta > 0:
            sess.seconds += delta
            self._roll_day(self._today())
            self._usage_pending += delta

    def _roll_day(self, today: _dt.date) -> None:
        if self._usage_day != today:
            if self._usage_day is not None and self._usage_pending > 0:
                self.usage.add(self._usage_day, self._usage_pending)
            self._usage_day = today
            self._usage_pending = 0.0
            self._usage_persisted = self.usage.get(today)

    def _flush_usage(self, force: bool = False) -> None:
        now = self._clock.now()
        if self._usage_day is None or self._usage_pending <= 0:
            return
        if force or now - self._last_flush >= USAGE_FLUSH_SECONDS:
            self._usage_persisted = self.usage.add(self._usage_day, self._usage_pending)
            self._usage_pending = 0.0
            self._last_flush = now

    # ================================================================ internals: settings / volume
    def _register_validators(self) -> None:
        s = self.ctx.settings
        s.register_validator("media.volume", self._clamp_volume)
        s.register_validator("media.session_limit_minutes", _int_range(0, MAX_SESSION_LIMIT_MINUTES, "media.session_limit_minutes"))
        s.register_validator("media.daily_limit_minutes", _int_range(0, MAX_DAILY_LIMIT_MINUTES, "media.daily_limit_minutes"))

    def _clamp_volume(self, value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError("media.volume: expected a number between 0 and 1") from e
        if v != v:  # NaN
            raise ValueError("media.volume: expected a number between 0 and 1")
        return round(max(0.0, min(float(self.cfg.max_volume), v)), 3)

    def _volume(self) -> float:
        try:
            return self._clamp_volume(self.ctx.settings.get("media.volume", self.cfg.default_volume))
        except ValueError:
            return self._clamp_volume(self.cfg.default_volume)

    # ================================================================ internals: bus callbacks (any thread)
    def _on_stop_all(self, event: Event) -> None:
        if self._session is not None:
            self.bridge.command("stop", "stopped")
        self._schedule(self._stop_session(None, "stopped", f"stop all ({event.get('source', 'parent')})"))

    def _on_settings(self, event: Event) -> None:
        key = str(event.get("key", ""))
        if key == "media.volume":
            self.bridge.set_volume(self._volume())
        elif key == "media.enabled" and not event.get("value"):
            self._schedule(self._stop_session(None, "stopped", "videos turned off"))
        elif key in ("media.allow_under_min_age", "profile"):
            sess = self._session
            if sess is not None and not sess.override:
                allow = bool(self.ctx.settings.get("media.allow_under_min_age", False))
                if not self.ctx.age_policy().video_allowed(allow):
                    self._schedule(self._stop_session(sess.id, "stopped", "age setting changed"))

    def _on_shutdown(self, _event: Event) -> None:
        self._shutting_down = True

    def _on_preempt(self, lease: Lease, by: str) -> None:
        sess = self._session
        if sess is None or sess.lease is not lease:
            return
        self.bridge.command("stop", "stopped")  # stop the video immediately; bookkeeping follows on the loop
        self._schedule(self._stop_session(sess.id, "stopped", f"preempted by {by}"))

    def _stop_if_playing_item(self, item_id: int, detail: str) -> None:
        sess = self._session
        if sess is not None and sess.item["id"] == item_id:
            self.bridge.command("stop", "stopped")
            self._schedule(self._stop_session(sess.id, "stopped", detail))

    async def _stop_session(self, session_id: int | None, reason: str, detail: str) -> None:
        async with self._op_lock:
            await self._end_session(reason, detail=detail, session_id=session_id)

    def _maybe_lookup_oembed(self, item: dict[str, Any]) -> None:
        if not self.cfg.oembed_titles or self.ctx.config.is_offline:
            return
        self._schedule(self._lookup_oembed(item))

    async def _lookup_oembed(self, item: dict[str, Any]) -> None:
        try:
            result = await self._title_lookup(item["url"])
        except Exception:  # noqa: BLE001
            log.info("oEmbed lookup failed for item %s", item["id"], exc_info=True)
            return
        if result.title:
            self.repo.set_title_if_default(item["id"], result.title)
        if result.problem:
            self.repo.set_error(item["id"], result.problem)

    # ================================================================ internals: loop plumbing
    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            self._loop = loop

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run ``coro`` on the controller's loop from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task = loop.create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                coro.close()

    async def _on_controller_loop(self, coro: Coroutine[Any, Any, Any]) -> Any:
        loop = self._loop
        running = asyncio.get_running_loop()
        if loop is None or loop is running or loop.is_closed() or not loop.is_running():
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wait_for(asyncio.wrap_future(fut), 10)


def _int_range(lo: int, hi: int, key: str) -> Callable[[Any], int]:
    def _validate(value: Any) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key}: expected a whole number of minutes") from e
        return max(lo, min(hi, v))

    return _validate
