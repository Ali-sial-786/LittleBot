"""Display backends: kiosk command line, launch/terminate, missing browser, auto selection."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from littlebot.core.events import Topics
from littlebot.media.display import (
    BrowserKioskBackend, DesktopBrowserBackend, NoneBackend, PlayerBackendError, build_kiosk_command,
    find_browser, select_backend,
)
from littlebot.testing import make_test_config

URL = "http://127.0.0.1:8765/player?token=abc"


class FakeProcess:
    _pids = iter(range(4000, 5000))

    def __init__(self, argv, *, obey_terminate: bool = True) -> None:
        self.argv = list(argv)
        self.pid = next(self._pids)
        self.returncode = None
        self.signals: list[str] = []
        self.obey_terminate = obey_terminate
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.signals.append("TERM")
        if self.obey_terminate:
            self._exit(-15)

    def kill(self) -> None:
        self.signals.append("KILL")
        self._exit(-9)

    def _exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode


class FakeLauncher:
    def __init__(self, **proc_kw) -> None:
        self.procs: list[FakeProcess] = []
        self.proc_kw = proc_kw

    async def __call__(self, argv):
        proc = FakeProcess(argv, **self.proc_kw)
        self.procs.append(proc)
        return proc


def test_kiosk_command_line(tmp_path):
    argv = build_kiosk_command("/usr/bin/chromium-browser", URL, user_data_dir=tmp_path / "profile",
                               width=480, height=320, extra_args=["--foo"])
    assert argv[0] == "/usr/bin/chromium-browser"
    for flag in ("--kiosk", f"--app={URL}", "--window-size=480,320", "--autoplay-policy=no-user-gesture-required",
                 "--noerrdialogs", "--disable-session-crashed-bubble", "--disable-extensions", "--no-first-run",
                 "--incognito", "--force-device-scale-factor=1", f"--user-data-dir={tmp_path / 'profile'}"):
        assert flag in argv
    assert argv[-1] == "--foo"
    assert not any("load-extension" in a for a in argv)


async def test_kiosk_launch_reuse_and_terminate(tmp_path):
    launcher = FakeLauncher()
    kiosk = BrowserKioskBackend("/usr/bin/chromium", user_data_dir=tmp_path / "p", launcher=launcher)
    assert kiosk.available and kiosk.can_close and kiosk.is_running() is False
    await kiosk.open(URL)
    assert len(launcher.procs) == 1 and kiosk.is_running() is True
    assert launcher.procs[0].argv == kiosk.command(URL) == kiosk.last_command
    assert (tmp_path / "p").is_dir()
    await kiosk.open(URL)  # already running: the page loads new items itself
    assert len(launcher.procs) == 1
    st = kiosk.status()
    assert st["backend"] == "kiosk" and st["running"] is True and st["pid"] == launcher.procs[0].pid
    await kiosk.close()
    assert launcher.procs[0].signals == ["TERM"] and kiosk.is_running() is False
    await kiosk.close()  # idempotent
    await kiosk.open(URL)
    assert len(launcher.procs) == 2


async def test_kiosk_killed_if_terminate_ignored(tmp_path):
    launcher = FakeLauncher(obey_terminate=False)
    kiosk = BrowserKioskBackend("/usr/bin/chromium", user_data_dir=tmp_path, launcher=launcher,
                                terminate_timeout=0.05)
    await kiosk.open(URL)
    await kiosk.close()
    assert launcher.procs[0].signals == ["TERM", "KILL"] and launcher.procs[0].returncode == -9


async def test_kiosk_missing_browser(tmp_path):
    kiosk = BrowserKioskBackend(None, user_data_dir=tmp_path)
    assert kiosk.available is False and "not found" in kiosk.status()["detail"]
    with pytest.raises(PlayerBackendError, match="Chromium is not installed"):
        await kiosk.open(URL)


async def test_kiosk_launch_failure(tmp_path):
    async def broken(argv):
        raise FileNotFoundError(argv[0])

    kiosk = BrowserKioskBackend("/nope/chromium", user_data_dir=tmp_path, launcher=broken)
    with pytest.raises(PlayerBackendError, match="could not start"):
        await kiosk.open(URL)
    assert kiosk.is_running() is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_kiosk_with_real_subprocess_is_terminated(tmp_path):
    fake_browser = tmp_path / "fake-chromium"
    fake_browser.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_browser.chmod(0o755)
    kiosk = BrowserKioskBackend(str(fake_browser), user_data_dir=tmp_path / "profile")
    await kiosk.open(URL)
    assert kiosk.is_running() is True
    pid = kiosk.status()["pid"]
    await kiosk.close()
    assert kiosk.is_running() is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_controller_closes_kiosk_on_stop(make_env, tmp_path):
    launcher = FakeLauncher()
    kiosk = BrowserKioskBackend("/usr/bin/chromium", user_data_dir=tmp_path, launcher=launcher)
    e = make_env(backend=kiosk)
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    assert len(launcher.procs) == 1 and launcher.procs[0].argv[2] == f"--app={e.media.player_url()}"
    await e.media.stop()
    await e.settle()
    assert launcher.procs[0].signals == ["TERM"]
    assert e.last(Topics.MEDIA_STOPPED).data["reason"] == "stopped"
    # a new session launches a fresh window
    await e.media.play(item["id"])
    assert len(launcher.procs) == 2
    await e.media.close()
    assert launcher.procs[1].returncode is not None


async def test_desktop_backend_opens_browser_in_thread():
    calls = []

    def opener(url, new):
        calls.append((url, new))
        return True

    backend = DesktopBrowserBackend(opener)
    assert backend.can_close is False and backend.is_running() is None
    await backend.open(URL)
    await backend.close()  # cannot close a user's browser: no-op
    assert calls == [(URL, 2)]
    failing = DesktopBrowserBackend(lambda url, new: False)
    with pytest.raises(PlayerBackendError):
        await failing.open(URL)


async def test_none_backend():
    backend = NoneBackend()
    await backend.open(URL)
    assert backend.last_url == URL and backend.status()["backend"] == "none"


def test_find_browser(tmp_path):
    found = {"chromium": "/usr/bin/chromium"}
    assert find_browser(None, found.get) == "/usr/bin/chromium"
    assert find_browser(None, {}.get) is None
    assert find_browser("my-chrome", {"my-chrome": "/opt/my-chrome"}.get) == "/opt/my-chrome"
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert find_browser(str(exe)) == str(exe)
    assert find_browser(str(tmp_path / "missing")) is None


def test_select_backend():
    which_chromium = {"chromium-browser": "/usr/bin/chromium-browser"}.get
    auto = make_test_config(media={"player": "auto"}, display={"headless": False})
    assert isinstance(select_backend(auto, is_pi=False), DesktopBrowserBackend)
    kiosk = select_backend(auto, is_pi=True, which=which_chromium)
    assert isinstance(kiosk, BrowserKioskBackend) and kiosk.browser == "/usr/bin/chromium-browser"
    assert kiosk.width == 480 and kiosk.height == 320
    assert str(kiosk.user_data_dir).endswith(os.path.join("data", "media", "chromium-profile"))
    assert isinstance(select_backend(auto, is_pi=True, which={}.get), NoneBackend)
    # headless runs auto -> none (test config is headless)
    headless = make_test_config(media={"player": "auto"}, display={"headless": True})
    assert isinstance(select_backend(headless, is_pi=True, which=which_chromium), NoneBackend)
    offline = make_test_config(mode="offline", media={"player": "kiosk"})
    assert isinstance(select_backend(offline, is_pi=True, which=which_chromium), NoneBackend)
    assert isinstance(select_backend(make_test_config(media={"player": "none"})), NoneBackend)
    assert isinstance(select_backend(make_test_config(media={"player": "browser"})), DesktopBrowserBackend)
    missing = select_backend(make_test_config(media={"player": "kiosk"}), which={}.get)
    assert isinstance(missing, BrowserKioskBackend) and missing.available is False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # an unreaped zombie counts as dead
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _fake_browser_with_child(tmp_path):
    script = tmp_path / "fake-chromium"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path / 'child.pid'}\nexec sleep 30\n")
    script.chmod(0o755)
    return script


async def _gone(pid: int, timeout: float = 2.0) -> bool:
    """The main process is reaped by stop(); its orphaned child dies a moment later."""
    for _ in range(int(timeout / 0.01)):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.01)
    return False


async def _child_pid(tmp_path) -> int:
    for _ in range(200):
        f = tmp_path / "child.pid"
        if f.exists() and f.read_text().strip():
            return int(f.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("fake browser did not start its child")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_app_shutdown_stop_terminates_kiosk_process_group(make_env, tmp_path):
    """systemd KillMode=mixed only signals the main process: MediaController.stop() must kill Chromium."""
    kiosk = BrowserKioskBackend(str(_fake_browser_with_child(tmp_path)), user_data_dir=tmp_path / "profile")
    e = make_env(backend=kiosk)
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    pid, child = kiosk.status()["pid"], await _child_pid(tmp_path)
    assert _alive(pid) and _alive(child)
    e.ctx.bus.publish(Topics.SYSTEM_SHUTDOWN, {"reason": "SIGTERM"})
    await e.media.stop()  # exactly what app/robot.py calls on shutdown
    assert not _alive(pid)  # the browser's main process is gone before stop() returned
    assert await _gone(child)  # ... and the rest of its process group was signalled too
    assert e.media.health()["status"] == "stopped"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_parent_stop_terminates_kiosk_before_returning(make_env, tmp_path):
    kiosk = BrowserKioskBackend(str(_fake_browser_with_child(tmp_path)), user_data_dir=tmp_path / "profile")
    e = make_env(backend=kiosk, media_cfg={"kiosk_close_delay_seconds": 1.5})
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    pid = kiosk.status()["pid"]
    await e.media.stop("stopped")
    assert not _alive(pid) and kiosk.is_running() is False
    await e.media.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_atexit_safety_net_signals_running_kiosks(tmp_path):
    from littlebot.media import display

    kiosk = BrowserKioskBackend(str(_fake_browser_with_child(tmp_path)), user_data_dir=tmp_path / "profile")
    await kiosk.open(URL)
    child = await _child_pid(tmp_path)
    assert kiosk in display._LIVE_KIOSKS
    display._terminate_live_kiosks()  # what runs at interpreter exit
    assert await _gone(child)
    await asyncio.wait_for(kiosk._proc.wait(), 2)
    await kiosk.close()
    assert kiosk not in display._LIVE_KIOSKS
