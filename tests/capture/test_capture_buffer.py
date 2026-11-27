""""Always recording" (littlebot/capture/buffer.py) with a fake camera, a fake ffmpeg, a fake battery and a clock
the tests turn: when it runs (auto / on / off; plugged in, on the battery, a low battery, no battery monitor), the
pieces rotating out and being wiped, the grace after unplugging, saving the right pieces into the gallery, what the
privacy switches do, one encoder at a time, the camera pausing, the indicator topic and the setting's validator.
And once with the real ffmpeg, when there is one."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from littlebot.capture import buffer as buf_mod
from littlebot.capture import recorder as rec_mod
from littlebot.capture.buffer import BUFFERING, OWNER, SETTING, SegmentRecorder, in_ram, span_words
from littlebot.capture.service import RECORDING, SAVED, CaptureService
from littlebot.core.events import Topics
from littlebot.core.settings import always_recording_value
from littlebot.skills.base import ANNOUNCE
from littlebot.testing import make_test_context
from littlebot.vision.service import PhotoUnavailable

WALL = 1_762_400_000.0


class FakeVision:
    def __init__(self) -> None:
        self.sinks: dict[str, Any] = {}
        self.fps: dict[str, float] = {}
        self.camera = "ok"
        self.mode = "active"

    def status(self) -> dict[str, Any]:
        return {"camera": self.camera, "detail": "", "detect_mode": self.mode}

    def add_frame_sink(self, owner: str, fn: Any, fps: float = 30.0) -> None:
        if self.camera != "ok":
            raise PhotoUnavailable("no_camera", "unplugged")
        self.sinks[owner] = fn
        self.fps[owner] = fps

    def remove_frame_sink(self, owner: str) -> bool:
        return self.sinks.pop(owner, None) is not None


class FakePower:
    """``ctx.power`` as the buffer reads it: ``fitted`` and ``state()``."""

    def __init__(self, *, fitted: bool = True, plugged: bool = True, percent: float | None = 80,
                 charging: bool | None = None) -> None:
        self.fitted, self.plugged, self.percent = fitted, plugged, percent
        self.charging = plugged if charging is None else charging
        self.health: list[str] = []

    def state(self) -> dict[str, Any]:
        return {"fitted": self.fitted, "percent": self.percent, "plugged_in": self.plugged,
                "charging": self.charging, "charged": self.plugged and not self.charging, "health": self.health}


class FakeFFmpeg:
    """The segment encoder: its first frame makes piece 0 of its run; it ends like ffmpeg (0 when the picture
    ends, 255 on Ctrl-C with sound)."""

    def __init__(self, argv: list[str]) -> None:
        self.argv, self.pattern, self.sound = argv, argv[-1], "pulse" in argv
        self.returncode: int | None = None
        self.written = 0
        self.killed = False
        self.stderr = io.BytesIO(b"")
        self._done = threading.Event()
        proc = self

        class _In:
            closed = False

            def write(self, data: bytes) -> int:
                if self.closed or proc.returncode is not None:
                    raise BrokenPipeError
                if not proc.written and "%" in proc.pattern:
                    Path(proc.pattern % 0).write_bytes(b"G" * 188)
                proc.written += len(data)
                return len(data)

            def close(self) -> None:
                self.closed = True
                if not proc.sound:
                    proc._exit(0)

        self.stdin = _In()

    def _exit(self, code: int) -> None:
        if self.returncode is None:
            if "%" not in self.pattern and code in (0, 255) and self.written:  # an asked-for recording's .mp4
                Path(self.pattern).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            self.returncode = code
            self._done.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return int(self.returncode or 0)

    def send_signal(self, sig: int) -> None:
        if sig == signal.SIGINT:
            self._exit(255)

    def terminate(self) -> None:
        self._exit(-15)

    def kill(self) -> None:
        self.killed = True
        self._exit(-9)


class Env:
    def __init__(self, tmp: Path, *, power: Any = None, sound: bool = False, **cfg: Any) -> None:
        self.ram = tmp / "ram"
        self.ctx = make_test_context(capture={"buffer_enabled": True, "buffer_dir": str(self.ram),
                                              "video_sound": sound, **cfg})
        self.vision = FakeVision()
        self.ctx.vision = self.vision
        self.ctx.power = power
        self.procs: list[FakeFFmpeg] = []
        self.joins: list[dict[str, Any]] = []
        self.join_ok = True
        self.t = [1000.0]
        self.wall = [WALL]
        self.svc = CaptureService(self.ctx, popen=self._popen, which=lambda cmd: f"/usr/bin/{cmd}",
                                  wall=lambda: self.wall[0], gallery_dir=tmp / "gallery", screen=False,
                                  run=self._run, buffer_ram_check=lambda _p: True, buffer_loop=False)
        self.buf = self.svc.buffer
        self.buf.clock = lambda: self.t[0]
        self.events: list[tuple[str, dict[str, Any]]] = []
        for topic in (SAVED, RECORDING, BUFFERING, ANNOUNCE):
            self.ctx.bus.subscribe(topic, lambda e, t=topic: self.events.append((t, dict(e.data))))

    def _popen(self, argv: list[str], **_kw: Any) -> FakeFFmpeg:
        p = FakeFFmpeg(list(argv))
        self.procs.append(p)
        return p

    def _run(self, argv: list[str], **_kw: Any) -> Any:
        """The join: which pieces, in which order, and the "video" it writes."""
        listing = Path(argv[argv.index("-i") + 1]).read_text(encoding="utf-8").splitlines()
        files = [Path(line[6:-1]).name for line in listing if line.startswith("file '")]
        self.joins.append({"argv": list(argv), "files": files})
        if not self.join_ok:
            return subprocess.CompletedProcess(argv, 1, b"", b"Invalid data found")
        Path(argv[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def topics(self, topic: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == topic]

    def feed(self, n: int = 30) -> None:
        fn = self.vision.sinks.get(OWNER)
        if fn is not None:
            for _ in range(n):
                fn(np.zeros((72, 128, 3), dtype=np.uint8), 0.0)

    async def second(self, *, frames: int = 30) -> str:
        """One second goes by (with the camera's pictures), then a tick (and the camera's next picture)."""
        self.feed(frames)
        self.t[0] += 1.0
        self.wall[0] += 1.0
        state = await self.buf.tick()
        if frames:
            self.feed(1)
        return state

    async def up(self) -> None:
        """Running: the sink in place, pictures coming, the fake ffmpeg writing."""
        await self.buf.tick()
        await self.runs(1)

    async def runs(self, encoders: int) -> None:
        """A second later it is running, on its ``encoders``-th ffmpeg (which got its first picture)."""
        assert await self.second() == "running", self.buf.status()
        _wait(lambda: len(self.procs) == encoders and self.procs[-1].written > 0)

    def piece(self, run: int, index: int, *, age: float = 0.0, size: int = 1000) -> Path:
        """A finished piece of ``run`` whose last byte was written ``age`` seconds ago."""
        path = self.ram / f"lbbuf-{run:04d}-{index:06d}.ts"
        with open(path, "wb") as f:
            f.truncate(size)
        os.utime(path, (self.wall[0] - age, self.wall[0] - age))
        return path

    def names(self) -> list[str]:
        return sorted(p.name for p in self.ram.glob("lbbuf-*.ts"))

    def live(self) -> list[FakeFFmpeg]:
        return [p for p in self.procs if p.returncode is None]


def _wait(cond: Any, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        time.sleep(0.01)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rec_mod, "AUDIO_PROBE_S", 0.1)
    monkeypatch.setattr(rec_mod, "SOUND_TAIL_S", 0.02)
    made: list[Env] = []

    async def _make(**kw: Any) -> Env:
        e = Env(tmp_path, **kw)
        await e.svc.start()
        made.append(e)
        return e

    yield _make
    for e in made:
        for p in e.live():
            p.kill()
        e.ctx.db.close()


# ---------------------------------------------------------------- the encoder
def test_the_command_writes_ten_second_ts_pieces_at_720p30(tmp_path: Path) -> None:
    rec = SegmentRecorder(tmp_path, 7, segment_seconds=10, cv2=None, audio_source="default")
    argv = rec.command(1280, 720, audio=True)
    assert argv[-1] == str(tmp_path / "lbbuf-0007-%06d.ts")
    assert argv[argv.index("-f", argv.index("-c:v")) + 1] == "segment"
    assert argv[argv.index("-segment_time") + 1] == "10" and argv[argv.index("-segment_format") + 1] == "mpegts"
    assert "-reset_timestamps" in argv and "+faststart" not in argv and "mp4" not in argv
    assert argv[argv.index("-video_size") + 1] == "1280x720" and argv[argv.index("-framerate") + 1] == "30"
    assert "libx264" in argv and "pulse" in argv and "aac" in argv
    assert "pulse" not in rec.command(1280, 720, audio=False)


async def test_it_runs_once_the_camera_delivers_and_says_so(env: Any) -> None:
    e = await env()  # no battery monitor: mains power
    assert await e.buf.tick() == "no_frames" and OWNER in e.vision.sinks and e.vision.fps[OWNER] == 30
    assert e.procs == [] and e.svc.buffering is False
    await e.runs(1)
    assert e.svc.buffering is True and e.topics(BUFFERING) == [{"active": True, "sound": False}]
    assert Path(e.procs[0].argv[-1]).parent == e.ram and e.names() == ["lbbuf-0001-000000.ts"]
    st = e.svc.status()["buffer"]
    assert st["running"] is True and st["state"] == "running" and st["setting"] == "auto" and st["detail"] is None
    assert st["minutes"] == 3 and st["plugged_in"] is True and st["battery"] is False and st["pieces"] == 1
    await e.svc.stop()  # shutdown: killed, and nothing left in RAM
    assert e.procs[0].killed and e.names() == [] and e.topics(BUFFERING)[-1]["active"] is False
    assert OWNER not in e.vision.sinks


async def test_the_folder_is_emptied_at_start_but_only_of_its_own_files(tmp_path: Path) -> None:
    ram = tmp_path / "ram"
    ram.mkdir()
    (ram / "lbbuf-0003-000001.ts").write_bytes(b"old")
    (ram / "holiday.ts").write_bytes(b"not ours")
    e = Env(tmp_path)
    try:
        await e.svc.start()
        assert e.names() == [] and (ram / "holiday.ts").is_file()
    finally:
        e.ctx.db.close()


async def test_a_folder_that_is_not_in_ram_is_refused(tmp_path: Path) -> None:
    e = Env(tmp_path)
    try:
        e.buf._ram_check = lambda _p: False  # noqa: SLF001
        await e.svc.start()
        assert await e.buf.tick() == "no_ram" and await e.second() == "no_ram"
        assert e.procs == [] and OWNER not in e.vision.sinks and "RAM" in e.buf.status()["detail"]
    finally:
        e.ctx.db.close()
    assert in_ram("/proc") is False or in_ram("/proc") is None  # (never a RAM disk; None off Linux)


async def test_the_master_switch_in_the_config(env: Any) -> None:
    e = await env(buffer_enabled=False)
    e.ctx.settings.set(SETTING, "on")
    assert await e.buf.tick() == "config" and await e.second() == "config"
    assert e.procs == [] and e.vision.sinks == {} and e.svc.status()["buffer"]["enabled"] is False
    assert (await e.svc.save_buffer())["error"] == "unavailable"


# ---------------------------------------------------------------- the pieces
async def test_old_pieces_go_as_new_ones_arrive(env: Any) -> None:
    e = await env()
    await e.up()
    for i, age in ((0, 410.0), (1, 400.0), (2, 200.0), (3, 181.0), (4, 170.0), (5, 20.0)):
        e.piece(1, i, age=age)
    e.piece(1, 6, age=0.0)  # (the one being written is never touched)
    assert await e.second() == "running"
    assert e.names() == ["lbbuf-0001-000004.ts", "lbbuf-0001-000005.ts", "lbbuf-0001-000006.ts"]
    assert e.buf.status()["seconds"] <= 190


async def test_the_folder_never_passes_its_cap(env: Any) -> None:
    e = await env(buffer_max_mb=16)
    await e.up()
    for i in range(1, 6):
        e.piece(1, i, age=50.0 - i, size=5 * 1024 * 1024)
    await e.second()
    assert e.names() == ["lbbuf-0001-000003.ts", "lbbuf-0001-000004.ts", "lbbuf-0001-000005.ts"]
    assert e.buf.status()["mb"] <= 16


# ---------------------------------------------------------------- auto / on / off and the battery
@pytest.mark.parametrize(("power", "mode", "want"), [
    (None, "auto", "running"),  # no battery monitor at all
    (FakePower(fitted=False, plugged=False), "auto", "running"),  # ... or none found: mains power
    (FakePower(plugged=True, charging=True), "auto", "running"),
    (FakePower(plugged=True, charging=False, percent=100), "auto", "running"),  # full, on the charger
    (FakePower(plugged=False), "auto", "on_battery"),
    (FakePower(plugged=False), "on", "running"),
    (FakePower(plugged=True), "on", "running"),
    (FakePower(plugged=True), "off", "off"),
    (None, "off", "off"),
])
async def test_when_it_runs(env: Any, power: Any, mode: str, want: str) -> None:
    e = await env(power=power)
    e.ctx.settings.set(SETTING, mode)
    await e.buf.tick()
    assert await e.second() == want
    if want == "running":
        _wait(lambda: e.procs and e.procs[-1].written > 0)
    assert e.svc.buffering is (want == "running") and bool(e.live()) is (want == "running")
    assert e.ctx.settings.get(SETTING) == mode


async def test_unplugged_in_auto_what_was_buffered_stays_three_more_minutes(env: Any) -> None:
    power = FakePower(plugged=True)
    e = await env(power=power)
    await e.up()
    for i in range(1, 8):
        e.piece(1, i, age=70.0 - 10 * i)
    power.plugged = False
    e.ctx.bus.publish(Topics.POWER_UNPLUGGED, {"percent": 80}, source="power")
    assert await e.buf.tick() == "on_battery"
    assert e.live() == [] and not e.procs[0].killed  # ended properly: its last piece is whole
    assert len(e.names()) == 8 and OWNER not in e.vision.sinks
    assert e.topics(BUFFERING)[-1]["active"] is False
    st = e.buf.status()
    assert st["running"] is False and st["wipes_in"] == 180 and st["seconds"] > 60 and "battery" in st["detail"]
    e.t[0] += 179.0
    assert await e.buf.tick() == "on_battery" and len(e.names()) == 8
    res = await e.svc.save_buffer(source="dashboard")  # still saveable
    assert res["ok"] is True and len(e.joins[0]["files"]) == 8
    e.t[0] += 2.0
    assert await e.buf.tick() == "on_battery" and e.names() == []  # ... then wiped
    assert e.buf.status()["seconds"] == 0 and e.buf.status()["wipes_in"] is None
    assert (await e.svc.save_buffer())["error"] == "nothing_buffered"
    power.plugged = True  # back on the charger: it films again
    await e.buf.tick()
    await e.runs(2)


async def test_on_lasts_until_the_battery_is_low_then_auto(env: Any) -> None:
    power = FakePower(plugged=False, percent=60)
    e = await env(power=power)
    res = await e.svc.set_always_recording("on", source="voice")
    assert res["ok"] and res["setting"] == "on" and res["on_battery"] is True
    assert await e.second() == "running"
    power.percent = 21
    assert await e.second() == "running" and e.ctx.settings.get(SETTING) == "on"
    power.percent = 20  # the power policy's low level (power.low_percent)
    e.ctx.bus.publish(Topics.POWER_LOW, {"percent": 20}, source="power")
    assert await e.second() == "on_battery"
    assert e.ctx.settings.get(SETTING) == "auto" and e.live() == []
    assert [d["text"] for d in e.topics(ANNOUNCE)] == [buf_mod.LOW_BATTERY_LINE]
    assert await e.second() == "on_battery" and len(e.topics(ANNOUNCE)) == 1  # said once
    again = await e.svc.set_always_recording("on", source="screen")  # still low: it won't stay on
    assert again["setting"] == "auto" and again["state"] == "on_battery"


async def test_the_low_alarm_alone_counts_as_low(env: Any) -> None:
    power = FakePower(plugged=False, percent=None)  # (no percentage yet, but the policy latched "low")
    power.health = ["low"]
    e = await env(power=power)
    e.ctx.settings.set(SETTING, "on")
    assert await e.buf.tick() == "on_battery" and e.ctx.settings.get(SETTING) == "auto"


async def test_plugging_back_in_puts_on_back_to_auto(env: Any) -> None:
    power = FakePower(plugged=True)
    e = await env(power=power)
    e.ctx.settings.set(SETTING, "on")  # chosen on the charger: stays "on" ...
    await e.up()
    assert e.ctx.settings.get(SETTING) == "on"
    power.plugged = False  # ... carries on off the charger ...
    assert await e.second() == "running" and e.ctx.settings.get(SETTING) == "on"
    power.plugged = True  # ... and is "auto" again once it is back
    assert await e.second() == "running" and e.ctx.settings.get(SETTING) == "auto"
    assert len(e.procs) == 1 and e.topics(ANNOUNCE) == []  # the same encoder all along, nothing said


async def test_off_wipes_at_once(env: Any) -> None:
    e = await env()
    await e.up()
    e.piece(1, 1, age=5.0)
    res = await e.svc.set_always_recording("off", source="dashboard")
    assert res["ok"] and res["setting"] == "off" and res["state"] == "off"
    assert e.names() == [] and e.live() == [] and e.vision.sinks == {}
    assert (await e.svc.set_always_recording("sometimes"))["ok"] is False


async def test_the_parents_switch_stops_and_wipes_it(env: Any) -> None:
    e = await env()
    await e.up()
    e.ctx.settings.set("capture.enabled", False, source="dashboard")
    assert await e.buf.tick() == "disabled" and e.names() == [] and e.live() == []


# ---------------------------------------------------------------- saving
async def test_save_joins_the_newest_pieces_into_the_gallery(env: Any) -> None:
    e = await env()
    await e.up()
    for i in range(1, 12):
        e.piece(1, i, age=115.0 - 10 * i)
    e.feed(115 * 30)
    e.t[0] += 115.0  # the encoder has run 115 s: pieces 0..10 are 10 s each, piece 11 is 5 s
    res = await e.svc.save_buffer(60, source="voice", who="Ali")
    assert res["ok"] is True and res["words"] == "Saved the last minute." and res["sound"] is False
    join = e.joins[0]
    assert join["files"] == [f"lbbuf-0001-{i:06d}.ts" for i in range(5, 12)]  # 6 whole pieces and the last 5 s
    assert res["seconds"] == pytest.approx(65.0)
    argv = join["argv"]
    assert argv[argv.index("-f") + 1] == "concat" and argv[argv.index("-c") + 1] == "copy"
    assert "+faststart" in argv and argv[-3:-1] == ["-f", "mp4"] and "0:a:0?" not in argv
    assert not (e.ram / buf_mod.JOIN_LIST).exists()
    item = e.svc.gallery.get(res["item"]["id"])
    assert item is not None and item.kind == "video" and item.seconds == pytest.approx(65.0)
    assert item.source == "voice" and item.who == "Ali" and item.taken_at == pytest.approx(e.wall[0] - 65.0)
    assert e.svc.gallery.file(item).parent != e.ram and e.svc.gallery.file(item).stat().st_size > 0
    assert e.topics(SAVED) == [{"id": item.id, "kind": "video", "source": "voice"}]
    assert e.svc.share(item.id)["ok"] is True  # sent to a phone like any video
    # the encoder ended (so the last seconds were whole) and a new one runs; the pieces are still there
    assert e.procs[0].returncode == 0 and e.buf.state == "running"
    e.feed(1)
    _wait(lambda: len(e.procs) == 2 and e.procs[1].written > 0)
    assert len(e.names()) == 13
    everything = await e.svc.save_buffer(announce=True)
    assert everything["ok"] and len(e.joins[1]["files"]) == 13 and everything["words"] == "Saved the last 2 minutes."
    assert e.topics(ANNOUNCE)[-1]["text"] == "Saved the last 2 minutes."


async def test_save_with_nothing_buffered_or_a_join_that_fails(env: Any) -> None:
    e = await env()
    e.ctx.settings.set(SETTING, "off")
    await e.buf.tick()
    res = await e.svc.save_buffer(announce=True)
    assert res["ok"] is False and res["error"] == "nothing_buffered" and "Always recording is off" in res["detail"]
    assert e.topics(ANNOUNCE)[-1]["text"] == res["detail"] and e.joins == []
    e.ctx.settings.set(SETTING, "auto")
    await e.up()
    e.join_ok = False
    res = await e.svc.save_buffer()
    assert res["ok"] is False and res["error"] == "failed"
    assert e.svc.gallery.items() == [] and list(e.svc.gallery.root.glob("*.part")) == []
    assert e.buf.state == "running"  # and it films on


def test_what_it_says_it_saved() -> None:
    assert span_words(183) == "the last 3 minutes" and span_words(65) == "the last minute"
    assert span_words(40) == "the last 40 seconds" and span_words(118) == "the last 2 minutes"


# ---------------------------------------------------------------- privacy
async def test_the_camera_switch_wipes_at_once(env: Any) -> None:
    e = await env()
    await e.up()
    e.piece(1, 1, age=30.0)
    e.ctx.privacy.set(camera_enabled=False)
    e.ctx.bus.publish(Topics.PRIVACY_CHANGED, e.ctx.privacy.snapshot(), source="test")
    # in the switch's own callback, before any tick: killed (not finished) and nothing left in RAM
    assert e.procs[0].killed and e.names() == [] and e.topics(BUFFERING)[-1]["active"] is False
    assert await e.buf.tick() == "camera_off" and e.names() == [] and e.vision.sinks == {}
    assert (await e.svc.save_buffer())["error"] == "nothing_buffered"
    e.ctx.privacy.set(camera_enabled=True)
    await e.buf.tick()
    await e.runs(2)


async def test_the_microphone_switch_takes_the_sound_out_of_new_pieces(env: Any) -> None:
    e = await env(sound=True)
    await e.up()
    _wait(lambda: e.buf._rec is not None and e.buf._rec.state == "recording")  # noqa: SLF001
    assert e.procs[0].sound and e.topics(BUFFERING)[0] == {"active": True, "sound": True}
    assert e.buf.status()["sound"] is True
    e.ctx.privacy.set(microphone_enabled=False)
    await e.runs(2)
    assert e.procs[0].returncode == 255 and not e.procs[1].sound
    assert e.names() == ["lbbuf-0001-000000.ts", "lbbuf-0002-000000.ts"] and e.buf.status()["sound"] is False
    res = await e.svc.save_buffer()  # pieces with and without sound: the clip is saved without
    assert res["ok"] and res["sound"] is False and "0:a:0?" not in e.joins[0]["argv"]


async def test_a_clip_all_with_sound_keeps_it(env: Any) -> None:
    e = await env(sound=True)
    await e.up()
    _wait(lambda: e.buf._rec is not None and e.buf._rec.state == "recording")  # noqa: SLF001
    res = await e.svc.save_buffer()
    assert res["ok"] and res["sound"] is True and "0:a:0?" in e.joins[0]["argv"]


# ---------------------------------------------------------------- one encoder, the camera pausing
async def test_it_pauses_while_a_video_is_recorded_and_resumes(env: Any) -> None:
    e = await env()
    await e.up()
    started = await e.svc.start_video(source="voice")
    assert started["ok"] is True
    assert e.procs[0].returncode == 0  # the buffer's encoder ended before the recording's started
    assert e.buf.state == "recording" and e.svc.buffering is False and OWNER not in e.vision.sinks
    assert e.topics(BUFFERING)[-1]["active"] is False and len(e.names()) == 1  # what it had is kept
    for _ in range(3):
        assert await e.second() == "recording"
    assert len(e.procs) <= 2 and sum(".ts" in p.argv[-1] for p in e.live()) == 0
    fn = e.vision.sinks["capture"]
    fn(np.zeros((72, 128, 3), dtype=np.uint8), 0.0)
    _wait(lambda: len(e.procs) == 2 and e.procs[1].written > 0)
    assert (await e.svc.stop_video("asked"))["ok"] is True
    assert await e.buf.tick() == "no_frames"  # its sink is back; the next second's pictures start it
    await e.runs(3)
    assert e.svc.buffering is True and e.procs[2].argv[-1].endswith("lbbuf-0002-%06d.ts")


async def test_it_pauses_when_vision_pauses_the_camera_and_when_pictures_stop(env: Any) -> None:
    e = await env()
    await e.up()
    e.vision.mode = "paused"  # a YouTube video plays
    e.ctx.bus.publish(Topics.STATE_CHANGED, {"old": "IDLE", "new": "PLAYING_MEDIA"}, source="test")
    assert await e.buf.tick() == "camera_paused" and e.live() == [] and OWNER in e.vision.sinks
    assert len(e.names()) == 1
    e.vision.mode = "active"
    await e.runs(2)
    # asleep with pictures still coming: it carries on ...
    e.ctx.bus.publish(Topics.STATE_CHANGED, {"old": "IDLE", "new": "SLEEPING"}, source="test")
    for _ in range(5):
        assert await e.second() == "running"
    # ... but with (almost) none it pauses after a few seconds, and resumes when they come back
    assert await e.second(frames=0) == "running" and await e.second(frames=0) == "running"
    assert await e.second(frames=0) == "no_frames" and e.live() == [] and e.svc.buffering is False
    assert await e.second(frames=0) == "no_frames"
    await e.runs(3)


async def test_it_pauses_while_the_camera_is_lent_to_a_video_call_and_resumes(env: Any) -> None:
    """WhatsApp has the camera (littlebot/calls): no error, nothing wiped at once, and it carries on afterwards."""
    e = await env()
    await e.up()
    before = e.names()
    assert len(before) == 1
    e.vision.camera = "lent"  # vision closed the device for the call
    assert await e.buf.tick() == "camera_lent"
    assert e.live() == [] and e.svc.buffering is False
    assert OWNER in e.vision.sinks  # its sink stays, to see the pictures come back
    assert e.names() == before  # what was buffered is still there (saveable for buffer_minutes more)
    for _ in range(5):
        assert await e.second(frames=0) == "camera_lent"
    assert e.names() == before and e.buf.status()["state"] == "camera_lent"
    e.vision.camera = "ok"  # the call ended: the camera is back
    assert await e.buf.tick() == "no_frames"
    await e.runs(2)
    assert e.svc.buffering is True


async def test_the_encoder_stopping_by_itself_is_tried_again_later(env: Any) -> None:
    e = await env()
    await e.up()
    e.procs[0].kill()  # (ffmpeg crashed)
    _wait(lambda: not e.buf._rec.running)  # noqa: SLF001
    assert await e.second() == "retrying" and len(e.procs) == 1
    assert await e.second() == "retrying"
    e.t[0] += buf_mod.RETRY_S
    await e.second()
    await e.runs(2)


async def test_a_lost_camera(env: Any) -> None:
    e = await env()
    e.vision.camera = "disconnected"
    assert await e.buf.tick() == "no_camera" and await e.second() == "no_camera" and e.procs == []


# ---------------------------------------------------------------- the setting
def test_the_settings_validator() -> None:
    assert [always_recording_value(v) for v in ("auto", "ON", " off ", True, False, "automatic")] == \
        ["auto", "on", "off", "on", "off", "auto"]
    for bad in ("sometimes", "", None, 3):
        with pytest.raises(ValueError):
            always_recording_value(bad)
    ctx = make_test_context()
    try:
        assert ctx.settings.get(SETTING) == "auto"  # the default
        assert ctx.settings.set(SETTING, "On") == "on" and ctx.settings.get(SETTING) == "on"
        with pytest.raises(ValueError):
            ctx.settings.set(SETTING, "maybe")
        assert ctx.config.capture.buffer_enabled is False  # (tests opt in)
        assert ctx.config.capture.buffer_minutes == 3 and ctx.config.capture.buffer_fps == 30
        assert ctx.config.capture.buffer_dir.startswith("/dev/shm/")
    finally:
        ctx.db.close()


# ---------------------------------------------------------------- the real ffmpeg
def _real_ffmpeg() -> str | None:
    path = shutil.which("ffmpeg")
    if path is None or shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return path if b"libx264" in out else None


async def test_end_to_end_with_the_real_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = _real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg with libx264 here")
    ctx = make_test_context(capture={"buffer_enabled": True, "buffer_dir": str(tmp_path / "ram"),
                                     "buffer_segment_seconds": 2, "video_sound": False, "buffer_width": 320})
    ctx.vision = FakeVision()
    svc = CaptureService(ctx, gallery_dir=tmp_path / "gallery", screen=False, buffer_ram_check=lambda _p: True,
                         buffer_loop=False)
    stop = threading.Event()

    def camera() -> None:  # 30 pictures a second, each a little brighter
        n = 0
        while not stop.is_set():
            fn = ctx.vision.sinks.get(OWNER)
            if fn is not None:
                fn(np.full((180, 320, 3), n % 250, dtype=np.uint8), time.monotonic())
                n += 1
            time.sleep(1 / 30)

    feeder = threading.Thread(target=camera, daemon=True)
    try:
        await svc.start()
        await svc.buffer.tick()
        feeder.start()
        t0 = time.monotonic()
        while await svc.buffer.tick() != "running":
            assert time.monotonic() - t0 < 10, svc.buffer.status()
            time.sleep(0.2)
        ram = tmp_path / "ram"
        while len(list(ram.glob("lbbuf-*.ts"))) < 3:  # two whole pieces and the one being written
            assert time.monotonic() - t0 < 40, svc.buffer.status()
            time.sleep(0.2)
        res = await svc.save_buffer(source="dashboard")
        assert res["ok"] is True, res
        item = svc.gallery.get(res["item"]["id"])
        assert item is not None and (item.width, item.height) == (320, 180)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,width,height:format=duration,format_name", "-of",
                                "default=nw=1", str(svc.gallery.file(item))], capture_output=True, timeout=30)
        text = probe.stdout.decode()
        assert probe.returncode == 0 and "codec_name=h264" in text and "width=320" in text and "mp4" in text
        seconds = float(next(line.split("=")[1] for line in text.splitlines() if line.startswith("duration=")))
        assert 3.5 < seconds < 30 and abs(seconds - res["seconds"]) < 4.0
        assert await svc.buffer.tick() == "running"  # and it films on
    finally:
        stop.set()
        await svc.stop()
        ctx.db.close()
    assert list((tmp_path / "ram").glob("lbbuf-*")) == []
