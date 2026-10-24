"""Where the YouTube player page is shown.

* :class:`BrowserKioskBackend`  - on the robot: launches Chromium full-screen (kiosk) on the
  480x320 screen pointing at the local player page, and terminates it when playback stops
  so the animated face is visible again.
* :class:`DesktopBrowserBackend` - desktop simulation: opens the page in the default browser
  (``webbrowser``). It cannot be closed programmatically; the page shows a calm
  "All done!" screen instead.
* :class:`NoneBackend`          - nothing is launched; a parent opens ``/player`` manually
  (the URL is in ``media.status()["player"]["url"]``). Used in tests, offline and headless runs.

Nothing here blocks the asyncio loop: processes are started with asyncio subprocesses and
``webbrowser`` runs in a worker thread.
"""

from __future__ import annotations

import abc
import asyncio
import atexit
import logging
import os
import shutil
import signal
import subprocess
import webbrowser
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from littlebot.core.config import LittleBotConfig

log = logging.getLogger("littlebot.media.display")

BROWSER_CANDIDATES: tuple[str, ...] = ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable")


class PlayerBackendError(RuntimeError):
    """The player window could not be opened (e.g. Chromium missing)."""


# Safety net: kiosk browsers still running when the interpreter exits (crash, unhandled exit)
# get SIGTERM to their whole process group. A clean stop() has already closed them.
_LIVE_KIOSKS: "weakref.WeakSet[BrowserKioskBackend]" = weakref.WeakSet()


@atexit.register
def _terminate_live_kiosks() -> None:
    for kiosk in list(_LIVE_KIOSKS):
        kiosk.terminate_now()


class ProcessHandle(Protocol):
    pid: int

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


Launcher = Callable[[Sequence[str]], Awaitable[ProcessHandle]]


class DisplayBackend(abc.ABC):
    name: str = "none"
    can_close: bool = False  # True when close() really removes the player from the screen

    def __init__(self) -> None:
        self.detail = ""
        self.last_url: str | None = None

    @property
    def available(self) -> bool:
        return True

    @abc.abstractmethod
    async def open(self, url: str) -> None:
        """Show the player page. Raises PlayerBackendError if impossible."""

    async def close(self) -> None:
        """Remove the player from the screen (no-op where impossible)."""

    def is_running(self) -> bool | None:
        """True/False when the backend knows whether its window is alive, None when it can't tell."""
        return None

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "available": self.available, "can_close": self.can_close,
                "running": self.is_running(), "detail": self.detail}


class NoneBackend(DisplayBackend):
    name = "none"

    def __init__(self, reason: str = "open the player page manually") -> None:
        super().__init__()
        self.detail = reason

    async def open(self, url: str) -> None:
        self.last_url = url
        log.info("media player backend 'none': open the player page manually")


class DesktopBrowserBackend(DisplayBackend):
    name = "browser"

    def __init__(self, opener: Callable[..., bool] | None = None) -> None:
        super().__init__()
        self._opener = opener or webbrowser.open
        self.detail = "opens the default web browser (close the tab yourself)"

    async def open(self, url: str) -> None:
        self.last_url = url
        try:
            ok = await asyncio.to_thread(self._opener, url, 2)
        except Exception as e:  # noqa: BLE001
            raise PlayerBackendError(f"could not open a web browser: {e}") from e
        if ok is False:
            raise PlayerBackendError("no web browser is available on this computer")


class _GroupProcess:
    """asyncio subprocess started in its own session, so the whole Chromium process tree can be signalled."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def _signal(self, sig: int) -> None:
        if self._proc.returncode is not None:
            return
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError, AttributeError, OSError):
            try:
                self._proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    async def wait(self) -> int:
        return await self._proc.wait()


async def default_launcher(argv: Sequence[str]) -> ProcessHandle:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return _GroupProcess(proc)


def find_browser(preferred: str | None = None, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Absolute path of the configured browser, else the first Chromium-family browser found."""
    if preferred:
        if os.path.isabs(preferred):
            return preferred if os.access(preferred, os.X_OK) else None
        return which(preferred)
    for name in BROWSER_CANDIDATES:
        path = which(name)
        if path:
            return path
    return None


def build_kiosk_command(
    browser: str, url: str, *, user_data_dir: str | Path, width: int = 480, height: int = 320,
    extra_args: Sequence[str] = (), kiosk: bool = True,
) -> list[str]:
    """Chromium command line for a locked-down, full-screen player window (``kiosk=False``: an app window of
    ``width`` x ``height`` instead, for the desktop simulator's browser view). Later switches in ``extra_args``
    override earlier ones."""
    return [
        browser,
        *(["--kiosk"] if kiosk else []),
        f"--app={url}",
        f"--window-size={int(width)},{int(height)}",
        "--window-position=0,0",
        "--force-device-scale-factor=1",  # small player -> YouTube picks a small (cheap) stream
        "--autoplay-policy=no-user-gesture-required",  # the parent's tap on the dashboard is the "gesture"
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-sync",
        "--disable-translate",
        "--disable-features=Translate,MediaRouter,GlobalMediaControls",
        "--disable-pinch",
        "--overscroll-history-navigation=0",
        "--incognito",  # no cookies / history persisted on the robot
        "--password-store=basic",
        "--disk-cache-size=33554432",  # 32 MB cap, protects the SD card
        "--check-for-update-interval=31536000",
        "--ozone-platform-hint=auto",  # X11 or Wayland (Raspberry Pi OS Bookworm uses Wayland)
        f"--user-data-dir={user_data_dir}",
        *extra_args,
    ]


class BrowserKioskBackend(DisplayBackend):
    name = "kiosk"
    can_close = True

    def __init__(
        self,
        browser: str | None,
        *,
        user_data_dir: str | Path,
        width: int = 480,
        height: int = 320,
        extra_args: Sequence[str] = (),
        launcher: Launcher | None = None,
        terminate_timeout: float = 2.0,  # SIGTERM -> wait -> SIGKILL -> wait 1 s: < 5 s shutdown budget
        kiosk: bool = True,
        label: str = "player kiosk",  # for the log ("browser view" for littlebot/browser)
    ) -> None:
        super().__init__()
        self.browser = browser
        self.kiosk = kiosk
        self.label = label
        self.user_data_dir = Path(user_data_dir)
        self.width, self.height = width, height
        self.extra_args = list(extra_args)
        self._launcher = launcher or default_launcher
        self._terminate_timeout = terminate_timeout
        self._proc: ProcessHandle | None = None
        self._lock = asyncio.Lock()
        self.detail = f"Chromium kiosk ({browser})" if browser else "Chromium not found (install chromium-browser)"
        self.last_command: list[str] | None = None

    @property
    def available(self) -> bool:
        return self.browser is not None

    def command(self, url: str) -> list[str]:
        if self.browser is None:
            raise PlayerBackendError("Chromium is not installed, so videos can't be shown on LittleBot's screen")
        return build_kiosk_command(self.browser, url, user_data_dir=self.user_data_dir, width=self.width,
                                   height=self.height, extra_args=self.extra_args, kiosk=self.kiosk)

    def is_running(self) -> bool | None:
        return self._proc is not None and self._proc.returncode is None

    async def open(self, url: str) -> None:
        async with self._lock:
            self.last_url = url
            if self.is_running():
                return  # the page loads new items itself
            argv = self.command(url)
            try:
                await asyncio.to_thread(self.user_data_dir.mkdir, parents=True, exist_ok=True)
            except OSError as e:
                log.warning("could not create %s: %s", self.user_data_dir, e)
            self.last_command = argv
            try:
                self._proc = await self._launcher(argv)
            except (OSError, ValueError) as e:
                self._proc = None
                raise PlayerBackendError(f"could not start the player browser: {e}") from e
            _LIVE_KIOSKS.add(self)
            log.info("%s started (pid %s)", self.label, getattr(self._proc, "pid", "?"))

    def terminate_now(self) -> None:
        """Synchronous best-effort SIGTERM (no event loop needed; used at interpreter exit)."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        async with self._lock:
            proc, self._proc = self._proc, None
            _LIVE_KIOSKS.discard(self)
            if proc is None or proc.returncode is not None:
                return
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), self._terminate_timeout)
            except asyncio.TimeoutError:
                log.warning("%s did not exit after SIGTERM; killing it", self.label)
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), 1.0)
                except asyncio.TimeoutError:
                    log.error("%s process %s did not exit", self.label, getattr(proc, "pid", "?"))
            log.info("%s closed", self.label)

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["pid"] = getattr(self._proc, "pid", None) if self.is_running() else None
        if self.browser and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            out["warning"] = "no DISPLAY / WAYLAND_DISPLAY in the environment; the kiosk window may not appear"
        return out


def select_backend(
    config: LittleBotConfig,
    *,
    is_pi: bool | None = None,
    which: Callable[[str], str | None] = shutil.which,
    launcher: Launcher | None = None,
    opener: Callable[..., bool] | None = None,
) -> DisplayBackend:
    """Pick the backend for ``config.media.player`` (auto: Pi -> kiosk, desktop -> browser, headless -> none)."""
    media = config.media
    choice = media.player
    if config.is_offline:
        return NoneBackend("offline mode: YouTube needs the internet")
    if choice == "auto":
        if config.display.headless:
            return NoneBackend("headless run: open the player page manually")
        if is_pi is None:
            from littlebot.hardware.platform import is_raspberry_pi

            is_pi = is_raspberry_pi()
        if not is_pi:
            return DesktopBrowserBackend(opener)
        browser = find_browser(media.browser_command, which)
        if browser is None:
            log.warning("Chromium not found; videos need the player page opened manually")
            return NoneBackend("Chromium not found: install chromium-browser, or open the player page manually")
        choice = "kiosk"
    if choice == "kiosk":
        browser = find_browser(media.browser_command, which)
        if browser is None:
            log.warning("media.player=kiosk but no Chromium browser was found")
        return BrowserKioskBackend(
            browser, user_data_dir=config.data_path / "media" / "chromium-profile",
            width=config.display.width, height=config.display.height, extra_args=media.kiosk_extra_args,
            launcher=launcher,
        )
    if choice == "browser":
        return DesktopBrowserBackend(opener)
    return NoneBackend()
