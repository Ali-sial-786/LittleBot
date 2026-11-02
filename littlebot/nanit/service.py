"""NanitService: the Nanit baby camera's live picture on LittleBot's screen, while a grown-up asked for it.

Public API (``ctx.nanit``; used by the voice's tools, littlebot/skills/nanit.py, and the dashboard)::

    await nanit.show(camera=None, *, source="voice") -> {"ok", "camera", "state", "error", "detail", "already"}
        the camera by name (else the first); "starting" until the picture comes, then "showing"
    await nanit.close(reason) -> bool          the view off, ffplay exited (True if it was on)
    nanit.close_view(reason) -> bool           the same from any thread or a bus callback (doesn't wait)
    await nanit.refresh(force=False) -> str    the bridge unit's state (``systemctl --user is-active``), cached
    nanit.status(), nanit.check(), nanit.cameras(), nanit.set_up(), nanit.offered(), nanit.active

Error codes: "unavailable" (the config switch, no screen), "disabled" (the parent's switch), "not_set_up",
"no_player", "not_running", "no_cameras", "no_such_camera", "asleep", "busy", "failed". ``ERRORS`` has the words.

The bridge
----------
indiefan/home_assistant_nanit (a Go program; scripts/setup_nanit.sh builds it at a pinned commit, signs in with
the user's own email, password and emailed code, and starts it as the systemd user unit ``littlebot-nanit``). It
keeps a sign-in (a refresh token) in ``data/nanit/session.json`` (mode 600), runs an RTMP server on port 1935 and,
while it runs, keeps asking each camera to send its picture to ``rtmp://<the robot's LAN address>:1935/local/
<baby uid>``. A viewer that connects while no camera is sending is closed at once ("No stream publisher registered
yet"), and every viewer is closed when the camera stops ("Closing subscriber because publisher quit").

LittleBot never reads the session file (it only checks that one exists). The unit's start script
(scripts/nanit_bridge.sh) writes ``data/nanit/cameras.json`` with each camera's name and baby uid, and nothing else.

The view
--------
``ffplay -fs -noborder -alwaysontop -window_title LittleBot -fflags nobuffer -flags low_delay -framedrop -autoexit
-nostats -loglevel verbose [-an] rtmp://127.0.0.1:1935/local/<uid>``: full screen and borderless over the face, the
picture scaled to fit (ffplay keeps its shape), little delay. The video decoder starting ("[h264 @ ...] Reinit
context to 1920x1088", verbose) = the picture is on the screen (``state`` "showing"). (``-nostats`` also hides
ffplay's "Input #0" summary, which the first version waited for: the picture showed and the view still closed after
20 s, 2026-11-02.)
ffplay exiting before that = no picture yet: it is tried again every ``RETRY_S`` until
``nanit.start_timeout_seconds``, then the view closes and says why (the bridge isn't running, or the camera isn't
sending). ffplay exiting after that = the stream ended: the face comes back.

The screen, never the speaker
-----------------------------
One lease on ``Resource.SCREEN`` at mirroring's priority (45), like the browser view: a game (50) or a video (60)
takes it and the view closes. LittleBot's speaker and microphone are never taken, so a voice chat keeps working
("close the camera", "stop"). The baby's sound (the ``nanit.sound`` switch, default on) plays through the system's
sound beside the voice.

It closes on STOP ALL, sleep, an error state, a video, game or activity starting, the lease being taken, the
parent's switch, ffplay exiting, the dashboard's Close, ``close_baby_camera``, no picture in time, and after
``nanit.max_minutes`` (0 = never).

Logs: one INFO line per open, per picture starting and per close (the camera's name, the source, the reason; never
what ffplay prints, which is at DEBUG). Bus: ``nanit.started`` {camera, source}, ``nanit.showing`` {camera},
``nanit.stopped`` {camera, reason, code}; ``screen.keep_awake`` {kind: "nanit"} every 30 s while it shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.mirror import adb as A
from littlebot.mirror.service import PRIORITY as MIRROR_PRIORITY
from littlebot.mirror.service import Launcher, default_launcher, make_room

log = logging.getLogger("littlebot.nanit")

OWNER = "nanit"
PRIORITY = MIRROR_PRIORITY  # above a card (40), below a game (50) and a video (60): as mirroring and the browser
STARTED = "nanit.started"  # {camera, source}
SHOWING = "nanit.showing"  # {camera}
STOPPED = "nanit.stopped"  # {camera, reason, code}
KEEP_AWAKE = "screen.keep_awake"  # {kind}: LittleBot doesn't doze off on the baby camera
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
KEEP_AWAKE_EVERY_S = 30.0
TERMINATE_S = 2.0  # SIGTERM -> this long -> SIGKILL
RETRY_S = 2.0  # no picture yet: ffplay tries again after this long
BRIDGE_CHECK_S = 5.0  # the bridge's state is asked again after this long
SYSTEMCTL_TIMEOUT_S = 5.0
MAX_LINE = 300
MAX_CAMERAS = 8
MAX_LIST_BYTES = 64 * 1024
SETUP_COMMAND = "bash ~/LittleBot/scripts/setup_nanit.sh"
STATUS_COMMAND = "systemctl --user status littlebot-nanit"
# The picture is coming: the video decoder started (verbose; the camera's H.264), or ffplay's stream summary (only
# printed without -nostats).
STREAM_OPEN = re.compile(r"^\s*Input #0\b|\bReinit context to \d+x\d+")
UID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")  # the bridge's own rule for /local/<uid>
BRIDGE_STATES = ("active", "inactive", "failed", "activating", "deactivating", "reloading", "refreshing",
                 "maintenance")
_CLOSE_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value,
                 RobotState.PLAYING_ACTIVITY.value}
NO_STREAM = "no_stream"  # the close code when no picture came in time

ERRORS = {
    "unavailable": "The baby camera view isn't available on this robot.",
    "disabled": "The baby camera is switched off on the dashboard (Assistant page).",
    "not_set_up": f"The Nanit bridge isn't set up on LittleBot yet. On the robot, run: {SETUP_COMMAND}",
    "no_player": "ffplay isn't installed on LittleBot (it comes with ffmpeg: sudo apt install ffmpeg).",
    "not_running": f"The Nanit bridge isn't running on LittleBot. Check it with: {STATUS_COMMAND}",
    "no_cameras": "The Nanit bridge hasn't found a camera yet. Give it a minute after it starts.",
    "no_such_camera": "There's no Nanit camera with that name.",
    "asleep": "LittleBot is asleep.",
    "busy": "LittleBot's screen is busy with something more important right now.",
    "failed": "The baby camera view didn't start.",
}


@dataclass(eq=False)
class _View:
    id: int
    camera: dict[str, str]  # {"uid", "name"}
    source: str
    started: float
    state: str = "starting"  # starting (waiting for the picture) | showing
    handle: Any = None
    lease: Lease | None = None
    ended: bool = False
    reason: str = ""
    code: str = ""
    shown_at: float | None = None
    tries: int = 0
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class NanitService(Service):
    name = "nanit"

    def __init__(self, ctx: Any, *, launcher: Launcher | None = None, runner: A.Runner | None = None,
                 which: Callable[[str], str | None] = shutil.which, clock: Callable[[], float] = time.monotonic,
                 screen: bool | None = None, data_dir: str | os.PathLike[str] | None = None,
                 unit_dir: str | os.PathLike[str] | None = None, start_timeout: float | None = None,
                 max_seconds: float | None = None, retry_every: float = RETRY_S,
                 keep_awake_every: float = KEEP_AWAKE_EVERY_S) -> None:
        """``launcher`` starts ffplay, ``runner`` runs systemctl (tests fake both). ``data_dir``: the bridge's
        folder (default ``data/nanit``), ``unit_dir``: where the user unit lives (default
        ``~/.config/systemd/user``). ``screen``: whether there is a screen to show on (default: not headless)."""
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.nanit
        self._launcher = launcher or default_launcher
        self._runner = runner or A.default_runner
        self._which = which
        self._clock = clock
        self._screen = (not ctx.config.display.headless) if screen is None else bool(screen)
        extras = getattr(ctx, "extras", None) or {}
        self._dir = Path(data_dir or extras.get("nanit_dir") or ctx.config.data_path / "nanit")
        self._unit_dir = Path(unit_dir) if unit_dir is not None else (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user")
        self._start_s = float(self.cfg.start_timeout_seconds) if start_timeout is None else float(start_timeout)
        self._max_s = float(self.cfg.max_minutes) * 60.0 if max_seconds is None else float(max_seconds)
        self._retry_s = float(retry_every)
        self._awake_every = float(keep_awake_every)
        self._lock = threading.RLock()
        self._show_lock = asyncio.Lock()
        self._view: _View | None = None
        self._ids = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._teardowns: set[Any] = set()
        self._last: dict[str, Any] | None = None
        self._bridge: str | None = None  # the unit's state, None until asked
        self._bridge_at: float | None = None
        self._list_cache: tuple[float, list[dict[str, str]]] | None = None
        self.last_error: str | None = None
        self._subs: list[Subscription] = []

    # ================================================================ lifecycle
    async def start(self) -> None:  # type: ignore[override]
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, lambda _e: self.close_view("stop all")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.close_view("a video started")),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close_view("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.close_view("a game started")),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
            ]
        why = self.unavailable_reason()
        if why is None:
            self.set_status(ServiceStatus.RUNNING, "" if self.set_up() else "not set up")
        elif not self.cfg.enabled:
            self.set_status(ServiceStatus.DISABLED, why)
        else:  # no screen: degraded, like the browser view and mirroring without one
            self.set_status(ServiceStatus.DEGRADED, why)

    async def stop(self, reason: str | None = None) -> Any:  # type: ignore[override]
        """``await stop()``: the subsystem shuts down (the view closes first)."""
        await self.close(reason or "shutdown")
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)

    # ================================================================ set up?
    def _bridge_path(self) -> Path:
        return Path(os.path.expanduser(str(self.cfg.bridge_command)))

    def _player(self) -> str | None:
        cmd = str(self.cfg.ffplay_command or "ffplay")
        if os.path.isabs(cmd):
            return cmd if os.access(cmd, os.X_OK) else None
        return self._which(cmd)

    def setup_parts(self) -> dict[str, bool]:
        """What setup_nanit.sh made: the bridge program, a sign-in (the session file only exists: it is never
        read here) and the user unit."""
        return {"bridge": self._bridge_path().is_file(),
                "signed_in": (self._dir / "session.json").is_file(),
                "service": (self._unit_dir / str(self.cfg.unit)).is_file()}

    def set_up(self) -> bool:
        return all(self.setup_parts().values())

    def cameras(self) -> list[dict[str, str]]:
        """The cameras the bridge listed ([{uid, name}]), from ``data/nanit/cameras.json``; [] if none yet."""
        path = self._dir / "cameras.json"
        try:
            st = path.stat()
        except OSError:
            return []
        cached = self._list_cache
        if cached is not None and cached[0] == st.st_mtime and st.st_size <= MAX_LIST_BYTES:
            return [dict(c) for c in cached[1]]
        out: list[dict[str, str]] = []
        try:
            if st.st_size > MAX_LIST_BYTES:
                raise ValueError("too big")
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("cameras") if isinstance(raw, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("uid") or "")
                if not UID_RE.match(uid) or any(c["uid"] == uid for c in out):
                    continue
                name = re.sub(r"[^\w '\-.]", "", str(item.get("name") or ""))[:40].strip()
                out.append({"uid": uid, "name": name or f"Camera {len(out) + 1}"})
                if len(out) >= MAX_CAMERAS:
                    break
        except (OSError, ValueError, AttributeError):
            log.debug("nanit: the camera list can't be read", exc_info=True)
            out = []
        self._list_cache = (st.st_mtime, out)
        return [dict(c) for c in out]

    def pick(self, camera: Any = None) -> dict[str, str] | None:
        """The camera called ``camera`` (its name, loosely), the first with no name; None if there is no such
        camera (with one camera, any name means it)."""
        cams = self.cameras()
        if not cams:
            return None
        want = _words(camera)
        if not want:
            return cams[0]
        for cam in cams:
            if _words(cam["name"]) == want:
                return cam
        for cam in cams:
            have = _words(cam["name"])
            if have and (set(want) <= set(have) or set(have) <= set(want)):
                return cam
        for cam in cams:
            if set(want) & set(_words(cam["name"])):
                return cam
        return cams[0] if len(cams) == 1 else None

    # ================================================================ the bridge's state
    async def refresh(self, force: bool = False) -> str:
        """The bridge unit's state from ``systemctl --user is-active`` (never blocks the loop; asked again after
        ``BRIDGE_CHECK_S``): active | inactive | failed | activating | ... | missing (no unit) | unknown."""
        now = self._clock()
        if not force and self._bridge is not None and self._bridge_at is not None and \
                now - self._bridge_at < BRIDGE_CHECK_S:
            return self._bridge
        if not self.setup_parts()["service"]:
            state = "missing"
        else:
            systemctl = self._which(str(self.cfg.systemctl_command or "systemctl"))
            if systemctl is None:
                state = "unknown"
            else:
                try:
                    _rc, out = await self._runner([systemctl, "--user", "is-active", str(self.cfg.unit)],
                                                  SYSTEMCTL_TIMEOUT_S, None)
                except (OSError, ValueError):
                    out = ""
                word = (out.strip().splitlines() or [""])[0].strip()
                state = word if word in BRIDGE_STATES else "unknown"
        self._bridge, self._bridge_at = state, self._clock()
        return state

    @property
    def bridge_running(self) -> bool:
        return self._bridge == "active"

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (nanit.enabled)"
        if not self._screen:
            return "no screen (headless run)"
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get("nanit.enabled", True))

    @property
    def sound(self) -> bool:
        return bool(self.ctx.settings.get("nanit.sound", self.cfg.sound))

    def offered(self) -> bool:
        """The voice may offer the camera: it exists, it's set up and the parent's switch is on."""
        return self.unavailable_reason() is None and self.switched_on and self.set_up()

    def check(self) -> str | None:
        """Why the view can't open now (an ``ERRORS`` code), else None. Cheap: nothing is run."""
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        if not self.set_up():
            return "not_set_up"
        if self._player() is None:
            return "no_player"
        if self.ctx.state.state in (RobotState.SLEEPING, RobotState.ERROR):
            return "asleep"
        return None

    # ================================================================ showing
    @property
    def active(self) -> bool:
        v = self._view
        return v is not None and not v.ended

    async def show(self, camera: Any = None, *, source: str = "voice") -> dict[str, Any]:
        """Show a camera's picture (by name, else the first); see the module docstring."""
        self._bind_loop()
        source = str(source or "voice")[:20]
        why = self.check()
        if why is not None:
            return self._fail(why)
        if await self.refresh(force=True) != "active":
            return self._fail("not_running")
        if not self.cameras():
            return self._fail("no_cameras")
        cam = self.pick(camera)
        if cam is None:
            return self._fail("no_such_camera")
        async with self._show_lock:
            v = self._view
            if v is not None and not v.ended:
                if v.camera["uid"] == cam["uid"]:
                    return self._ok(v, already=True)
                await self.close("switching cameras")
            await make_room(self.ctx, "nanit", "the baby camera opened")
            v = self._new_view(cam, source)
            if not self._take_screen(v):
                self._drop(v)
                return self._fail("busy")
            try:
                v.handle = await self._launch(v)
            except (OSError, ValueError) as e:
                log.warning("nanit: ffplay didn't start (%s)", str(e)[:120])
                self._drop(v)
                return self._fail("failed")
            if v.ended:  # closed while starting
                await self._wait_teardowns()
                await self._teardown(v)
                return self._fail("failed", log_it=False)
            self._spawn(v, self._watch(v))
            self._spawn(v, self._start_deadline(v))
            if self._max_s > 0:
                self._spawn(v, self._time_limit(v))
            log.info("nanit: opening the camera %r (from %s)", cam["name"], source)
            self._publish(STARTED, {"camera": cam["name"], "source": source})
            return self._ok(v)

    def _new_view(self, cam: dict[str, str], source: str) -> _View:
        self._ids += 1
        v = _View(id=self._ids, camera=dict(cam), source=source, started=self._clock())
        with self._lock:
            self._view = v
        return v

    def stream_url(self, uid: str) -> str:
        return f"rtmp://127.0.0.1:{int(self.cfg.rtmp_port)}/local/{uid}"

    def ffplay_command(self, uid: str) -> list[str]:
        argv = [self._player() or "ffplay", "-hide_banner", "-nostats", "-loglevel", "verbose",
                "-fflags", "nobuffer", "-flags", "low_delay", "-framedrop",
                "-fs", "-noborder", "-alwaysontop", "-window_title", "LittleBot", "-autoexit"]
        if not self.sound:
            argv.append("-an")
        argv += [str(a) for a in self.cfg.extra_ffplay_args]
        return [*argv, self.stream_url(uid)]

    async def _launch(self, v: _View) -> Any:
        v.tries += 1
        return await self._launcher(self.ffplay_command(v.camera["uid"]), None)

    # ================================================================ the screen
    def _take_screen(self, v: _View) -> bool:
        # Taken like a video (a card on the screen goes), then held lower so a game or a video wins later.
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=self._preempted(v))
        if lease is None:
            return False
        v.lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], PRIORITY, on_preempt=self._preempted(v)) or lease
        return True

    def _preempted(self, v: _View) -> Callable[[Lease, str], None]:
        def on_preempt(lease: Lease, by: str) -> None:
            if v.lease is None or v.lease is lease:
                self.close_view(f"{by} took the screen")
        return on_preempt

    def _showing(self, v: _View) -> None:
        v.state, v.shown_at = "showing", self._clock()
        log.info("nanit: the camera %r is on the screen", v.camera["name"])
        self._publish(SHOWING, {"camera": v.camera["name"]})
        self._publish(KEEP_AWAKE, {"kind": "nanit"})
        self._spawn(v, self._keep_awake(v))

    async def _keep_awake(self, v: _View) -> None:
        while not v.ended:
            await asyncio.sleep(self._awake_every)
            if not v.ended:
                self._publish(KEEP_AWAKE, {"kind": "nanit"})

    # ================================================================ watching ffplay
    async def _watch(self, v: _View) -> None:
        """ffplay's output (so its pipe never fills; "Input #0" = the picture is coming) and its exit: before the
        picture came it is tried again (the bridge closes a viewer while the camera isn't sending yet)."""
        while not v.ended:
            reader = self._spawn(v, self._read(v, v.handle))
            code = await v.handle.wait()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), 0.5)
            if v.ended:
                return
            if v.state == "showing":
                self.close_view("the stream ended")
                return
            log.debug("nanit: no picture yet (ffplay exited %s); trying again", code)
            await asyncio.sleep(self._retry_s)
            if v.ended:
                return
            try:
                v.handle = await self._launch(v)
            except (OSError, ValueError) as e:
                log.warning("nanit: ffplay didn't start again (%s)", str(e)[:120])
                self.close_view("ffplay didn't start")
                return

    async def _read(self, v: _View, handle: Any) -> None:
        try:
            async for line in handle.lines():
                if v.ended:
                    return
                line = line[:MAX_LINE]
                log.debug("nanit ffplay: %s", line)
                if v.state == "starting" and STREAM_OPEN.search(line):
                    self._showing(v)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("nanit: reading ffplay's output failed", exc_info=True)

    async def _start_deadline(self, v: _View) -> None:
        await asyncio.sleep(self._start_s)
        if v.ended or v.state == "showing":
            return
        bridge = await self.refresh(force=True)
        reason = "the bridge isn't running" if bridge != "active" else "the camera isn't sending its picture"
        self.close_view(reason, code=NO_STREAM)

    async def _time_limit(self, v: _View) -> None:
        await asyncio.sleep(self._max_s)
        if not v.ended:
            minutes = self._max_s / 60.0
            self.close_view(f"the time limit ({minutes:.0f} minutes)" if minutes >= 1 else "the time limit")

    # ================================================================ closing
    def close_view(self, reason: str = "closed", *, code: str = "") -> bool:
        """The view off, the face back (ffplay exits). Any thread. True if it was on."""
        with self._lock:
            v = self._view
            if v is None or v.ended:
                return False
            v.ended = True
            v.reason = str(reason or "closed")[:80]
            v.code = str(code or "")[:20]
            self._view = None
        if v.lease is not None:
            v.lease.release()
        seconds = max(0.0, self._clock() - v.started)
        self._last = {"camera": v.camera["name"], "reason": v.reason, "code": v.code or None,
                      "seconds": round(seconds, 1), "showed": v.shown_at is not None, "at": time.time()}
        log.info("nanit: the camera %r closed (%s) after %.0f s", v.camera["name"], v.reason, seconds)
        self._publish(STOPPED, {"camera": v.camera["name"], "reason": v.reason, "code": v.code or None})
        self._schedule_teardown(v)
        return True

    async def close(self, reason: str = "closed") -> bool:
        """``close_view``, then wait until ffplay has exited."""
        closed = self.close_view(reason)
        await self._wait_teardowns()
        return closed

    def _drop(self, v: _View) -> None:
        """A view that never got going: forget it (nothing was announced)."""
        with self._lock:
            if self._view is v:
                self._view = None
        v.ended = True
        if v.lease is not None:
            v.lease.release()

    async def _wait_teardowns(self) -> None:
        pending = [f for f in list(self._teardowns) if not f.done()]
        if pending:
            await asyncio.wait([asyncio.wrap_future(f) if not isinstance(f, asyncio.Future) else f
                                for f in pending], timeout=TERMINATE_S + 3)

    def _schedule_teardown(self, v: _View) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._teardown_now(v)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut: Any = loop.create_task(self._teardown(v))
        else:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._teardown(v), loop)
            except RuntimeError:
                self._teardown_now(v)
                return
        self._teardowns.add(fut)
        fut.add_done_callback(self._teardowns.discard)

    async def _teardown(self, v: _View) -> None:
        current = asyncio.current_task()
        for t in list(v.tasks):
            if t is not current:
                t.cancel()
        h = v.handle
        if h is None or h.returncode is not None:
            return
        try:
            h.terminate()
            await asyncio.wait_for(h.wait(), TERMINATE_S)
        except asyncio.TimeoutError:
            log.warning("nanit: ffplay didn't exit after SIGTERM; killing it")
            h.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(h.wait(), 1.0)
        except Exception:  # noqa: BLE001
            log.exception("nanit: stopping ffplay failed")

    def _teardown_now(self, v: _View) -> None:
        if v.handle is not None and v.handle.returncode is None:
            with contextlib.suppress(Exception):
                v.handle.terminate()

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        """What the dashboard shows (no secret: never the sign-in, never a token). The bridge's state is the
        last one asked (``await refresh()`` first for a fresh one)."""
        why = self.unavailable_reason()
        parts = self.setup_parts()
        out: dict[str, Any] = {
            "available": why is None, "enabled": self.switched_on, "detail": why,
            "set_up": all(parts.values()), "setup": parts, "setup_command": SETUP_COMMAND,
            "status_command": STATUS_COMMAND, "player": self._player() is not None,
            "bridge": self._bridge, "bridge_running": self.bridge_running,
            "cameras": [{"name": c["name"]} for c in self.cameras()],
            "sound": self.sound, "max_minutes": int(self.cfg.max_minutes),
            "start_timeout_seconds": self._start_s,
            "active": False, "camera": None, "state": None, "seconds": None, "minutes_left": None,
            "last": dict(self._last) if self._last else None, "last_error": self.last_error}
        v = self._view
        if v is not None and not v.ended:
            elapsed = max(0.0, self._clock() - v.started)
            out.update({"active": True, "camera": v.camera["name"], "state": v.state, "source": v.source,
                        "seconds": round(elapsed, 1)})
            if self._max_s > 0:
                out["minutes_left"] = round(max(0.0, self._max_s - elapsed) / 60.0, 1)
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _CLOSE_STATES:
            self.close_view({"SLEEPING": "LittleBot went to sleep", "ERROR": "an error"}.get(str(new),
                                                                                             f"state {new}"))

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "nanit.enabled" and not event.get("value"):
            self.close_view("switched off")

    # ================================================================ plumbing
    def _spawn(self, v: _View, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        v.tasks.add(task)
        task.add_done_callback(v.tasks.discard)
        return task

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            self._loop = loop

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="nanit")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _ok(self, v: _View, *, already: bool = False) -> dict[str, Any]:
        self.last_error = None
        return {"ok": True, "camera": v.camera["name"], "state": v.state, "error": None, "detail": "",
                "already": already}

    def _fail(self, code: str, *, log_it: bool = True) -> dict[str, Any]:
        detail = ERRORS.get(code, "")
        if code == "no_such_camera":
            names = ", ".join(c["name"] for c in self.cameras())
            detail = f"{detail} The cameras are: {names}." if names else detail
        if log_it:
            self.last_error = code
            log.info("nanit: the camera view didn't open (%s)", code)
        return {"ok": False, "camera": None, "state": None, "error": code, "detail": detail, "already": False}


def _words(text: Any) -> list[str]:
    """A camera's name as plain words, for matching ("Nursery cam" ~ "the nursery camera")."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'s", ""))
    skip = {"the", "a", "my", "our", "camera", "cam", "nanit", "monitor", "baby", "s"}
    return [w for w in words if w not in skip]


__all__ = ["ERRORS", "KEEP_AWAKE", "NO_STREAM", "OWNER", "PRIORITY", "SETUP_COMMAND", "SHOWING", "STARTED",
           "STOPPED", "NanitService"]
