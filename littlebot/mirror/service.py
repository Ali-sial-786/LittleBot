"""MirrorService: a phone's screen on LittleBot's screen, while a grown-up asked for it.

Public API (``ctx.mirror``; used by the voice's tools, littlebot/skills/mirror.py, and the dashboard)::

    await mirror.start(kind, *, source="voice") -> {"ok", "kind", "state", "error", "detail", "already"}
        kind "iphone": UxPlay, the AirPlay receiver called LittleBot, opens and waits for an iPhone ("waiting");
        "android": a paired Android phone on the Wi-Fi is connected with adb and scrcpy shows it ("showing").
    await mirror.stop(reason) -> bool                 mirroring off, the program exited (True if it was on)
    mirror.stop_mirroring(reason) -> bool             the same from any thread or a bus callback (doesn't wait)
    await mirror.pair(address, code) -> {"ok", "error", "detail", "guid", "connected"}   Android, once per phone
    await mirror.forget(guid) -> bool                 a paired phone off LittleBot's list
    mirror.status(), mirror.check(kind), mirror.active, mirror.installed()

Error codes: "unavailable" (the config switch, no screen), "disabled" (the parent's switch), "not_installed",
"bad_kind", "asleep", "not_paired", "no_phone", "busy", "failed", "bad_input", "pair_failed". ``ERRORS`` has the words.

iPhone (UxPlay)
---------------
``uxplay -n LittleBot -nh -fs -avdec -vs waylandsink -s <w>x<h> -fps 30 [-as 0]``: full screen, the screen's size,
software H.264 decoding (the Pi 5 has no H.264 decoder chip), the Wayland video sink, no sound unless
``mirror.sound``. It runs only while mirroring was asked for: from the start until ``mirror.wait_seconds`` (90) pass
with no iPhone, or until the phone stops. Its output (stdout, line-buffered through ``stdbuf -oL`` when there is
one) tells what happens, from UxPlay's own log lines (1.71 / master source: lib/raop_rtp_mirror.c, uxplay.cpp):
``raop_rtp_mirror starting mirroring`` = a phone started mirroring (the screen is taken then, not while waiting,
so cards still show during the chat); ``raop_rtp_mirror->running is no longer true``, ``Open connections: 0`` or
``lost connection with client`` after that = it stopped. An iPhone that only looks at the receiver (``/info``)
opens and closes a connection without mirroring; that doesn't count.

Android (scrcpy over Wi-Fi)
---------------------------
Paired once on the dashboard (Android 11+: Developer options → Wireless debugging → Pair device with pairing
code: ``adb pair <ip>:<port>`` with the code on stdin). Then each time: ``adb devices`` (adb's own mDNS may have
connected the phone already), else ``adb mdns services`` and ``adb connect`` to each paired phone it sees
(Wireless debugging's port changes each time it is switched on, so the address is looked up, not kept), then
``scrcpy --serial=<phone> --fullscreen --window-borderless --max-size=<n> --video-bit-rate=<b> --max-fps=<f>
--no-audio --no-control`` (sound and taps on LittleBot's screen only when the config says so; ``--stay-awake``
needs control). scrcpy exits when the phone goes away or stops sharing: the face comes back.

The screen, never the speaker
-----------------------------
One lease on ``Resource.SCREEN``, taken at ``Priority.MEDIA`` (a card or game on the screen goes) and held at
``PRIORITY`` (45), like the browser view: a game (50) or a video (60) takes it and mirroring stops. LittleBot's
speaker and microphone are never taken, so a voice chat keeps working ("stop mirroring", "stop"). A playing video
and a running activity stop first. Phone sound (``mirror.sound``) goes to the system's default output directly.

It stops on STOP ALL, sleep, an error state, a video, game or activity starting, the lease being taken, the
parent's switch, its process exiting, the dashboard's Stop, ``stop_mirroring``, and (iPhone) no phone in time.

Logs: one INFO line per open, per phone starting to mirror, and per close (the kind, the source, the reason; never
what the programs print, which is at DEBUG). Bus: ``mirror.started`` {kind, source}, ``mirror.showing`` {kind},
``mirror.stopped`` {kind, reason}; ``screen.keep_awake`` {kind: "mirror"} every 30 s while a phone shows.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.mirror import adb as A

log = logging.getLogger("littlebot.mirror")

OWNER = "mirror"
PRIORITY = int(Priority.CONVERSATION) + 5  # above a card (40), below a game (50) and a video (60): as the browser
KINDS = ("iphone", "android")
STARTED = "mirror.started"  # {kind, source}
SHOWING = "mirror.showing"  # {kind}
STOPPED = "mirror.stopped"  # {kind, reason}
KEEP_AWAKE = "screen.keep_awake"  # {kind}: LittleBot doesn't doze off on a phone's screen
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
KEEP_AWAKE_EVERY_S = 30.0
TERMINATE_S = 2.0  # SIGTERM -> this long -> SIGKILL
ADB_TIMEOUT_S = 8.0
PAIR_TIMEOUT_S = 20.0
MAX_PHONES = 10
MAX_LINE = 300
_CLOSE_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value,
                 RobotState.PLAYING_ACTIVITY.value}
ALIASES = {"iphone": "iphone", "ios": "iphone", "apple": "iphone", "ipad": "iphone", "airplay": "iphone",
           "android": "android", "samsung": "android", "pixel": "android", "google": "android"}

# UxPlay's log lines (stdout): a phone started mirroring / stopped.
UXPLAY_MIRRORING = re.compile(r"raop_rtp_mirror starting mirroring")
UXPLAY_ENDED = re.compile(r"raop_rtp_mirror->running is no longer true|Open connections: 0\b|"
                          r"lost connection with client")
UXPLAY_KNOCK = re.compile(r"connection request from|Open connections: [1-9]")

# What to install (Debian trixie / Raspberry Pi OS). scrcpy is only in trixie-backports (contrib).
BACKPORTS = ("echo 'deb http://deb.debian.org/debian trixie-backports main contrib' | "
             "sudo tee /etc/apt/sources.list.d/trixie-backports.list && sudo apt update")
INSTALL = {
    "uxplay": "sudo apt install uxplay gstreamer1.0-libav gstreamer1.0-plugins-bad gstreamer1.0-plugins-good",
    "adb": "sudo apt install adb",
    "scrcpy": f"{BACKPORTS} && sudo apt install -t trixie-backports scrcpy",
}
SETUP_SCRIPT = "bash scripts/setup_mirroring.sh"
NEEDS = {"iphone": ("uxplay",), "android": ("scrcpy", "adb")}

ERRORS = {
    "unavailable": "Phone mirroring isn't available on this robot.",
    "disabled": "Phone mirroring is switched off on the dashboard (Assistant page).",
    "not_installed": "The program for that phone isn't installed on LittleBot yet.",
    "bad_kind": "LittleBot can mirror an iPhone or an Android phone.",
    "asleep": "LittleBot is asleep.",
    "not_paired": "No Android phone is paired yet: pair it once on the dashboard (Assistant page, Phone mirroring).",
    "no_phone": ("No paired Android phone answered on the Wi-Fi. On the phone, switch Wireless debugging on "
                 "(Settings, Developer options) and keep it on the same Wi-Fi."),
    "busy": "LittleBot's screen is busy with something more important right now.",
    "failed": "Mirroring didn't start.",
    "bad_input": "That isn't a pairing address (like 192.168.1.23:37099) and a six-digit code.",
    "pair_failed": "The phone didn't accept the pairing. Check the code and the address, and try again quickly.",
}

# ---------------------------------------------------------------- processes
_LIVE: "weakref.WeakSet[Any]" = weakref.WeakSet()


@atexit.register
def _terminate_live() -> None:
    for handle in list(_LIVE):
        with contextlib.suppress(Exception):
            handle.terminate()


class _MirrorProcess:
    """A program in its own process group (the whole tree is signalled), its output read line by line."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        from littlebot.media.display import _GroupProcess  # noqa: PLC0415 - the videos' kiosk pattern

        self._proc = proc
        self._group = _GroupProcess(proc)
        self.pid = proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def terminate(self) -> None:
        self._group.terminate()

    def kill(self) -> None:
        self._group.kill()

    async def wait(self) -> int:
        return await self._proc.wait()

    async def lines(self) -> AsyncIterator[str]:
        stream = self._proc.stdout
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except (ValueError, OSError):  # a line longer than the buffer: carry on with the rest
                continue
            if not raw:
                return
            yield raw.decode("utf-8", "replace").rstrip()


Launcher = Callable[[Sequence[str], "dict[str, str] | None"], Awaitable[Any]]


async def default_launcher(argv: Sequence[str], env: dict[str, str] | None = None) -> _MirrorProcess:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, env=env)
    return _MirrorProcess(proc)


@dataclass(eq=False)
class _Session:
    id: int
    kind: str
    source: str
    started: float
    state: str = "starting"  # starting | waiting (iPhone: the receiver is open) | showing
    serial: str = ""  # Android: the phone adb talks to
    handle: Any = None
    lease: Lease | None = None
    ended: bool = False
    reason: str = ""
    shown_at: float | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class MirrorService(Service):
    name = "mirror"

    def __init__(self, ctx: Any, *, launcher: Launcher | None = None, runner: A.Runner | None = None,
                 which: Callable[[str], str | None] = shutil.which, clock: Callable[[], float] = time.monotonic,
                 screen: bool | None = None, wait_seconds: float | None = None,
                 keep_awake_every: float = KEEP_AWAKE_EVERY_S) -> None:
        """``launcher`` starts uxplay / scrcpy, ``runner`` runs adb's short commands (tests fake both).
        ``screen``: whether there is a screen to show on (default: not in a headless run)."""
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.mirror
        self._launcher = launcher or default_launcher
        self._runner = runner or A.default_runner
        self._which = which
        self._clock = clock
        self._screen = (not ctx.config.display.headless) if screen is None else bool(screen)
        self._wait_s = float(self.cfg.wait_seconds) if wait_seconds is None else float(wait_seconds)
        self._awake_every = float(keep_awake_every)
        self._lock = threading.RLock()
        self._start_lock = asyncio.Lock()
        self._session: _Session | None = None
        self._ids = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._teardowns: set[Any] = set()
        self._last: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._subs: list[Subscription] = []

    # ================================================================ lifecycle
    async def start(self, kind: str | None = None, *, source: str = "voice") -> Any:  # type: ignore[override]
        """``await start("iphone" | "android", source=...)``: start mirroring (see ``begin``). With no kind: the
        subsystem starts (``app/robot.py``)."""
        if kind is not None:
            return await self.begin(kind, source=source)
        await self._start_service()

    async def stop(self, reason: str | None = None) -> Any:  # type: ignore[override]
        """``await stop("why")``: mirroring off, and wait for the program to exit (True if it was on). With no
        reason: the subsystem shuts down. From another thread or a bus callback: ``stop_mirroring(reason)``."""
        if reason is not None:
            return await self.astop(reason)
        await self.astop("shutdown")
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)
        return None

    async def _start_service(self) -> None:
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, lambda _e: self.stop_mirroring("stop all")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.stop_mirroring("a video started")),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.stop_mirroring("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.stop_mirroring("a game started")),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
            ]
        why = self.unavailable_reason()
        if why is None:
            missing = [t for t, ok in self.installed().items() if not ok]
            self.set_status(ServiceStatus.RUNNING, f"not installed: {', '.join(missing)}" if missing else "")
        elif not self.cfg.enabled:  # switched off: as the browser view does
            self.set_status(ServiceStatus.DISABLED, why)
        else:  # no screen: degraded, like the browser view without one
            self.set_status(ServiceStatus.DEGRADED, why)

    # ================================================================ what is installed
    def _path(self, tool: str) -> str | None:
        cmd = str(getattr(self.cfg, f"{tool}_command", tool) or tool)
        if os.path.isabs(cmd):
            return cmd if os.access(cmd, os.X_OK) else None
        return self._which(cmd)

    def installed(self) -> dict[str, bool]:
        return {t: self._path(t) is not None for t in ("uxplay", "scrcpy", "adb")}

    def install_command(self, kind: str | None = None) -> str | None:
        """The exact command(s) that install what is missing (for ``kind``, or both), or None."""
        have = self.installed()
        tools = NEEDS.get(kind, ()) if kind else ("uxplay", "adb", "scrcpy")
        missing = [t for t in dict.fromkeys(tools) if not have[t]]
        if not missing:
            return None
        apt = [t for t in missing if t != "scrcpy"]
        parts = []
        if apt:
            pkgs = " ".join(INSTALL[t].removeprefix("sudo apt install ") for t in apt)
            parts.append(f"sudo apt install {pkgs}")
        if "scrcpy" in missing:
            parts.append(INSTALL["scrcpy"])
        return " && ".join(parts)

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (mirror.enabled)"
        if not self._screen:
            return "no screen (headless run)"
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get("mirror.enabled", True))

    def offered(self) -> bool:
        """The voice may offer mirroring (it exists and the parent's switch is on)."""
        return self.unavailable_reason() is None and self.switched_on

    def check(self, kind: str) -> str | None:
        """Why ``kind`` can't start now (an ``ERRORS`` code), else None. Cheap: nothing is run."""
        if kind not in KINDS:
            return "bad_kind"
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        if any(self._path(t) is None for t in NEEDS[kind]):
            return "not_installed"
        if self.ctx.state.state in (RobotState.SLEEPING, RobotState.ERROR):
            return "asleep"
        return None

    @staticmethod
    def kind_of(text: Any) -> str | None:
        return ALIASES.get(str(text or "").strip().lower().replace(" ", ""))

    # ================================================================ starting
    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    async def begin(self, kind: str, *, source: str = "voice") -> dict[str, Any]:
        """Start mirroring a phone of ``kind`` ("iphone" | "android"); see the module docstring."""
        self._bind_loop()
        source = str(source or "voice")[:20]
        k = self.kind_of(kind)
        why = self.check(k or "")
        if why is not None:
            return self._fail(why, kind=k)
        assert k is not None
        async with self._start_lock:
            s = self._session
            if s is not None and not s.ended:
                if s.kind == k:
                    return self._ok(s, already=True)
                await self.astop("switching phones")
            if k == "iphone":
                return await self._start_iphone(source)
            return await self._start_android(source)

    def _new_session(self, kind: str, source: str) -> _Session:
        self._ids += 1
        s = _Session(id=self._ids, kind=kind, source=source, started=self._clock())
        with self._lock:
            self._session = s
        return s

    async def _start_iphone(self, source: str) -> dict[str, Any]:
        s = self._new_session("iphone", source)
        argv = self.uxplay_command()
        try:
            s.handle = await self._launcher(argv, None)
        except (OSError, ValueError) as e:
            log.warning("mirror: uxplay didn't start (%s)", str(e)[:120])
            self._drop(s)
            return self._fail("failed", kind="iphone")
        _LIVE.add(s.handle)
        if s.ended:  # stopped while starting
            await self._wait_teardowns()
            await self._teardown(s)
            return self._fail("failed", kind="iphone", log_it=False)
        s.state = "waiting"
        self._spawn(s, self._read(s))
        self._spawn(s, self._exit_watch(s))
        self._spawn(s, self._wait_for_phone(s))
        log.info("mirror: the AirPlay receiver %r is open, waiting up to %.0f s for an iPhone (from %s)",
                 self.cfg.airplay_name, self._wait_s, source)
        self._publish(STARTED, {"kind": "iphone", "source": source})
        return self._ok(s)

    async def _start_android(self, source: str) -> dict[str, Any]:
        serial = await self._find_phone()
        if not serial:
            return self._fail("no_phone" if self.phones() else "not_paired", kind="android")
        await self._make_room()
        s = self._new_session("android", source)
        s.serial = serial
        if not self._take_screen(s):
            self._drop(s)
            return self._fail("busy", kind="android")
        env = dict(os.environ)
        adb_path = self._path("adb")
        if adb_path:
            env["ADB"] = adb_path  # scrcpy runs this adb
        try:
            s.handle = await self._launcher(self.scrcpy_command(serial), env)
        except (OSError, ValueError) as e:
            log.warning("mirror: scrcpy didn't start (%s)", str(e)[:120])
            self._drop(s)
            return self._fail("failed", kind="android")
        _LIVE.add(s.handle)
        if s.ended:
            await self._wait_teardowns()
            await self._teardown(s)
            return self._fail("failed", kind="android", log_it=False)
        self._spawn(s, self._read(s))
        self._spawn(s, self._exit_watch(s))
        log.info("mirror: an Android phone is on the screen (from %s)", source)
        self._publish(STARTED, {"kind": "android", "source": source})
        self._showing(s)
        return self._ok(s)

    def _size(self) -> tuple[int, int]:
        w, h = int(self.ctx.config.display.width), int(self.ctx.config.display.height)
        top = int(self.cfg.max_size)
        if top and max(w, h) != top:
            f = top / max(w, h)
            w, h = int(round(w * f / 2) * 2), int(round(h * f / 2) * 2)
        return max(w, 16), max(h, 16)

    def uxplay_command(self) -> list[str]:
        w, h = self._size()
        argv = [self._path("uxplay") or "uxplay", "-n", str(self.cfg.airplay_name), "-nh", "-fs", "-avdec",
                "-vs", str(self.cfg.video_sink), "-s", f"{w}x{h}", "-fps", str(int(self.cfg.fps))]
        if not self.cfg.sound:
            argv += ["-as", "0"]
        argv += [str(a) for a in self.cfg.extra_uxplay_args]
        stdbuf = self._which("stdbuf")
        return [stdbuf, "-oL", *argv] if stdbuf else argv  # its log lines as they happen, not in 4 KB blocks

    def scrcpy_command(self, serial: str) -> list[str]:
        argv = [self._path("scrcpy") or "scrcpy", f"--serial={serial}", "--fullscreen", "--window-borderless",
                "--window-title=LittleBot", f"--max-size={max(self._size())}",
                f"--video-bit-rate={int(float(self.cfg.android_bit_rate_mbps) * 1_000_000)}",
                f"--max-fps={int(self.cfg.fps)}"]
        if not self.cfg.sound:
            argv.append("--no-audio")
        if self.cfg.touch_control:
            argv.append("--stay-awake")
        else:
            argv.append("--no-control")
        return argv + [str(a) for a in self.cfg.extra_scrcpy_args]

    # ================================================================ the screen
    def _take_screen(self, s: _Session) -> bool:
        # Taken like a video (a card on the screen goes), then held lower so a game or a video wins later.
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=self._preempted(s))
        if lease is None:
            return False
        s.lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], PRIORITY, on_preempt=self._preempted(s)) or lease
        return True

    def _preempted(self, s: _Session) -> Callable[[Lease, str], None]:
        def on_preempt(lease: Lease, by: str) -> None:
            if s.lease is None or s.lease is lease:
                self.stop_mirroring(f"{by} took the screen")
        return on_preempt

    async def _make_room(self) -> None:
        """A playing video and a running activity stop first (as for the browser view)."""
        media = getattr(self.ctx, "media", None)
        try:
            st = media.status() if media is not None else None
        except Exception:  # noqa: BLE001
            st = None
        if isinstance(st, dict) and not st.get("held") and (
                st.get("state") not in (None, "idle") or (st.get("player") or {}).get("running")):
            log.info("mirror: stopping the video first")
            try:
                await media.stop("stopped")
            except Exception:  # noqa: BLE001
                log.exception("mirror: stopping the video failed")
        acts = getattr(self.ctx, "activities", None)
        try:
            running = acts is not None and bool(acts.status().get("running"))
        except Exception:  # noqa: BLE001
            running = False
        if running:
            try:
                await acts.stop("phone mirroring started")
            except Exception:  # noqa: BLE001
                log.exception("mirror: stopping the activity failed")

    def _showing(self, s: _Session) -> None:
        s.state, s.shown_at = "showing", self._clock()
        self._publish(SHOWING, {"kind": s.kind})
        self._publish(KEEP_AWAKE, {"kind": "mirror"})
        self._spawn(s, self._keep_awake(s))

    async def _keep_awake(self, s: _Session) -> None:
        while not s.ended:
            await asyncio.sleep(self._awake_every)
            if not s.ended:
                self._publish(KEEP_AWAKE, {"kind": "mirror"})

    # ================================================================ watching the programs
    async def _read(self, s: _Session) -> None:
        """Every line the program prints (so its pipe never fills); UxPlay's say what the phone does."""
        try:
            async for line in s.handle.lines():
                if s.ended:
                    return
                line = line[:MAX_LINE]
                log.debug("mirror %s: %s", s.kind, line)
                if s.kind == "iphone":
                    await self._uxplay_line(s, line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("mirror: reading the program's output failed", exc_info=True)

    async def _uxplay_line(self, s: _Session, line: str) -> None:
        if UXPLAY_MIRRORING.search(line):
            if s.state == "showing":
                return
            await self._make_room()
            if s.ended:
                return
            if not self._take_screen(s):
                self.stop_mirroring("the screen was busy")
                return
            log.info("mirror: an iPhone started mirroring")
            self._showing(s)
        elif UXPLAY_ENDED.search(line) and s.state == "showing":
            self.stop_mirroring("the phone stopped mirroring")
        elif UXPLAY_KNOCK.search(line) and s.state == "waiting":
            log.debug("mirror: a phone is talking to the receiver")

    async def _exit_watch(self, s: _Session) -> None:
        code = await s.handle.wait()
        if s.ended:
            return
        if s.kind == "android":
            self.stop_mirroring("the phone went away" if s.shown_at is not None else f"scrcpy exited ({code})")
        else:
            self.stop_mirroring(f"uxplay exited ({code})")

    async def _wait_for_phone(self, s: _Session) -> None:
        await asyncio.sleep(self._wait_s)
        if not s.ended and s.state == "waiting":
            self.stop_mirroring("no phone came")

    # ================================================================ Android: pairing and finding the phone
    def phones(self) -> list[dict[str, Any]]:
        got = self.ctx.settings.get("mirror.android_phones", [])
        return [p for p in got if isinstance(p, dict)] if isinstance(got, list) else []

    async def _adb(self, *args: str, timeout: float = ADB_TIMEOUT_S, stdin: bytes | None = None) -> tuple[int, str]:
        path = self._path("adb")
        if path is None:
            return 127, "adb is not installed"
        try:
            return await self._runner([path, *args], timeout, stdin)
        except (OSError, ValueError) as e:
            return 127, str(e)[:200]

    async def _wifi_devices(self) -> list[str]:
        _, out = await self._adb("devices")
        return [serial for serial, state in A.parse_devices(out) if state == "device" and A.is_wifi(serial)]

    async def _find_phone(self) -> str | None:
        """A paired phone on the Wi-Fi that adb is connected to (connecting it first), or None."""
        await self._adb("start-server", timeout=15.0)
        found = await self._wifi_devices()
        if not found:
            _, out = await self._adb("mdns", "services")
            for _name, address in A.parse_mdns(out)[:MAX_PHONES]:
                _, said = await self._adb("connect", address)
                if A.connected(said):
                    log.info("mirror: connected to a paired Android phone on the Wi-Fi")
            found = await self._wifi_devices()
        if not found:
            log.info("mirror: no paired Android phone answered on the Wi-Fi")
            return None
        guids = {str(p.get("guid") or "") for p in self.phones()} - {""}
        return next((d for d in found if any(d.startswith(g) for g in guids)), found[0])

    async def pair(self, address: Any, code: Any, *, name: str = "") -> dict[str, Any]:
        """Pair an Android phone once (Wireless debugging → Pair device with pairing code)."""
        addr, pin = A.check_address(address), A.check_code(code)
        if addr is None or pin is None:
            return self._fail("bad_input", kind="android", log_it=False)
        why = self.check("android")
        if why is not None and why != "asleep":
            return self._fail(why, kind="android")
        rc, out = await self._adb("pair", addr, timeout=PAIR_TIMEOUT_S, stdin=(pin + "\n").encode("ascii"))
        paired, guid = A.parse_pair(out)
        if not paired:
            log.info("mirror: pairing an Android phone failed (adb exit %s)", rc)
            return self._fail("pair_failed", kind="android")
        ip = addr.rsplit(":", 1)[0]
        phones = [p for p in self.phones() if not (guid and p.get("guid") == guid) and p.get("address") != ip]
        clean = re.sub(r"[^\w '\-.]", "", str(name or ""))[:40].strip()
        phones.append({"guid": guid, "address": ip, "name": clean or f"Android phone ({ip})",
                       "paired_at": round(time.time())})
        self.ctx.settings.set("mirror.android_phones", phones[-MAX_PHONES:], source="mirror")
        log.info("mirror: an Android phone was paired")
        found = await self._find_phone()
        return {"ok": True, "error": None, "detail": "", "guid": guid, "connected": bool(found)}

    async def forget(self, key: Any) -> bool:
        """Take a paired phone (its guid or address) off the list and disconnect it. True if it was there."""
        key = str(key or "").strip()
        phones = self.phones()
        keep = [p for p in phones if key not in (p.get("guid"), p.get("address"))]
        gone = [p for p in phones if p not in keep]
        if not key or not gone:
            return False
        self.ctx.settings.set("mirror.android_phones", keep, source="mirror")
        s = self._session
        for p in gone:
            if s is not None and not s.ended and s.kind == "android" and (
                    (p.get("guid") and s.serial.startswith(str(p["guid"]))) or s.serial.startswith(f"{p.get('address')}:")):
                await self.astop("the phone was forgotten")
            for serial in await self._wifi_devices():
                if (p.get("guid") and serial.startswith(str(p["guid"]))) or serial.startswith(f"{p.get('address')}:"):
                    await self._adb("disconnect", serial)
        log.info("mirror: a paired Android phone was forgotten")
        return True

    # ================================================================ stopping
    def stop_mirroring(self, reason: str = "stopped") -> bool:
        """Mirroring off, the face back (the program exits). Any thread. True if it was on."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            s.ended = True
            s.reason = str(reason or "stopped")[:80]
            self._session = None
        if s.lease is not None:
            s.lease.release()
        seconds = max(0.0, self._clock() - s.started)
        self._last = {"kind": s.kind, "reason": s.reason, "seconds": round(seconds, 1), "at": time.time()}
        log.info("mirror: %s stopped (%s) after %.0f s", s.kind, s.reason, seconds)
        self._publish(STOPPED, {"kind": s.kind, "reason": s.reason})
        self._schedule_teardown(s)
        return True

    async def astop(self, reason: str = "stopped") -> bool:
        """``stop``, then wait until the program has exited."""
        stopped = self.stop_mirroring(reason)
        await self._wait_teardowns()
        return stopped

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
                                for f in pending], timeout=TERMINATE_S + 3)

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
        h = s.handle
        if h is None:
            return
        _LIVE.discard(h)
        if h.returncode is not None:
            return
        try:
            h.terminate()
            await asyncio.wait_for(h.wait(), TERMINATE_S)
        except asyncio.TimeoutError:
            log.warning("mirror: %s didn't exit after SIGTERM; killing it", s.kind)
            h.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(h.wait(), 1.0)
        except Exception:  # noqa: BLE001
            log.exception("mirror: stopping the program failed")

    def _teardown_now(self, s: _Session) -> None:
        if s.handle is not None and s.handle.returncode is None:
            with contextlib.suppress(Exception):
                s.handle.terminate()

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        why = self.unavailable_reason()
        have = self.installed()
        kinds = {k: {"installed": all(have[t] for t in NEEDS[k]), "install": self.install_command(k)} for k in KINDS}
        out: dict[str, Any] = {
            "available": why is None, "enabled": self.switched_on, "detail": why, "tools": have, "kinds": kinds,
            "install_command": self.install_command(), "setup_script": SETUP_SCRIPT,
            "active": False, "kind": None, "state": None, "seconds": None, "waits_left": None,
            "android_phones": [{k: p.get(k) for k in ("guid", "address", "name", "paired_at")} for p in self.phones()],
            "airplay_name": self.cfg.airplay_name, "wait_seconds": self._wait_s, "sound": bool(self.cfg.sound),
            "touch_control": bool(self.cfg.touch_control),
            "last": dict(self._last) if self._last else None, "last_error": self.last_error}
        s = self._session
        if s is not None and not s.ended:
            now = self._clock()
            out.update({"active": True, "kind": s.kind, "state": s.state, "source": s.source,
                        "seconds": round(max(0.0, now - s.started), 1)})
            if s.state == "waiting":
                out["waits_left"] = round(max(0.0, self._wait_s - (now - s.started)), 1)
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _CLOSE_STATES:
            self.stop_mirroring({"SLEEPING": "LittleBot went to sleep", "ERROR": "an error"}.get(str(new),
                                                                                                 f"state {new}"))

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "mirror.enabled" and not event.get("value"):
            self.stop_mirroring("switched off")

    # ================================================================ plumbing
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
            self.ctx.bus.publish(topic, data, source="mirror")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _ok(self, s: _Session, *, already: bool = False) -> dict[str, Any]:
        self.last_error = None
        return {"ok": True, "kind": s.kind, "state": s.state, "error": None, "detail": "", "already": already}

    def _fail(self, code: str, *, kind: str | None = None, log_it: bool = True) -> dict[str, Any]:
        detail = ERRORS.get(code, "")
        if code == "not_installed" and kind:
            detail = f"{detail} Install it with: {self.install_command(kind)}"
        if log_it:
            self.last_error = code
            log.info("mirror: %s not started (%s)", kind or "phone", code)
        return {"ok": False, "kind": kind, "state": None, "error": code, "detail": detail, "already": False}


__all__ = ["ERRORS", "INSTALL", "KINDS", "OWNER", "PRIORITY", "SHOWING", "STARTED", "STOPPED", "MirrorService",
           "default_launcher"]
