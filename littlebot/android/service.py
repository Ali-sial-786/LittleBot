"""AndroidService: Android (Waydroid) on LittleBot's screen while it is in front, and shut down the rest of the time.

Public API (``ctx.android``; used by the voice's tools, littlebot/skills/android.py, and the dashboard)::

    await android.open(source="voice", app=None) -> {"ok", "state", "error", "detail", "already"}
    await android.close(reason) -> bool      Android shut down, its memory back (True if it was running)
    android.close_soon(reason) -> bool       the same from any thread or a bus callback (doesn't wait)
    await android.refresh()                  re-check the container service (the dashboard, before status())
    android.status(), android.check(), android.setup_step(), android.active

Error codes: "unavailable" (the config switch, no screen), "disabled" (the parent's switch), "not_set_up",
"not_ready" (an error state), "low_memory", "busy", "failed", "unknown_app". ``ERRORS`` has the words.

Only while it is on the screen (the owner's rule, 2026-11-03)
------------------------------------------------------------
Android takes 1 to 1.5 GB of the robot's 4 GB while it runs. So it is shut down (``waydroid session stop``: the
container's processes end and the memory comes back) as soon as it isn't on the screen:

* its window isn't the one in front for ``android.off_screen_seconds`` (10): closed with its title bar's X, or
  another window over it. The desktop is asked every ``POLL_S`` (littlebot/android/windows.py);
* the screen went dark: the face's quiet spell (``face.screen_off_after_seconds``, 3 min). Using Android counts
  as something happening (below), so that happens when nobody has touched it for that long;
* something else takes the screen (a video, a game, an activity, phone mirroring, the Nanit camera, the browser),
  STOP ALL, sleep, an error state, the parent's switch, the dashboard's Close, "close Android";
* its window didn't come within ``android.start_timeout_seconds`` (180) of starting;
* free memory fell under ``android.min_free_mb_running`` (250 MB) while it ran: LittleBot comes first;
* LittleBot stopping (a restart of its service ends Android too).

A Waydroid session started outside LittleBot (``scripts/setup_waydroid.sh start``) is noticed within
``ADOPT_EVERY_S`` and watched the same way.

Starting
--------
The gates (``check``), then the screen, as phone mirroring takes it (one lease on ``Resource.SCREEN``, taken at
MEDIA so a card goes, held at 45 so a game or a video wins), then ``waydroid session start``, which runs until
Android stops (its exit means Android is off). Once ``waydroid status`` says RUNNING and ``sys.boot_completed`` is
1 (the first start takes a minute or two): ``waydroid show-full-ui``, and with an app ``waydroid app launch
<package>`` (``android.apps``: "aosu" is the doorbell cameras' app).

While it runs
-------------
The robot's input devices are watched (littlebot/android/inputs.py: only the time of the last touch). A touch is
published as ``android.used`` (at most every ``USED_EVERY_S``), which keeps the face's screen on and LittleBot from
falling asleep, as a touch on the face does. The wake word and the voice keep working while Android is in front.

Logs: one INFO line per start, per window shown and per stop (the reason, how long, the memory free after).
Bus: ``android.started`` {source, app}, ``android.showing`` {}, ``android.used`` {}, ``android.stopped`` {reason}.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mmap
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from littlebot.android.inputs import InputWatch
from littlebot.android.windows import Window, list_windows
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.mirror import adb as A
from littlebot.mirror.service import PRIORITY as MIRROR_PRIORITY
from littlebot.mirror.service import Launcher, default_launcher, make_room

log = logging.getLogger("littlebot.android")

OWNER = "android"
PRIORITY = MIRROR_PRIORITY  # above a card (40), below a game (50) and a video (60): as mirroring and the browser
STARTED = "android.started"  # {source, app}
SHOWING = "android.showing"  # {}
USED = "android.used"  # {}: someone touched the screen while Android runs
STOPPED = "android.stopped"  # {reason}
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
POLL_S = 2.0  # how often the desktop is asked which window is in front
BOOT_POLL_S = 3.0  # how often Android is asked whether it has finished starting
ADOPT_EVERY_S = 15.0  # how often to look for a Waydroid session LittleBot didn't start
USED_EVERY_S = 5.0
COMMAND_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 30.0  # ``waydroid session stop``
EXIT_WAIT_S = 10.0  # then ``waydroid session start`` should have exited; else it's terminated
SETTLE_S = 2.0  # the memory comes back a moment after the container stops
TERMINATE_S = 2.0
PAGE_SIZE = 4096  # Android's images need the 4K-page kernel (the Pi 5's usual one has 16K pages)
IMAGES = ("/var/lib/waydroid/images/system.img", "/usr/share/waydroid-extra/images/system.img")
CONTAINER_UNIT = "waydroid-container.service"
SETUP_SCRIPT = "~/LittleBot/scripts/setup_waydroid.sh"
READY_LINE = "is ready"  # ``waydroid session start`` prints "Android with user 0 is ready" once it has started
_CLOSE_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value,
                 RobotState.PLAYING_ACTIVITY.value}

SETUP = {  # the next step still to do -> what to run on the robot (each asks for the password itself)
    "kernel": f"{SETUP_SCRIPT} kernel   (then: sudo reboot)",
    "install": f"{SETUP_SCRIPT} install",
    "container": f"sudo systemctl enable --now {CONTAINER_UNIT}",
}
ERRORS = {
    "unavailable": "Android isn't available on this robot.",
    "disabled": "Android is switched off on the dashboard (Assistant page).",
    "not_set_up": "Android isn't set up on LittleBot yet.",
    "not_ready": "LittleBot has an error right now.",
    "low_memory": "There isn't enough free memory for Android right now.",
    "busy": "LittleBot's screen is busy with something more important right now.",
    "failed": "Android didn't start.",
    "unknown_app": "That app isn't on LittleBot's list of Android apps.",
}


@dataclass(eq=False)
class _Session:
    id: int
    source: str
    app: str | None  # the Android package to open once it has started
    started: float
    state: str = "starting"  # starting | showing
    handle: Any = None  # ``waydroid session start`` (None: a session LittleBot didn't start)
    pid: int | None = None  # a session LittleBot didn't start: its process
    lease: Lease | None = None
    inputs: Any = None
    ended: bool = False
    reason: str = ""
    shown_at: float | None = None
    off_since: float | None = None
    free_before: int | None = None  # MB free just before it was shut down
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # waydroid said Android "is ready"
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


def where_android_is(windows: list[Window] | None, match: str = "waydroid") -> str | None:
    """Android's window: "front", "behind" (another window is in front of it), "none" (no such window), or None
    (the desktop couldn't be asked). Its app id or title has ``match`` in it (any case)."""
    if windows is None:
        return None
    m = match.lower()
    mine = [w for w in windows if m in w.app_id.lower() or m in w.title.lower()]
    if not mine:
        return "none"
    if any(w.active and not w.minimized for w in mine):
        return "front"
    if not any(w.active for w in windows) and any(not w.minimized for w in mine):
        return "front"  # nothing has the focus: it shows
    return "behind"


def free_mb(path: str = "/proc/meminfo") -> int | None:
    """MemAvailable in MB (the memory programs can still get), or None (not Linux)."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def outside_session(proc: str = "/proc", *, me: int | None = None) -> int | None:
    """The pid of a ``waydroid session start`` running as this user (not one of LittleBot's own), or None."""
    me = os.getpid() if me is None else me
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    uid = os.getuid() if hasattr(os, "getuid") else None
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if uid is not None and os.stat(os.path.join(proc, name)).st_uid != uid:
                continue
            with open(os.path.join(proc, name, "cmdline"), "rb") as fh:
                args = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if any(os.path.basename(a) == "waydroid" for a in args[:2]) and args[-2:] == ["session", "start"]:
            return int(name)
    return None


class AndroidService(Service):
    name = "android"

    def __init__(self, ctx: Any, *, launcher: Launcher | None = None, runner: A.Runner | None = None,
                 windows: Callable[[], list[Window] | None] | None = None,
                 inputs: Callable[[Callable[[], None]], Any] | None = None,
                 which: Callable[[str], str | None] = shutil.which, exists: Callable[[str], bool] = os.path.exists,
                 page_size: Callable[[], int] | None = None, memory: Callable[[], int | None] = free_mb,
                 outside: Callable[[], int | None] = outside_session, alive: Callable[[int], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, screen: bool | None = None,
                 poll: float = POLL_S, boot_poll: float = BOOT_POLL_S, adopt_every: float = ADOPT_EVERY_S,
                 settle: float = SETTLE_S) -> None:
        """Everything that touches the robot is a parameter (tests fake them): ``launcher`` starts ``waydroid session
        start``, ``runner`` its short commands, ``windows`` asks the desktop, ``inputs`` makes the touch watch.
        ``screen``: whether there is a screen to show on (default: not in a headless run)."""
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.android
        self._launcher = launcher or default_launcher
        self._runner = runner or A.default_runner
        self._windows = windows or list_windows
        self._inputs = inputs or (lambda cb: InputWatch(cb, every=USED_EVERY_S))
        self._which = which
        self._exists = exists
        self._page_size = page_size or (lambda: mmap.PAGESIZE)
        self._memory = memory
        self._outside = outside
        self._alive = alive or (lambda pid: os.path.exists(f"/proc/{pid}"))
        self._clock = clock
        self._screen = (not ctx.config.display.headless) if screen is None else bool(screen)
        self._poll, self._boot_poll, self._adopt_every, self._settle = poll, boot_poll, adopt_every, settle
        self._lock = threading.RLock()
        self._start_lock = asyncio.Lock()
        self._session: _Session | None = None
        self._ids = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._teardowns: set[Any] = set()
        self._container: bool | None = None  # waydroid-container.service active (None: not asked yet)
        self._own_pids: set[int] = set()  # the ``waydroid session start`` processes LittleBot started
        self._blind_told = False
        self._last: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._subs: list[Subscription] = []
        self._adopter: asyncio.Task[Any] | None = None

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, lambda _e: self.close_soon("stop all")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.close_soon("a video started")),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close_soon("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.close_soon("a game started")),
                bus.subscribe(Topics.FACE_SCREEN, self._on_screen),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
            ]
        why = self.unavailable_reason()
        if why is not None:
            self.set_status(ServiceStatus.DISABLED if not self.cfg.enabled else ServiceStatus.DEGRADED, why)
            return
        await self.refresh()
        step = self.setup_step()
        self.set_status(ServiceStatus.RUNNING, f"not set up: {step}" if step else "")
        if self._adopter is None:  # (also when Waydroid is installed later: it looks only once it is)
            self._adopter = asyncio.get_running_loop().create_task(self._adopt_loop())

    async def stop(self) -> None:
        if self._adopter is not None:
            self._adopter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._adopter
            self._adopter = None
        await self.close("LittleBot is stopping")
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)

    # ================================================================ set up?
    def _path(self) -> str | None:
        cmd = str(self.cfg.waydroid_command or "waydroid")
        if os.path.isabs(cmd):
            return cmd if self._exists(cmd) else None
        return self._which(cmd)

    def setup_step(self) -> str | None:
        """The next step still to do ("kernel", "install", "container"; ``SETUP`` has the command), or None."""
        if self._page_size() != PAGE_SIZE:
            return "kernel"
        if self._path() is None or not any(self._exists(p) for p in IMAGES):
            return "install"
        if self._container is False:
            return "container"
        return None

    async def refresh(self) -> None:
        """Ask systemd whether Android's container service runs (``waydroid session start`` needs it)."""
        if self._path() is None:
            self._container = None
            return
        rc, out = await self._run([str(self.cfg.systemctl_command), "is-active", CONTAINER_UNIT], timeout=5.0)
        self._container = rc == 0 and out.strip().splitlines()[:1] == ["active"]

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (android.enabled)"
        if not self._screen:
            return "no screen (headless run)"
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get("android.enabled", True))

    def offered(self) -> bool:
        """The voice may offer Android (it exists and the parent's switch is on)."""
        return self.unavailable_reason() is None and self.switched_on

    def check(self) -> str | None:
        """Why Android can't start now (an ``ERRORS`` code), else None. Cheap: nothing is run."""
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        if self.setup_step() is not None:
            return "not_set_up"
        if self.ctx.state.state == RobotState.ERROR:
            return "not_ready"
        free = self._memory()
        if free is not None and free < int(self.cfg.min_free_mb):
            return "low_memory"
        return None

    def package_of(self, app: Any) -> str | None:
        """The Android package for an app's name on ``android.apps`` ("aosu"), or None."""
        key = str(app or "").strip().lower()
        apps = {str(k).lower(): str(v) for k, v in dict(self.cfg.apps or {}).items()}
        if key in apps:
            return apps[key]
        return key if key in apps.values() else None

    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    # ================================================================ starting
    async def open(self, *, source: str = "voice", app: str | None = None) -> dict[str, Any]:
        """Start Android and show it (with ``app``: open that app in it); see the module docstring."""
        self._bind_loop()
        source = str(source or "voice")[:20]
        package = None
        if app:
            package = self.package_of(app)
            if package is None:
                return self._fail("unknown_app", log_it=False)
        async with self._start_lock:
            s = self._session
            if s is not None and not s.ended:
                if package:
                    if s.state == "showing":
                        await self._launch_app(package)
                    else:
                        s.app = package
                return self._ok(s, already=True)
            await self.refresh()
            why = self.check()
            if why is not None:
                return self._fail(why)
            if self.ctx.state.state == RobotState.SLEEPING:
                self.ctx.state.transition(RobotState.IDLE, "Android was opened")
            await make_room(self.ctx, "android", "Android started")
            s = self._new_session(source, package)
            if not self._take_screen(s):
                self._drop(s)
                return self._fail("busy")
            try:
                s.handle = await self._launcher([self._path() or "waydroid", "session", "start"], self._env())
            except (OSError, ValueError) as e:
                log.warning("android: waydroid didn't start (%s)", str(e)[:120])
                self._drop(s)
                return self._fail("failed")
            pid = getattr(s.handle, "pid", None)
            if isinstance(pid, int):
                self._own_pids.add(pid)
            if s.ended:  # stopped while starting
                await self._wait_teardowns()
                return self._fail("failed", log_it=False)
            free = self._memory()
            log.info("android: starting Android (from %s%s)%s", source, f", to open {app}" if app else "",
                     f"; {free} MB of memory free" if free is not None else "")
            self._publish(STARTED, {"source": source, "app": str(app or "")})
            self._spawn(s, self._read(s))
            self._spawn(s, self._exit_watch(s))
            self._spawn(s, self._bring_up(s))
            self._begin_watching(s)
            return self._ok(s)

    def _new_session(self, source: str, app: str | None) -> _Session:
        self._ids += 1
        s = _Session(id=self._ids, source=source, app=app, started=self._clock())
        with self._lock:
            self._session = s
        return s

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if hasattr(os, "getuid"):
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        return env

    async def _bring_up(self, s: _Session) -> None:
        """Once Android has started: its full screen UI, then the app asked for."""
        while not s.ended:
            if await self._booted():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.ready.wait(), self._boot_poll)
            s.ready.clear()
        if s.ended:
            return
        rc, _ = await self._waydroid("show-full-ui")
        if rc != 0:
            log.info("android: waydroid show-full-ui said exit %s", rc)
        if s.app and not s.ended:
            await self._launch_app(s.app)

    async def _booted(self) -> bool:
        rc, out = await self._waydroid("status", timeout=10.0)
        if rc != 0 or not any(line.split(":", 1)[0].strip() == "Session" and "RUNNING" in line
                              for line in out.splitlines()):
            return False
        rc, out = await self._waydroid("prop", "get", "sys.boot_completed", timeout=10.0)
        return rc == 0 and out.strip().splitlines()[-1:] == ["1"]

    async def _launch_app(self, package: str) -> None:
        rc, _ = await self._waydroid("app", "launch", package)
        log.info("android: opening %s%s", package, "" if rc == 0 else f" failed (exit {rc})")

    # ================================================================ a session LittleBot didn't start
    async def _adopt_loop(self) -> None:
        while True:
            await asyncio.sleep(self._adopt_every)
            try:
                await self._adopt()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the loop die
                log.debug("android: looking for an outside session failed", exc_info=True)

    async def _adopt(self) -> bool:
        """A Waydroid session someone started by hand: watched (and shut down off the screen) like LittleBot's own."""
        if self.active or any(not f.done() for f in self._teardowns):  # (one of ours may still be stopping)
            return False
        if self._path() is None:
            return False
        pid = await asyncio.to_thread(self._outside)
        if pid is None or pid in self._own_pids:
            return False
        async with self._start_lock:
            if self.active:
                return False
            s = self._new_session("outside LittleBot", None)
            s.pid = pid
            log.info("android: Android was started outside LittleBot; it is shut down too once it isn't on the "
                     "screen")
            if self.unavailable_reason() is not None or not self.switched_on:
                self.close_soon("Android is switched off on the dashboard")
                return True
            if not self._take_screen(s):
                self.close_soon("the screen was busy")
                return True
            self._publish(STARTED, {"source": s.source, "app": ""})
            self._begin_watching(s)
            return True

    # ================================================================ watching it
    def _begin_watching(self, s: _Session) -> None:
        try:
            s.inputs = self._inputs(self._on_input)
            s.inputs.start()
        except Exception:  # noqa: BLE001 - no touch watch: it still closes when the screen goes dark
            log.warning("android: watching the touch screen failed", exc_info=True)
            s.inputs = None
        self._spawn(s, self._watch(s))

    def _on_input(self) -> None:
        if self.active:
            self._publish(USED, {})

    async def _watch(self, s: _Session) -> None:
        """Every ``POLL_S``: is Android's window in front? Shut it down when it hasn't been for a while."""
        timeout = float(self.cfg.start_timeout_seconds)
        off_after = float(self.cfg.off_screen_seconds)
        while not s.ended:
            await asyncio.sleep(self._poll)
            if s.ended:
                return
            if s.pid is not None and not self._alive(s.pid):
                self.close_soon("Android stopped")
                return
            free = self._memory()
            if free is not None and free < int(self.cfg.min_free_mb_running):
                self.close_soon(f"memory ran low ({free} MB free)")
                return
            windows = await asyncio.to_thread(self._windows)
            where = where_android_is(windows, str(self.cfg.window_match))
            if where is None:
                if not self._blind_told:
                    self._blind_told = True
                    log.info("android: the desktop can't say which window is in front; Android is shut down when "
                             "the screen goes dark or something else takes it")
                where = "front"
            now = self._clock()
            if s.state == "starting":
                if where == "front":
                    self._showing(s, now)
                elif now - s.started >= timeout:
                    seen = ", ".join(sorted({w.app_id or "?" for w in windows or []}))[:120]
                    self.close_soon(f"its window didn't come within {timeout:.0f} s (the desktop's windows: "
                                    f"{seen or 'none'})")
                    return
                continue
            if where == "front":
                s.off_since = None
                continue
            if s.off_since is None:
                s.off_since = now
            if now - s.off_since >= off_after:
                self.close_soon("its window was closed" if where == "none" else "another window is in front of it")
                return

    def _showing(self, s: _Session, now: float) -> None:
        s.state, s.shown_at = "showing", now
        log.info("android: Android is on the screen (%.0f s after it was started)", now - s.started)
        self._publish(SHOWING, {})

    async def _read(self, s: _Session) -> None:
        """Everything ``waydroid session start`` prints (so its pipe never fills); "... is ready" = it has started."""
        try:
            async for line in s.handle.lines():
                log.debug("android waydroid: %s", line[:300])
                if READY_LINE in line:
                    s.ready.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("android: reading waydroid's output failed", exc_info=True)

    async def _exit_watch(self, s: _Session) -> None:
        code = await s.handle.wait()
        if not s.ended:
            self.close_soon(f"Android stopped (waydroid exit {code})")

    # ================================================================ the screen
    def _take_screen(self, s: _Session) -> bool:
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=self._preempted(s))
        if lease is None:
            return False
        s.lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], PRIORITY, on_preempt=self._preempted(s)) or lease
        return True

    def _preempted(self, s: _Session) -> Callable[[Lease, str], None]:
        def on_preempt(lease: Lease, by: str) -> None:
            if s.lease is None or s.lease is lease:
                self.close_soon(f"{by} took the screen")
        return on_preempt

    # ================================================================ stopping
    def close_soon(self, reason: str = "closed") -> bool:
        """Shut Android down, the face back. Any thread. True if it was running."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            s.ended = True
            s.reason = str(reason or "closed")[:160]
            self._session = None
        if s.lease is not None:
            s.lease.release()
        s.free_before = self._memory()
        log.info("android: shutting Android down (%s)", s.reason)
        self._publish(STOPPED, {"reason": s.reason})
        self._schedule_teardown(s)
        return True

    async def close(self, reason: str = "closed") -> bool:
        """``close_soon``, then wait until Android is off."""
        closed = self.close_soon(reason)
        await self._wait_teardowns()
        return closed

    def _drop(self, s: _Session) -> None:
        """A session that never got going: forget it (nothing was announced)."""
        with self._lock:
            if self._session is s:
                self._session = None
        s.ended = True
        if s.lease is not None:
            s.lease.release()

    async def _wait_teardowns(self) -> None:
        pending = [f for f in list(self._teardowns) if not f.done()]
        if pending:
            await asyncio.wait([asyncio.wrap_future(f) if not isinstance(f, asyncio.Future) else f
                                for f in pending], timeout=STOP_TIMEOUT_S + EXIT_WAIT_S + 10)

    def _schedule_teardown(self, s: _Session) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._teardown_now(s)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut: Any = loop.create_task(self._teardown(s))
        else:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._teardown(s), loop)
            except RuntimeError:
                self._teardown_now(s)
                return
        self._teardowns.add(fut)
        fut.add_done_callback(self._teardowns.discard)

    async def _teardown(self, s: _Session) -> None:
        current = asyncio.current_task()
        for t in list(s.tasks):
            if t is not current:
                t.cancel()
        if s.inputs is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(s.inputs.stop)
        rc, out = await self._waydroid("session", "stop", timeout=STOP_TIMEOUT_S)
        if rc != 0:
            log.info("android: waydroid session stop said exit %s (%s)", rc, out.strip()[:120])
        h = s.handle
        if h is not None and h.returncode is None:
            try:
                await asyncio.wait_for(h.wait(), EXIT_WAIT_S)
            except asyncio.TimeoutError:
                log.warning("android: waydroid didn't stop; ending it")
                h.terminate()
                try:
                    await asyncio.wait_for(h.wait(), TERMINATE_S)
                except asyncio.TimeoutError:
                    h.kill()
        await asyncio.sleep(self._settle)
        free = self._memory()
        seconds = max(0.0, self._clock() - s.started)
        back = None if free is None or s.free_before is None else free - s.free_before
        self._last = {"reason": s.reason, "seconds": round(seconds, 1), "at": time.time(), "free_mb": free,
                      "freed_mb": back, "source": s.source}
        log.info("android: Android is off (%s) after %.0f s%s", s.reason, seconds,
                 f"; {free} MB of memory free ({back:+d} MB)" if free is not None and back is not None else "")

    def _teardown_now(self, s: _Session) -> None:
        if s.handle is not None and s.handle.returncode is None:
            with contextlib.suppress(Exception):
                s.handle.terminate()

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        why = self.unavailable_reason()
        step = self.setup_step() if why is None else None
        apps = {str(k): str(v) for k, v in dict(self.cfg.apps or {}).items()}
        out: dict[str, Any] = {
            "available": why is None, "enabled": self.switched_on, "detail": why,
            "set_up": why is None and step is None, "setup_step": step, "setup_command": SETUP.get(step or ""),
            "setup_script": SETUP_SCRIPT, "kernel_ok": self._page_size() == PAGE_SIZE,
            "installed": self._path() is not None, "container": self._container,
            "active": False, "state": None, "source": None, "seconds": None, "apps": apps,
            "memory_free_mb": self._memory(), "min_free_mb": int(self.cfg.min_free_mb),
            "off_screen_seconds": float(self.cfg.off_screen_seconds),
            "last": dict(self._last) if self._last else None, "last_error": self.last_error}
        s = self._session
        if s is not None and not s.ended:
            out.update({"active": True, "state": s.state, "source": s.source,
                        "seconds": round(max(0.0, self._clock() - s.started), 1)})
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _CLOSE_STATES:
            self.close_soon({"SLEEPING": "LittleBot went to sleep", "ERROR": "an error"}.get(str(new),
                                                                                             f"state {new}"))

    def _on_screen(self, event: Event) -> None:
        if event.get("on") is False:
            self.close_soon(f"the screen went dark ({event.get('reason') or 'nothing happening'})")

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "android.enabled" and not event.get("value"):
            self.close_soon("switched off")

    # ================================================================ plumbing
    async def _run(self, argv: Sequence[str], *, timeout: float = COMMAND_TIMEOUT_S) -> tuple[int, str]:
        try:
            return await self._runner(list(argv), timeout, None)
        except (OSError, ValueError) as e:
            return 127, str(e)[:200]

    async def _waydroid(self, *args: str, timeout: float = COMMAND_TIMEOUT_S) -> tuple[int, str]:
        path = self._path()
        if path is None:
            return 127, "waydroid is not installed"
        return await self._run([path, *args], timeout=timeout)

    def _spawn(self, s: _Session, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        s.tasks.add(task)
        task.add_done_callback(s.tasks.discard)
        return task

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            self._loop = loop

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="android")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _ok(self, s: _Session, *, already: bool = False) -> dict[str, Any]:
        self.last_error = None
        return {"ok": True, "state": s.state, "error": None, "detail": "", "already": already}

    def _fail(self, code: str, *, log_it: bool = True) -> dict[str, Any]:
        detail = ERRORS.get(code, "")
        if code == "not_set_up":
            step = self.setup_step()
            detail = f"{detail} Next, on the robot: {SETUP.get(step or '', SETUP_SCRIPT)}"
        elif code == "low_memory":
            detail = f"{detail} It needs {int(self.cfg.min_free_mb)} MB; {self._memory()} MB are free."
        if log_it:
            self.last_error = code
            log.info("android: not started (%s)", code)
        return {"ok": False, "state": None, "error": code, "detail": detail, "already": False}


__all__ = ["ERRORS", "OWNER", "PRIORITY", "SETUP", "SHOWING", "STARTED", "STOPPED", "USED", "AndroidService",
           "free_mb", "outside_session", "where_android_is"]
