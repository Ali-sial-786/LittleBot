"""NanitService (littlebot/nanit): the baby camera view with a fake ffplay, a fake systemctl and a fake bridge folder
(a camera list, a session file that exists but is never read), no real programs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.nanit.service import NO_STREAM, OWNER, PRIORITY, SHOWING, STARTED, STOPPED, NanitService
from littlebot.testing import make_test_context

CAMERAS = {"cameras": [{"uid": "a1b2c3d4", "name": "Nursery"}, {"uid": "e5f6g7h8", "name": "Guest Room"}]}
SECRET = "refresh-token-must-never-show"


class FakeProc:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._done = asyncio.Event()
        self.signals: list[str] = []
        self.ignore_term = False

    def say(self, line: str) -> None:
        self._lines.put_nowait(line)

    def exit(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self._lines.put_nowait(None)
            self._done.set()

    def terminate(self) -> None:
        self.signals.append("TERM")
        if not self.ignore_term:
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


class FakeLauncher:
    """ffplay. ``no_picture``: each one exits at once, as when the bridge closes a viewer with no camera sending."""

    def __init__(self) -> None:
        self.procs: list[FakeProc] = []
        self.fail = False
        self.no_picture = False

    async def __call__(self, argv: Any, env: Any = None) -> FakeProc:
        if self.fail:
            raise OSError("no such program")
        p = FakeProc(list(argv))
        self.procs.append(p)
        if self.no_picture:
            p.say("rtmp://127.0.0.1:1935/local/a1b2c3d4: Input/output error")
            p.exit(1)
        return p

    @property
    def proc(self) -> FakeProc:
        return self.procs[-1]


class FakeSystemctl:
    def __init__(self) -> None:
        self.state = "active"
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Any, timeout: float, stdin: bytes | None = None) -> tuple[int, str]:
        self.calls.append(list(argv))
        return (0 if self.state == "active" else 3), f"{self.state}\n"


class Env:
    def __init__(self, tmp: Path, *, set_up: bool = True, cameras: Any = CAMERAS, installed: bool = True,
                 max_seconds: float = 0.0, **cfg: Any) -> None:
        self.ctx = make_test_context(nanit=cfg) if cfg else make_test_context()
        self.dir = tmp / "data" / "nanit"
        self.unit_dir = tmp / "systemd" / "user"
        self.bin = tmp / "bridge" / "nanit"
        self.dir.mkdir(parents=True)
        self.unit_dir.mkdir(parents=True)
        self.bin.parent.mkdir(parents=True)
        self.ctx.config.nanit.bridge_command = str(self.bin)
        if set_up:
            self.bin.write_text("#!/bin/sh\n")
            (self.dir / "session.json").write_text(json.dumps({"refreshToken": SECRET}))
            (self.unit_dir / "littlebot-nanit.service").write_text("[Service]\n")
        if cameras is not None:
            (self.dir / "cameras.json").write_text(json.dumps(cameras))
        self.installed = installed
        self.launcher = FakeLauncher()
        self.systemctl = FakeSystemctl()
        self.events: list[tuple[str, dict[str, Any]]] = []
        for topic in (STARTED, SHOWING, STOPPED, "screen.keep_awake"):
            self.ctx.bus.subscribe(topic, lambda e, t=topic: self.events.append((t, dict(e.data or {}))))
        self.svc = NanitService(self.ctx, launcher=self.launcher, runner=self.systemctl, which=self.which,
                                screen=True, data_dir=self.dir, unit_dir=self.unit_dir, start_timeout=0.4,
                                max_seconds=max_seconds, retry_every=0.02, keep_awake_every=0.05)

    def which(self, name: str) -> str | None:
        if name == "ffplay" and not self.installed:
            return None
        return f"/usr/bin/{name}"

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        await self.svc._wait_teardowns()

    async def until(self, cond: Any, timeout: float = 2.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("timed out")
            await asyncio.sleep(0.01)

    def topics(self, topic: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == topic]

    async def showing(self, camera: Any = None) -> FakeProc:
        res = await self.svc.show(camera, source="dashboard")
        assert res["ok"] and res["state"] == "starting", res
        self.launcher.proc.say("Input #0, flv, from 'rtmp://127.0.0.1:1935/local/a1b2c3d4':")
        await self.until(lambda: self.svc.status()["state"] == "showing")
        return self.launcher.proc


@pytest.fixture
async def env(tmp_path: Path) -> Any:
    e = Env(tmp_path)
    await e.svc.start()
    yield e
    await e.svc.stop()
    e.ctx.db.close()


# ---------------------------------------------------------------- showing and closing
async def test_show_then_close(env: Env) -> None:
    proc = await env.showing()
    argv = proc.argv
    assert argv[0] == "/usr/bin/ffplay" and argv[-1] == "rtmp://127.0.0.1:1935/local/a1b2c3d4"
    for opt in ("-fs", "-noborder", "-alwaysontop", "-framedrop", "-autoexit", "-nostats"):
        assert opt in argv, opt
    assert argv[argv.index("-fflags") + 1] == "nobuffer" and argv[argv.index("-flags") + 1] == "low_delay"
    assert argv[argv.index("-window_title") + 1] == "LittleBot"
    assert "-an" not in argv  # sound on by default: it's a baby monitor
    lease = env.ctx.arbiter.holder(Resource.SCREEN)
    assert lease is not None and lease.owner == OWNER and lease.priority == PRIORITY
    assert env.topics(STARTED) == [{"camera": "Nursery", "source": "dashboard"}]
    assert env.topics(SHOWING) == [{"camera": "Nursery"}]
    await env.until(lambda: len(env.topics("screen.keep_awake")) >= 2)
    st = env.svc.status()
    assert st["active"] and st["camera"] == "Nursery" and st["bridge"] == "active" and st["bridge_running"]
    assert await env.svc.close("parent") is True
    assert proc.signals == ["TERM"] and env.ctx.arbiter.holder(Resource.SCREEN) is None
    assert env.topics(STOPPED)[-1] == {"camera": "Nursery", "reason": "parent", "code": None}
    assert env.svc.status()["last"]["reason"] == "parent" and await env.svc.close("parent") is False
    assert ["/usr/bin/systemctl", "--user", "is-active", "littlebot-nanit.service"] in env.systemctl.calls


async def test_showing_twice_is_one_view(env: Env) -> None:
    await env.showing()
    again = await env.svc.show(None)
    assert again["ok"] and again["already"] and len(env.launcher.procs) == 1


async def test_picking_a_camera_by_name_and_switching(env: Env) -> None:
    await env.showing("the guest room camera")
    assert env.launcher.proc.argv[-1].endswith("/local/e5f6g7h8")
    res = await env.svc.show("nursery")
    assert res["ok"] and res["camera"] == "Nursery" and env.launcher.proc.argv[-1].endswith("/local/a1b2c3d4")
    assert env.launcher.procs[0].returncode is not None
    assert env.topics(STOPPED)[0]["reason"] == "switching cameras"
    assert env.svc.pick("Guest") == {"uid": "e5f6g7h8", "name": "Guest Room"}
    assert env.svc.pick(None)["name"] == "Nursery" and env.svc.pick("baby camera")["name"] == "Nursery"
    bad = await env.svc.show("garage")
    assert not bad["ok"] and bad["error"] == "no_such_camera" and "Nursery, Guest Room" in bad["detail"]


async def test_one_camera_answers_to_any_name(tmp_path: Path) -> None:
    e = Env(tmp_path, cameras={"cameras": [{"uid": "a1b2c3d4", "name": "Leo"}]})
    assert e.svc.pick("nursery") == {"uid": "a1b2c3d4", "name": "Leo"}
    e.ctx.db.close()


async def test_the_stream_never_starts_and_the_camera_is_said(env: Env) -> None:
    env.launcher.no_picture = True
    res = await env.svc.show()
    assert res["ok"] and res["state"] == "starting"
    await env.until(lambda: not env.svc.active)
    await env.settle()
    assert len(env.launcher.procs) > 1  # tried again while waiting
    assert env.topics(STOPPED)[-1] == {"camera": "Nursery", "reason": "the camera isn't sending its picture",
                                       "code": NO_STREAM}
    assert env.topics(SHOWING) == [] and env.ctx.arbiter.holder(Resource.SCREEN) is None
    assert env.svc.status()["last"]["showed"] is False


async def test_the_stream_never_starts_because_the_bridge_stopped(env: Env) -> None:
    res = await env.svc.show()
    assert res["ok"]
    env.systemctl.state = "failed"
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1]["reason"] == "the bridge isn't running"
    assert env.topics(STOPPED)[-1]["code"] == NO_STREAM


async def test_ffplay_exiting_brings_the_face_back(env: Env) -> None:
    proc = await env.showing()
    proc.exit(0)
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1]["reason"] == "the stream ended"
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None and len(env.launcher.procs) == 1


async def test_max_minutes(tmp_path: Path) -> None:
    e = Env(tmp_path, max_seconds=0.15)
    await e.svc.start()
    await e.showing()
    assert e.svc.status()["minutes_left"] is not None
    await e.until(lambda: not e.svc.active)
    assert e.topics(STOPPED)[-1]["reason"] == "the time limit"
    await e.svc.stop()
    e.ctx.db.close()


def test_max_minutes_comes_from_the_config(tmp_path: Path) -> None:
    ctx = make_test_context(nanit={"max_minutes": 0})
    assert NanitService(ctx, screen=True)._max_s == 0
    ctx.db.close()
    ctx = make_test_context()
    assert NanitService(ctx, screen=True)._max_s == 30 * 60
    ctx.db.close()


# ---------------------------------------------------------------- closing events
@pytest.mark.parametrize(("topic", "data", "reason"), [
    (Topics.STOP_ALL, {"source": "parent"}, "stop all"),
    (Topics.MEDIA_STARTED, {"item_id": 1}, "a video started"),
    (Topics.ACTIVITY_STARTED, {"id": "x"}, "an activity started"),
    ("game.started", {"game": "bubbles"}, "a game started"),
    (Topics.SETTINGS_CHANGED, {"key": "nanit.enabled", "value": False}, "switched off"),
])
async def test_closing_events(env: Env, topic: str, data: dict[str, Any], reason: str) -> None:
    proc = await env.showing()
    env.ctx.bus.publish(topic, data)
    assert not env.svc.active
    await env.settle()
    assert env.topics(STOPPED)[-1]["reason"] == reason and proc.returncode is not None


async def test_sleep_closes_it(env: Env) -> None:
    await env.showing()
    env.ctx.state.transition(RobotState.SLEEPING, "bedtime", force=True)
    assert not env.svc.active and env.topics(STOPPED)[-1]["reason"] == "LittleBot went to sleep"
    assert (await env.svc.show())["error"] == "asleep"


async def test_a_video_taking_the_screen_closes_it(env: Env) -> None:
    await env.showing()
    assert env.ctx.arbiter.acquire("screens", [Resource.SCREEN], Priority.CONVERSATION) is None  # a card can't
    assert env.ctx.arbiter.acquire("media", [Resource.SCREEN], Priority.MEDIA) is not None
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1]["reason"] == "media took the screen"


async def test_a_busy_screen(env: Env) -> None:
    assert env.ctx.arbiter.acquire("media", [Resource.SCREEN], Priority.PARENT) is not None
    res = await env.svc.show()
    assert not res["ok"] and res["error"] == "busy" and not env.launcher.procs


async def test_the_speaker_and_mic_are_never_taken(env: Env) -> None:
    voice = env.ctx.arbiter.acquire("assistant", [Resource.AUDIO_OUT, Resource.MICROPHONE, Resource.FACE],
                                    Priority.CONVERSATION + 10)
    await env.showing()
    assert voice is not None and voice.active and env.ctx.arbiter.holder(Resource.AUDIO_OUT) is voice


async def test_a_stubborn_ffplay_is_killed(env: Env) -> None:
    proc = await env.showing()
    proc.ignore_term = True
    assert await env.svc.close("parent") is True
    assert proc.signals == ["TERM", "KILL"]


# ---------------------------------------------------------------- sound
async def test_the_sound_switch(env: Env) -> None:
    env.ctx.settings.set("nanit.sound", False)
    assert "-an" in env.svc.ffplay_command("a1b2c3d4") and env.svc.status()["sound"] is False
    env.ctx.settings.set("nanit.sound", True)
    assert "-an" not in env.svc.ffplay_command("a1b2c3d4")
    ctx = make_test_context(nanit={"sound": False})
    assert ctx.settings.get("nanit.sound") is False  # the switch starts from the config
    ctx.db.close()


# ---------------------------------------------------------------- gates
async def test_not_set_up_is_never_a_crash(tmp_path: Path) -> None:
    e = Env(tmp_path, set_up=False, cameras=None)
    await e.svc.start()
    res = await e.svc.show()
    assert not res["ok"] and res["error"] == "not_set_up" and "bash ~/LittleBot/scripts/setup_nanit.sh" in res["detail"]
    st = e.svc.status()
    assert st["set_up"] is False and st["setup"] == {"bridge": False, "signed_in": False, "service": False}
    assert st["setup_command"] == "bash ~/LittleBot/scripts/setup_nanit.sh" and st["cameras"] == []
    assert await e.svc.refresh() == "missing" and e.systemctl.calls == []  # no unit: systemctl isn't asked
    assert not e.launcher.procs and not e.svc.offered()
    await e.svc.stop()
    e.ctx.db.close()


async def test_bridge_not_running_and_no_cameras(tmp_path: Path) -> None:
    e = Env(tmp_path, cameras=None)
    e.systemctl.state = "inactive"
    res = await e.svc.show()
    assert res["error"] == "not_running" and "systemctl --user status littlebot-nanit" in res["detail"]
    e.systemctl.state = "active"
    assert (await e.svc.show())["error"] == "no_cameras" and not e.launcher.procs
    e.ctx.db.close()


async def test_ffplay_missing_or_failing(tmp_path: Path) -> None:
    e = Env(tmp_path, installed=False)
    assert (await e.svc.show())["error"] == "no_player" and e.svc.status()["player"] is False
    e.installed = True
    e.launcher.fail = True
    res = await e.svc.show()
    assert res["error"] == "failed" and not e.svc.active and e.ctx.arbiter.holder(Resource.SCREEN) is None
    e.ctx.db.close()


async def test_the_parents_switch_and_the_config_switch(env: Env, tmp_path: Path) -> None:
    env.ctx.settings.set("nanit.enabled", False)
    assert (await env.svc.show())["error"] == "disabled" and not env.svc.offered()
    env.ctx.settings.set("nanit.enabled", True)
    assert env.svc.offered()
    e = Env(tmp_path / "off", enabled=False)
    assert (await e.svc.show())["error"] == "unavailable" and e.svc.status()["available"] is False
    e.ctx.db.close()


async def test_headless_has_no_camera_view() -> None:
    ctx = make_test_context()
    ctx.config.display.headless = True
    assert NanitService(ctx).unavailable_reason() == "no screen (headless run)"
    ctx.db.close()


# ---------------------------------------------------------------- the camera list and secrets
async def test_the_camera_list_is_checked(tmp_path: Path) -> None:
    e = Env(tmp_path, cameras={"cameras": [
        {"uid": "../../etc", "name": "Bad"}, {"uid": "ok_1", "name": "<b>Nursery</b>"}, {"uid": "ok_1", "name": "x"},
        "junk", {"uid": "ok-2", "name": ""}]})
    assert e.svc.cameras() == [{"uid": "ok_1", "name": "bNurseryb"}, {"uid": "ok-2", "name": "Camera 2"}]
    (e.dir / "cameras.json").write_text("{not json")
    import os  # noqa: PLC0415
    os.utime(e.dir / "cameras.json", (1, 1))
    assert e.svc.cameras() == []
    e.ctx.db.close()


async def test_no_secret_and_no_uid_in_the_status(env: Env) -> None:
    await env.showing()
    text = json.dumps(env.svc.status())
    assert SECRET not in text and "refreshToken" not in text and "a1b2c3d4" not in text
    assert env.svc.status()["cameras"] == [{"name": "Nursery"}, {"name": "Guest Room"}]


def test_the_test_context_never_points_at_the_real_folder() -> None:
    ctx = make_test_context()
    svc = NanitService(ctx)
    assert "littlebot-test-" in str(svc._dir)
    ctx.db.close()
