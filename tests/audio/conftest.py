"""Fixtures for the audio tests: mock-device services and a fake ``sounddevice`` module.

The fake PortAudio lets the *real* backends (SoundDeviceOutput / SoundDeviceInput)
run deterministically without hardware: device selection, sample-rate fallback,
stereo-only devices, PortAudio errors mid-stream and silent (unplugged) inputs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import numpy as np
import pytest

from littlebot.audio import devices
from littlebot.audio.devices import MockInput, MockOutput
from littlebot.audio.service import AudioService
from littlebot.audio.stt import MockSTT
from littlebot.audio.tts import MockTTS
from littlebot.core.clock import Clock
from littlebot.testing import make_test_context


# ------------------------------------------------------------------ fake PortAudio
class FakePortAudioError(Exception):
    pass


class FakeOutputStream:
    def __init__(self, sd: "FakeSD", device: int, samplerate: float, channels: int, dtype: str = "float32",
                 latency: Any = None, **_kw: Any) -> None:
        if sd.fail_open:
            raise FakePortAudioError("Device unavailable")
        self.sd, self.device, self.samplerate, self.channels = sd, device, samplerate, channels
        self.latency = 0.0
        self.active = False
        self.closed = False
        self.written = 0
        self.chunks: list[np.ndarray] = []
        self.aborted = 0
        sd.out_streams.append(self)

    def start(self) -> None:
        self.active = True

    def write(self, data: np.ndarray) -> bool:
        assert data.ndim == 2 and data.shape[1] == self.channels and data.dtype == np.float32
        if self.sd.fail_write_after is not None and self.written >= self.sd.fail_write_after:
            raise FakePortAudioError("Unanticipated host error (device unplugged)")
        self.chunks.append(data.copy())
        self.written += len(data)
        return False

    def stop(self) -> None:
        self.active = False

    def abort(self) -> None:
        self.active = False
        self.aborted += 1

    def close(self) -> None:
        self.active = False
        self.closed = True

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros((0, self.channels), np.float32)


class FakeInputStream:
    def __init__(self, sd: "FakeSD", device: int, samplerate: float, channels: int, dtype: str = "float32",
                 blocksize: int = 512, latency: Any = None, callback: Any = None, **_kw: Any) -> None:
        self.sd, self.device, self.samplerate, self.channels = sd, device, int(samplerate), channels
        self.blocksize = blocksize
        self.callback = callback
        self.active = False
        self.closed = False
        self._thread: threading.Thread | None = None
        self._t = 0
        sd.in_streams.append(self)

    def start(self) -> None:
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self.active:
            if not self.sd.input_dead:
                n = self.blocksize
                t = (self._t + np.arange(n)) / self.samplerate
                self._t += n
                block = (np.sin(2 * np.pi * 440 * t) * self.sd.input_level).astype(np.float32)
                self.callback(np.repeat(block[:, None], self.channels, axis=1), n, None, None)
            time.sleep(n / self.samplerate * 0.25)  # 4x faster than real time

    def abort(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False
        self.closed = True
        if self._thread is not None:
            self._thread.join(1.0)


class FakeSD:
    """Just enough of the ``sounddevice`` API for littlebot.audio.devices."""

    PortAudioError = FakePortAudioError

    def __init__(self, devices_: list[dict[str, Any]] | None = None) -> None:
        if devices_ is None:
            devices_ = [
                {"name": "vc4-hdmi-0: MAI PCM (hw:0,0)", "max_input_channels": 0, "max_output_channels": 2,
                 "default_samplerate": 48000.0},
                {"name": "USB Audio Device: - (hw:2,0)", "max_input_channels": 0, "max_output_channels": 2,
                 "default_samplerate": 48000.0},
                {"name": "USB PnP Sound Device: Mic (hw:3,0)", "max_input_channels": 1, "max_output_channels": 0,
                 "default_samplerate": 48000.0},
            ]
        self.devices = [{"index": i, "hostapi": 0, **d} for i, d in enumerate(devices_)]
        self.default_out = 1 if len(self.devices) > 1 else -1
        self.default_in = 2 if len(self.devices) > 2 else -1
        self.rates: dict[int, set[int]] = {i: {44100, 48000} for i in range(len(self.devices))}
        self.stereo_only: set[int] = {1}
        self.out_streams: list[FakeOutputStream] = []
        self.in_streams: list[FakeInputStream] = []
        self.fail_write_after: int | None = None
        self.fail_open = False
        self.input_level = 0.1
        self.input_dead = False
        self.reinits = 0

    def query_devices(self, device: Any = None, kind: str | None = None) -> Any:
        if kind is not None:
            idx = self.default_in if kind == "input" else self.default_out
            if idx < 0:
                raise FakePortAudioError(f"no default {kind} device")
            return dict(self.devices[idx])
        return [dict(d) for d in self.devices]

    def query_hostapis(self) -> tuple[dict[str, Any], ...]:
        return ({"name": "ALSA"},)

    def _check(self, device: int, channels: int, samplerate: float, kind: str) -> None:
        d = self.devices[device]
        if channels > d[f"max_{kind}_channels"]:
            raise FakePortAudioError("Invalid number of channels")
        if kind == "output" and device in self.stereo_only and channels != 2:
            raise FakePortAudioError("Invalid number of channels")
        if int(samplerate) not in self.rates[device]:
            raise FakePortAudioError("Invalid sample rate")

    def check_output_settings(self, device: int, channels: int, samplerate: float, dtype: str = "float32") -> None:
        self._check(device, channels, samplerate, "output")

    def check_input_settings(self, device: int, channels: int, samplerate: float, dtype: str = "float32") -> None:
        self._check(device, channels, samplerate, "input")

    def OutputStream(self, **kw: Any) -> FakeOutputStream:  # noqa: N802 - mirrors sounddevice
        return FakeOutputStream(self, **kw)

    def InputStream(self, **kw: Any) -> FakeInputStream:  # noqa: N802
        return FakeInputStream(self, **kw)

    def _terminate(self) -> None:
        self.reinits += 1

    def _initialize(self) -> None:
        pass

    def get_portaudio_version(self) -> tuple[int, str]:
        return (1, "PortAudio fake")


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> FakeSD:
    sd = FakeSD()
    monkeypatch.setattr(devices, "_sd_module", sd)
    monkeypatch.setattr(devices, "_sd_error", None)
    monkeypatch.setattr(devices, "_open_streams", 0)
    return sd


@pytest.fixture
def no_devices(monkeypatch: pytest.MonkeyPatch) -> FakeSD:
    """PortAudio present but no devices, and no SDL speaker either (deterministic on any machine)."""
    sd = FakeSD(devices_=[])
    monkeypatch.setattr(devices, "_sd_module", sd)
    monkeypatch.setattr(devices, "_sd_error", None)
    monkeypatch.setattr(devices, "_open_streams", 0)

    def _no_sdl(self: Any) -> None:
        self._set_status(devices.STATUS_UNAVAILABLE, "SDL audio driver is 'dummy' (no real speaker)")
        raise devices.AudioDeviceError(self.detail)

    monkeypatch.setattr(devices.PygameMixerOutput, "open", _no_sdl)
    return sd


# ------------------------------------------------------------------ contexts / services
class RecordingClock(Clock):
    """Instant clock that remembers requested sleeps."""

    def __init__(self) -> None:
        super().__init__(scale=0.0)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def recording_clock() -> RecordingClock:
    return RecordingClock()


@pytest.fixture
def actx():
    c = make_test_context(fakes=False)
    yield c
    c.db.close()


@pytest.fixture
async def make_service():
    """Factory: AudioService with mock devices/providers (overridable); stopped at teardown."""
    created: list[AudioService] = []

    def _make(ctx: Any, **kw: Any) -> AudioService:
        kw.setdefault("output", MockOutput(time_scale=0.0))
        kw.setdefault("input", MockInput())
        kw.setdefault("tts", MockTTS())
        kw.setdefault("stt", MockSTT())
        svc = AudioService(ctx, **kw)
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        await svc.stop()
