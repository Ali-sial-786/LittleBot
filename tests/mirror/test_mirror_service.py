"""MirrorService (littlebot/mirror): iPhone (uxplay) and Android (scrcpy + adb) with fake programs, no real ones."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.mirror import adb as A
from littlebot.mirror.service import OWNER, PRIORITY, SHOWING, STARTED, STOPPED, MirrorService
from littlebot.testing import make_test_context

PAIRED = "Enter pairing code: Successfully paired to 192.168.1.23:37099 [guid=adb-R5CT-abc123]\n"
MDNS = "List of discovered mdns services\nadb-R5CT-abc123\t_adb-tls-connect._tcp\t192.168.1.23:41235\n"
NO_DEVICES = "List of devices attached\n\n"
ONE_DEVICE = "List of devices attached\n192.168.1.23:41235\tdevice\n"


class FakeProc:
    def __init__(self, argv: list[str], env: dict[str, str] | None) -> None:
        self.argv, self.env = argv, env
        self.pid = 4242
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
    def __init__(self) -> None:
        self.procs: list[FakeProc] = []
        self.fail = False

    async def __call__(self, argv: Any, env: Any = None) -> FakeProc:
        if self.fail:
            raise OSError("no such program")
        p = FakeProc(list(argv), env)
        self.procs.append(p)
        return p

    @property
    def proc(self) -> FakeProc:
        return self.procs[-1]


class FakeAdb:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.devices = NO_DEVICES
        self.mdns = MDNS
        self.connect_ok = True
        self.pair_out = PAIRED

    async def __call__(self, argv: Any, timeout: float, stdin: bytes | None = None) -> tuple[int, str]:
        argv = list(argv)
        self.calls.append((argv, stdin))
        cmd = argv[1:]
        if cmd[:1] == ["devices"]:
            return 0, self.devices
        if cmd[:2] == ["mdns", "services"]:
            return 0, self.mdns
        if cmd[:1] == ["connect"]:
            if self.connect_ok:
                self.devices = f"List of devices attached\n{cmd[1]}\tdevice\n"
                return 0, f"connected to {cmd[1]}\n"
            return 1, f"failed to connect to {cmd[1]}\n"
        if cmd[:1] == ["pair"]:
            return (0 if "Successfully" in self.pair_out else 1), self.pair_out
        return 0, ""

    def commands(self) -> list[list[str]]:
        return [c[0][1:] for c in self.calls]


class Env:
    def __init__(self, installed: tuple[str, ...] = ("uxplay", "scrcpy", "adb", "stdbuf"), **cfg: Any) -> None:
        self.ctx = make_test_context(mirror=cfg) if cfg else make_test_context()
        self.launcher = FakeLauncher()
        self.adb = FakeAdb()
        self.installed = set(installed)
        self.events: list[tuple[str, dict[str, Any]]] = []
        for topic in (STARTED, SHOWING, STOPPED, "screen.keep_awake"):
            self.ctx.bus.subscribe(topic, lambda e, t=topic: self.events.append((t, dict(e.data or {}))))
        self.found: list[tuple[str, str]] = []  # what looking on the Wi-Fi finds (when adb's mDNS finds nothing)
        self.svc = MirrorService(self.ctx, launcher=self.launcher, runner=self.adb, which=self.which, screen=True,
                                 wait_seconds=0.3, keep_awake_every=0.05, browser=self.browse)

    async def browse(self) -> list[tuple[str, str]]:
        return list(self.found)

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.installed else None

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

    def pair(self) -> None:
        self.ctx.settings.set("mirror.android_phones", [{"guid": "adb-R5CT-abc123", "address": "192.168.1.23",
                                                         "name": "Ali's phone", "paired_at": 1}])


@pytest.fixture
async def env() -> Any:
    e = Env()
    await e.svc.start()
    yield e
    await e.svc.stop()
    e.ctx.db.close()


# ---------------------------------------------------------------- iPhone
async def test_iphone_waits_then_shows_then_the_phone_stops(env: Env) -> None:
    res = await env.svc.start("iphone", source="dashboard")
    assert res["ok"] and res["state"] == "waiting" and res["kind"] == "iphone"
    argv = env.launcher.proc.argv
    assert argv[:2] == ["/usr/bin/stdbuf", "-oL"] and argv[2] == "/usr/bin/uxplay"
    for opt in ("-fs", "-avdec", "-nh"):
        assert opt in argv
    assert argv[argv.index("-n") + 1] == "LittleBot" and argv[argv.index("-vs") + 1] == "waylandsink"
    assert argv[argv.index("-s") + 1] == "1024x600" and argv[argv.index("-as") + 1] == "0"  # the 7" screen; no sound by default
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None  # waiting: cards still show
    assert env.svc.status()["state"] == "waiting" and env.svc.status()["waits_left"] is not None
    env.launcher.proc.say("Open connections: 1")
    env.launcher.proc.say("Open connections: 0")  # an iPhone only looking at the list: not mirroring
    await env.settle()
    assert env.svc.active and env.svc.status()["state"] == "waiting"
    env.launcher.proc.say("raop_rtp_mirror starting mirroring")
    await env.until(lambda: env.svc.status()["state"] == "showing")
    lease = env.ctx.arbiter.holder(Resource.SCREEN)
    assert lease is not None and lease.owner == OWNER and lease.priority == PRIORITY
    assert env.topics(SHOWING) == [{"kind": "iphone"}]
    await env.until(lambda: len(env.topics("screen.keep_awake")) >= 2)
    env.launcher.proc.say("raop_rtp_mirror->running is no longer true")
    await env.until(lambda: not env.svc.active)
    await env.settle()
    assert env.topics(STOPPED)[-1] == {"kind": "iphone", "reason": "the phone stopped mirroring"}
    assert env.launcher.proc.signals == ["TERM"] and env.ctx.arbiter.holder(Resource.SCREEN) is None
    assert env.svc.status()["last"]["reason"] == "the phone stopped mirroring"


async def test_no_iphone_in_time_closes_the_receiver(env: Env) -> None:
    await env.svc.start("iphone")
    await env.until(lambda: not env.svc.active)
    await env.settle()
    assert env.topics(STOPPED) == [{"kind": "iphone", "reason": "no phone came"}]
    assert env.launcher.proc.returncode is not None


async def test_a_lost_connection_after_mirroring_brings_the_face_back(env: Env) -> None:
    await env.svc.start("iphone")
    env.launcher.proc.say("raop_rtp_mirror starting mirroring")
    await env.until(lambda: env.svc.status()["state"] == "showing")
    env.launcher.proc.say("*** ERROR lost connection with client (network problem?)")
    await env.until(lambda: not env.svc.active)


async def test_starting_twice_is_one_receiver_and_switching_phones_stops_the_first(env: Env) -> None:
    await env.svc.start("iphone")
    again = await env.svc.start("iphone")
    assert again["ok"] and again["already"] and len(env.launcher.procs) == 1
    env.pair()
    res = await env.svc.start("android")
    assert res["ok"] and res["kind"] == "android" and len(env.launcher.procs) == 2
    assert env.launcher.procs[0].returncode is not None
    assert env.topics(STOPPED)[0]["reason"] == "switching phones"


async def test_uxplay_exiting_stops_it(env: Env) -> None:
    await env.svc.start("iphone")
    env.launcher.proc.exit(1)
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1]["reason"] == "uxplay exited (1)"


async def test_sound_and_size_come_from_the_config() -> None:
    e = Env(sound=True, max_size=960, airplay_name="Kitchen Bot")
    argv = e.svc.uxplay_command()
    assert "-as" not in argv and argv[argv.index("-s") + 1] == "960x562" and "Kitchen Bot" in argv  # 1024x600 at 960
    android = e.svc.scrcpy_command("192.168.1.23:41235")
    assert "--no-audio" not in android and "--max-size=960" in android
    e.ctx.db.close()


async def test_without_stdbuf_uxplay_runs_directly() -> None:
    e = Env(installed=("uxplay",))
    assert e.svc.uxplay_command()[0] == "/usr/bin/uxplay"
    e.ctx.db.close()


# ---------------------------------------------------------------- Android
async def test_android_connects_with_mdns_and_scrcpy_shows_it(env: Env) -> None:
    env.pair()
    res = await env.svc.start("android", source="voice")
    assert res["ok"] and res["state"] == "showing"
    assert ["mdns", "services"] in env.adb.commands() and ["connect", "192.168.1.23:41235"] in env.adb.commands()
    argv = env.launcher.proc.argv
    assert argv[0] == "/usr/bin/scrcpy" and "--serial=192.168.1.23:41235" in argv
    for opt in ("--fullscreen", "--window-borderless", "--max-size=1024", "--max-fps=30", "--no-audio",
                "--no-control", "--video-bit-rate=4000000"):
        assert opt in argv, opt
    assert "--stay-awake" not in argv  # needs control
    assert env.launcher.proc.env is not None and env.launcher.proc.env["ADB"] == "/usr/bin/adb"
    lease = env.ctx.arbiter.holder(Resource.SCREEN)
    assert lease is not None and lease.owner == OWNER
    assert env.topics(STARTED) == [{"kind": "android", "source": "voice"}]
    env.launcher.proc.exit(0)  # the phone went away
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1] == {"kind": "android", "reason": "the phone went away"}
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None


async def test_android_already_connected_needs_no_mdns(env: Env) -> None:
    env.pair()
    env.adb.devices = "List of devices attached\nadb-R5CT-abc123._adb-tls-connect._tcp\tdevice\nemulator-5554\tdevice\n"
    res = await env.svc.start("android")
    assert res["ok"] and ["mdns", "services"] not in env.adb.commands()
    assert "--serial=adb-R5CT-abc123._adb-tls-connect._tcp" in env.launcher.proc.argv


async def test_android_found_on_the_wifi_when_adb_has_no_mdns(env: Env) -> None:
    # Debian's adb has no mDNS: the paired phone was never found again (the robot, 2026-11-02)
    env.pair()
    env.adb.mdns = "List of discovered mdns services\n"
    env.found = [("adb-OTHER-zzz", "192.168.1.40:40001"), ("adb-R5CT-abc123", "192.168.1.23:41235"),
                 ("adb-BAD", "not an address")]
    res = await env.svc.start("android", source="dashboard")
    assert res["ok"] and res["state"] == "showing"
    connects = [c for c in env.adb.commands() if c[0] == "connect"]
    assert connects[0] == ["connect", "192.168.1.23:41235"]  # the paired phone first
    assert ["connect", "not an address"] not in connects
    assert "--serial=192.168.1.23:41235" in env.launcher.proc.argv


async def test_android_touch_control_from_the_config() -> None:
    e = Env(touch_control=True)
    argv = e.svc.scrcpy_command("192.168.1.23:41235")
    assert "--no-control" not in argv and "--stay-awake" in argv
    e.ctx.db.close()


async def test_android_without_a_pairing_or_a_phone(env: Env) -> None:
    env.adb.mdns = "List of discovered mdns services\n"
    res = await env.svc.start("android")
    assert not res["ok"] and res["error"] == "not_paired" and not env.launcher.procs
    env.pair()
    res = await env.svc.start("android")
    assert not res["ok"] and res["error"] == "no_phone" and "Wireless debugging" in res["detail"]
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None and not env.svc.active


async def test_android_busy_screen(env: Env) -> None:
    env.pair()
    assert env.ctx.arbiter.acquire("media", [Resource.SCREEN], Priority.PARENT) is not None
    res = await env.svc.start("android")
    assert not res["ok"] and res["error"] == "busy" and not env.launcher.procs


# ---------------------------------------------------------------- closing
@pytest.mark.parametrize(("topic", "data", "reason"), [
    (Topics.STOP_ALL, {"source": "parent"}, "stop all"),
    (Topics.MEDIA_STARTED, {"item_id": 1}, "a video started"),
    (Topics.ACTIVITY_STARTED, {"id": "x"}, "an activity started"),
    ("game.started", {"game": "bubbles"}, "a game started"),
    (Topics.SETTINGS_CHANGED, {"key": "mirror.enabled", "value": False}, "switched off"),
])
async def test_closing_events(env: Env, topic: str, data: dict[str, Any], reason: str) -> None:
    env.pair()
    await env.svc.start("android")
    env.ctx.bus.publish(topic, data)
    assert not env.svc.active
    await env.settle()
    assert env.topics(STOPPED)[-1]["reason"] == reason and env.launcher.proc.returncode is not None


async def test_sleep_closes_it(env: Env) -> None:
    await env.svc.start("iphone")
    env.ctx.state.transition(RobotState.SLEEPING, "bedtime", force=True)
    assert not env.svc.active and env.topics(STOPPED)[-1]["reason"] == "LittleBot went to sleep"
    assert (await env.svc.start("iphone"))["error"] == "asleep"


async def test_a_game_taking_the_screen_stops_it(env: Env) -> None:
    env.pair()
    await env.svc.start("android")
    assert env.ctx.arbiter.acquire("screens", [Resource.SCREEN], Priority.CONVERSATION) is None  # a card can't
    assert env.ctx.arbiter.acquire("games", [Resource.SCREEN], Priority.ACTIVITY) is not None
    await env.until(lambda: not env.svc.active)
    assert env.topics(STOPPED)[-1]["reason"] == "games took the screen"


async def test_the_speaker_and_mic_are_never_taken(env: Env) -> None:
    voice = env.ctx.arbiter.acquire("assistant", [Resource.AUDIO_OUT, Resource.MICROPHONE, Resource.FACE],
                                    Priority.CONVERSATION + 10)
    env.pair()
    assert (await env.svc.start("android"))["ok"]
    assert voice is not None and voice.active
    assert env.ctx.arbiter.holder(Resource.AUDIO_OUT) is voice


async def test_stop_from_the_dashboard_and_a_stubborn_program(env: Env) -> None:
    await env.svc.start("iphone")
    env.launcher.proc.ignore_term = True
    assert await env.svc.stop("parent") is True
    assert env.launcher.proc.signals == ["TERM", "KILL"]
    assert await env.svc.stop("parent") is False


# ---------------------------------------------------------------- gates
async def test_not_installed_is_never_a_crash() -> None:
    e = Env(installed=())
    await e.svc.start()
    res = await e.svc.start("iphone")
    assert not res["ok"] and res["error"] == "not_installed" and "sudo apt install uxplay" in res["detail"]
    res = await e.svc.start("android")
    assert res["error"] == "not_installed" and "trixie-backports" in res["detail"]
    st = e.svc.status()
    assert st["tools"] == {"uxplay": False, "scrcpy": False, "adb": False}
    assert st["kinds"]["iphone"]["installed"] is False and "gstreamer1.0-libav" in st["kinds"]["iphone"]["install"]
    assert "sudo apt install uxplay" in st["install_command"] and "scrcpy" in st["install_command"]
    assert not e.launcher.procs
    await e.svc.stop()
    e.ctx.db.close()


async def test_a_program_that_fails_to_start() -> None:
    e = Env()
    e.launcher.fail = True
    res = await e.svc.start("iphone")
    assert not res["ok"] and res["error"] == "failed" and not e.svc.active
    e.ctx.db.close()


async def test_the_parents_switch_and_the_config_switch(env: Env) -> None:
    env.ctx.settings.set("mirror.enabled", False)
    assert (await env.svc.start("iphone"))["error"] == "disabled" and not env.svc.offered()
    e = Env(enabled=False)
    assert (await e.svc.start("iphone"))["error"] == "unavailable"
    assert e.svc.status()["available"] is False
    e.ctx.db.close()
    assert (await env.svc.start("windows phone"))["error"] == "bad_kind"


async def test_headless_has_no_mirroring() -> None:
    ctx = make_test_context()
    ctx.config.display.headless = True
    svc = MirrorService(ctx)
    assert svc.unavailable_reason() == "no screen (headless run)"
    ctx.db.close()


# ---------------------------------------------------------------- pairing
async def test_pairing_sends_the_code_on_stdin_and_keeps_the_phone(env: Env) -> None:
    res = await env.svc.pair("192.168.1.23:37099", "123456", name="Ali's phone")
    assert res["ok"] and res["guid"] == "adb-R5CT-abc123" and res["connected"] is True
    argv, stdin = next(c for c in env.adb.calls if c[0][1] == "pair")
    assert argv == ["/usr/bin/adb", "pair", "192.168.1.23:37099"] and stdin == b"123456\n"
    phones = env.svc.status()["android_phones"]
    assert phones == [{"guid": "adb-R5CT-abc123", "address": "192.168.1.23", "name": "Ali's phone",
                       "paired_at": phones[0]["paired_at"]}]
    again = await env.svc.pair("192.168.1.23:40000", "654321")
    assert again["ok"] and len(env.svc.phones()) == 1  # the same phone, not twice


@pytest.mark.parametrize(("address", "code"), [
    ("192.168.1.23", "123456"), ("192.168.1.23:37099", "12345"), ("192.168.1.23:37099; rm -rf /", "123456"),
    ("192.168.1.23:37099", "12345a"), ("999.1.1.1:5555", "123456"), ("192.168.1.23:70000", "123456"),
    ("$(reboot):1", "123456"), ("192.168.1.23:37099", "1234567"), ("192.168.1.23:37099", "123 456"), ("phone.local:5555", "123456"),
])
async def test_pairing_input_is_checked(env: Env, address: str, code: str) -> None:
    res = await env.svc.pair(address, code)
    assert not res["ok"] and res["error"] == "bad_input" and env.adb.calls == []


async def test_a_wrong_code(env: Env) -> None:
    env.adb.pair_out = "Failed: Wrong password or connection was dropped.\n"
    res = await env.svc.pair("192.168.1.23:37099", "000000")
    assert not res["ok"] and res["error"] == "pair_failed" and env.svc.phones() == []


async def test_forgetting_a_phone(env: Env) -> None:
    env.pair()
    await env.svc.start("android")
    assert await env.svc.forget("adb-R5CT-abc123") is True
    assert env.svc.phones() == [] and not env.svc.active
    assert ["disconnect", "192.168.1.23:41235"] in env.adb.commands()
    assert await env.svc.forget("adb-R5CT-abc123") is False


def test_adb_output_parsing() -> None:
    assert A.parse_pair(PAIRED) == (True, "adb-R5CT-abc123")
    assert A.parse_pair("Failed: Wrong password") == (False, "")
    assert A.parse_mdns(MDNS) == [("adb-R5CT-abc123", "192.168.1.23:41235")]
    assert A.parse_devices(ONE_DEVICE + "emulator-5554\toffline\n") == [("192.168.1.23:41235", "device"),
                                                                        ("emulator-5554", "offline")]
    assert A.is_wifi("192.168.1.23:41235") and A.is_wifi("adb-X._adb-tls-connect._tcp") and not A.is_wifi("R5CT")
    assert A.connected("connected to 1.2.3.4:5") and A.connected("already connected to 1.2.3.4:5")
    assert not A.connected("failed to connect to 1.2.3.4:5")
    assert A.check_address(" 192.168.001.023:37099 ") == "192.168.1.23:37099"
    assert A.check_code("012345") == "012345"
