"""AndroidService (littlebot/android): Android runs only while it is on the screen. Fake waydroid, desktop and clock."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from littlebot.android.service import IMAGES, OWNER, SHOWING, STARTED, STOPPED, USED, AndroidService
from littlebot.android.windows import Window
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context

FACE = Window(app_id="__main__.py", title="LittleBot", fullscreen=True)
FACE_FRONT = Window(app_id="__main__.py", title="LittleBot", fullscreen=True, active=True)
DROID = Window(app_id="Waydroid", title="Waydroid", active=True)
DROID_BEHIND = Window(app_id="Waydroid", title="Waydroid")
WAYDROID = "/usr/bin/waydroid"


class FakeProc:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.pid = 4242
        self.returncode: int | None = None
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._done = asyncio.Event()
        self.signals: list[str] = []

    def say(self, line: str) -> None:
        self._lines.put_nowait(line)

    def exit(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self._lines.put_nowait(None)
            self._done.set()

    def terminate(self) -> None:
        self.signals.append("TERM")
        self.exit(-15)

    def kill(self) -> None:
        self.signals.append("KILL")
        self.exit(-9)

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    async def lines(self) -> Any:
        while True:
            line = await self._lines.get()
            if line is None:
                return
            yield line


class FakeInputs:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class Env:
    def __init__(self, *, page: int = 4096, installed: bool = True, images: bool = True, **cfg: Any) -> None:
        self.ctx = make_test_context(android=cfg) if cfg else make_test_context()
        self.t = 1000.0
        self.windows: list[Window] | None = [FACE_FRONT]
        self.free: int | None = 3000
        self.free_after_stop = 2900
        self.booted = True
        self.container = "active"
        self.session_running = False
        self.procs: list[FakeProc] = []
        self.calls: list[list[str]] = []
        self.inputs: list[FakeInputs] = []
        self.outside_pid: int | None = None
        self.alive: set[int] = set()
        self.events: list[tuple[str, dict[str, Any]]] = []
        for topic in (STARTED, SHOWING, USED, STOPPED):
            self.ctx.bus.subscribe(topic, lambda e, t=topic: self.events.append((t, dict(e.data or {}))))
        self.svc = AndroidService(
            self.ctx, launcher=self.launch, runner=self.run, windows=lambda: self.windows, inputs=self.make_inputs,
            which=lambda n: WAYDROID if installed and n == "waydroid" else None,
            exists=lambda p: images and p in IMAGES, page_size=lambda: page, memory=lambda: self.free,
            outside=lambda: self.outside_pid if self.outside_pid in self.alive else None,
            alive=lambda pid: pid in self.alive, clock=lambda: self.t,
            screen=True, poll=0.005, boot_poll=0.005, adopt_every=0.01, settle=0.0)

    async def launch(self, argv: Any, env: Any = None) -> FakeProc:
        p = FakeProc(list(argv))
        self.procs.append(p)
        self.session_running = True
        return p

    async def run(self, argv: Any, timeout: float, stdin: bytes | None = None) -> tuple[int, str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "systemctl":
            return (0 if self.container == "active" else 3), f"{self.container}\n"
        cmd = argv[1:]
        if cmd == ["status"]:
            return 0, f"Session:\t{'RUNNING' if self.session_running else 'STOPPED'}\nContainer:\tRUNNING\n"
        if cmd == ["prop", "get", "sys.boot_completed"]:
            return 0, "1\n" if self.booted else "\n"
        if cmd == ["session", "stop"]:
            self.session_running = False
            self.alive.clear()
            self.free = self.free_after_stop
            for p in self.procs:
                p.exit(0)
        return 0, ""

    def make_inputs(self, callback: Any) -> FakeInputs:
        w = FakeInputs(callback)
        self.inputs.append(w)
        return w

    def waydroid(self, *args: str) -> bool:
        return [WAYDROID, *args] in self.calls

    def topics(self, topic: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == topic]

    async def until(self, cond: Any, timeout: float = 2.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("timed out")
            await asyncio.sleep(0.005)

    async def showing(self) -> None:
        res = await self.svc.open(source="dashboard")
        assert res["ok"], res
        await self.until(lambda: self.waydroid("show-full-ui"))
        self.windows = [FACE, DROID]
        await self.until(lambda: self.topics(SHOWING))

    async def closed(self) -> str:
        await self.until(lambda: not self.svc.active and self.topics(STOPPED))
        await self.svc._wait_teardowns()
        return self.topics(STOPPED)[-1]["reason"]


@pytest.fixture
async def env() -> Any:
    e = Env()
    await e.svc.start()
    yield e
    await e.svc.stop()


async def test_not_set_up_says_the_next_step() -> None:
    for kw, step in (({"page": 16384}, "kernel"), ({"images": False}, "install"), ({"installed": False}, "install")):
        e = Env(**kw)
        await e.svc.start()
        res = await e.svc.open()
        assert res["error"] == "not_set_up" and e.svc.status()["setup_step"] == step
        assert "setup_waydroid.sh" in res["detail"]
        await e.svc.stop()
    e = Env()
    e.container = "inactive"
    await e.svc.start()
    res = await e.svc.open()
    assert res["error"] == "not_set_up" and "systemctl enable --now waydroid-container" in res["detail"]
    assert e.procs == []
    await e.svc.stop()


async def test_open_starts_it_then_shows_it(env: Env) -> None:
    res = await env.svc.open(source="voice")
    assert res == {"ok": True, "state": "starting", "error": None, "detail": "", "already": False}
    assert env.procs[0].argv == [WAYDROID, "session", "start"]
    assert env.topics(STARTED) == [{"source": "voice", "app": ""}]
    assert env.ctx.arbiter.holder(Resource.SCREEN).owner == OWNER
    assert env.inputs and env.inputs[0].started
    await env.until(lambda: env.waydroid("show-full-ui"))
    assert env.svc.status()["state"] == "starting"  # its window isn't there yet
    env.windows = [FACE, DROID]
    await env.until(lambda: env.svc.status()["state"] == "showing")
    assert env.topics(SHOWING) == [{}]
    again = await env.svc.open()
    assert again["already"] is True and len(env.procs) == 1


async def test_it_waits_for_android_to_finish_starting(env: Env) -> None:
    env.booted = False
    await env.svc.open()
    await asyncio.sleep(0.05)
    assert not env.waydroid("show-full-ui")
    env.booted = True
    await env.until(lambda: env.waydroid("show-full-ui"))


async def test_closing_its_window_shuts_it_down_and_frees_the_memory(env: Env) -> None:
    await env.showing()
    env.free = 1500
    env.windows = [FACE_FRONT]
    await asyncio.sleep(0.05)
    assert env.svc.active  # not at once: off_screen_seconds (10)
    env.t += 11
    assert await env.closed() == "its window was closed"
    assert env.waydroid("session", "stop")
    assert env.procs[0].returncode == 0 and env.procs[0].signals == []
    assert env.inputs[0].stopped
    last = env.svc.status()["last"]
    assert last["free_mb"] == 2900 and last["freed_mb"] == 1400 and last["reason"] == "its window was closed"
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None


async def test_another_window_in_front_shuts_it_down(env: Env) -> None:
    await env.showing()
    env.windows = [FACE_FRONT, DROID_BEHIND]
    await asyncio.sleep(0.05)
    env.t += 5
    await asyncio.sleep(0.05)
    assert env.svc.active
    env.windows = [FACE, DROID]  # back in front: the wait starts again
    await asyncio.sleep(0.05)
    env.windows = [FACE_FRONT, DROID_BEHIND]
    await asyncio.sleep(0.05)
    env.t += 9
    await asyncio.sleep(0.05)
    assert env.svc.active
    env.t += 2
    assert await env.closed() == "another window is in front of it"


async def test_the_screen_going_dark_shuts_it_down(env: Env) -> None:
    await env.showing()
    env.ctx.bus.publish(Topics.FACE_SCREEN, {"on": True, "reason": "x"})
    await asyncio.sleep(0.02)
    assert env.svc.active
    env.ctx.bus.publish(Topics.FACE_SCREEN, {"on": False, "reason": "nothing happening for 180 s"})
    assert await env.closed() == "the screen went dark (nothing happening for 180 s)"


async def test_something_else_taking_the_screen_shuts_it_down(env: Env) -> None:
    await env.showing()
    lease = env.ctx.arbiter.acquire("media", [Resource.SCREEN], Priority.MEDIA)
    assert lease is not None
    assert await env.closed() == "media took the screen"
    lease.release()


@pytest.mark.parametrize(("topic", "data", "reason"), [
    (Topics.STOP_ALL, {"source": "dashboard"}, "stop all"),
    (Topics.MEDIA_STARTED, {"item_id": 1}, "a video started"),
    ("game.started", {"game": "x"}, "a game started"),
])
async def test_what_else_shuts_it_down(env: Env, topic: str, data: dict[str, Any], reason: str) -> None:
    await env.showing()
    env.ctx.bus.publish(topic, data)
    assert await env.closed() == reason


async def test_going_to_sleep_shuts_it_down(env: Env) -> None:
    await env.showing()
    env.ctx.state.transition(RobotState.SLEEPING, "told to stop")
    assert await env.closed() == "LittleBot went to sleep"


async def test_a_window_that_never_comes(env: Env) -> None:
    await env.svc.open()
    await env.until(lambda: env.waydroid("show-full-ui"))
    env.t += 181
    reason = await env.closed()
    assert reason == "its window didn't come within 180 s (the desktop's windows: __main__.py)"


async def test_memory(env: Env) -> None:
    env.free = 800
    res = await env.svc.open()
    assert res["error"] == "low_memory" and "1200 MB" in res["detail"] and "800 MB" in res["detail"]
    env.free = 3000
    await env.showing()
    env.free = 200
    assert await env.closed() == "memory ran low (200 MB free)"


async def test_waydroid_stopping_by_itself(env: Env) -> None:
    await env.showing()
    env.procs[0].exit(1)
    assert await env.closed() == "Android stopped (waydroid exit 1)"


async def test_an_app(env: Env) -> None:
    assert (await env.svc.open(app="Netflix"))["error"] == "unknown_app"
    res = await env.svc.open(app="Aosu")
    assert res["ok"]
    assert env.topics(STARTED) == [{"source": "voice", "app": "Aosu"}]
    await env.until(lambda: env.waydroid("app", "launch", "com.glazero.android"))
    assert env.calls.index([WAYDROID, "show-full-ui"]) < env.calls.index([WAYDROID, "app", "launch",
                                                                          "com.glazero.android"])


async def test_touches_are_published(env: Env) -> None:
    await env.showing()
    env.inputs[0].callback()
    assert env.topics(USED) == [{}]


async def test_a_session_started_outside_littlebot_is_watched(env: Env) -> None:
    env.outside_pid = 777
    env.alive = {777}
    await env.until(lambda: env.svc.active)
    assert env.svc.status()["source"] == "outside LittleBot" and env.procs == []
    env.windows = [FACE, DROID]
    await env.until(lambda: env.topics(SHOWING))
    env.windows = [FACE_FRONT]
    await asyncio.sleep(0.05)
    env.t += 11
    assert await env.closed() == "its window was closed"
    assert env.waydroid("session", "stop")
    await asyncio.sleep(0.05)
    assert not env.svc.active  # (stopped: not found again)
    env.alive = {777}  # started by hand once more, then stopped by hand
    await env.until(lambda: env.svc.active)
    env.alive = set()
    assert await env.closed() == "Android stopped"


async def test_the_parents_switch(env: Env) -> None:
    env.ctx.settings.set("android.enabled", False)
    assert (await env.svc.open())["error"] == "disabled"
    env.ctx.settings.set("android.enabled", True)
    await env.showing()
    env.ctx.settings.set("android.enabled", False)
    assert await env.closed() == "switched off"


async def test_a_sleeping_robot_wakes_up_for_it(env: Env) -> None:
    env.ctx.state.transition(RobotState.SLEEPING, "test")
    res = await env.svc.open(source="dashboard")
    assert res["ok"] and env.ctx.state.state == RobotState.IDLE
    assert env.svc.active


async def test_close_and_stop(env: Env) -> None:
    assert await env.svc.close("nothing") is False
    await env.showing()
    assert await env.svc.close("closed on the dashboard") is True
    assert not env.svc.active and env.topics(STOPPED)[-1]["reason"] == "closed on the dashboard"


async def test_the_desktop_that_cant_be_asked(env: Env) -> None:
    env.windows = None
    await env.svc.open()
    await env.until(lambda: env.topics(SHOWING))  # taken as on the screen; the screen going dark still ends it
    env.t += 60
    await asyncio.sleep(0.05)
    assert env.svc.active
