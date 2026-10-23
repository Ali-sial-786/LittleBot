"""Device backends: enumeration, selection, fallbacks, failures (fake PortAudio + mocks)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from littlebot.audio import devices as dev
from littlebot.audio.devices import (
    AudioDeviceError, MockInput, MockOutput, NullInput, NullOutput, PlaybackControl, PygameMixerOutput,
    SoundDeviceInput, SoundDeviceOutput,
)


def _tone(secs: float = 0.3, sr: int = 22050, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(secs * sr)) / sr
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


# ------------------------------------------------------------------ enumeration / selection
def test_list_devices_splits_inputs_and_outputs(fake_sd):
    listing = dev.list_devices()
    assert [d["index"] for d in listing["outputs"]] == [0, 1]
    assert [d["index"] for d in listing["inputs"]] == [2]
    assert listing["outputs"][1]["default"] and not listing["outputs"][0]["default"]
    assert listing["inputs"][0]["default"] and listing["inputs"][0]["channels"] == 1
    assert listing["outputs"][0]["default_samplerate"] == 48000.0 and listing["outputs"][0]["hostapi"] == "ALSA"
    assert listing["portaudio"] is True


def test_find_device_by_default_index_and_name(fake_sd):
    assert dev.find_device(None, "output")["index"] == 1
    assert dev.find_device("default", "input")["index"] == 2
    assert dev.find_device(0, "output")["index"] == 0
    assert dev.find_device("0", "output")["index"] == 0
    assert dev.find_device("usb audio", "output")["index"] == 1  # case-insensitive substring
    assert dev.find_device("PnP", "input")["index"] == 2
    with pytest.raises(AudioDeviceError, match="no output device matching"):
        dev.find_device("bluetooth", "output")
    with pytest.raises(AudioDeviceError, match="not found"):
        dev.find_device(2, "output")  # #2 is input-only
    fake_sd.default_out = -1
    assert dev.find_device(None, "output")["index"] == 1  # no system default: the USB speaker, not HDMI


def test_default_is_the_usb_speaker_not_hdmi(fake_sd):
    fake_sd.default_out = 0  # the desktop's default output is the HDMI screen
    assert dev.find_device(None, "output")["index"] == 1
    assert dev.find_device("default", "output")["index"] == 1
    assert dev.find_device("system", "output")["index"] == 0  # explicit opt-in to the desktop default
    assert dev.find_device("hdmi", "output")["index"] == 0  # or name it


def test_without_usb_the_default_avoids_hdmi_when_it_can(fake_sd):
    fake_sd.devices = [{"index": i, "hostapi": 0, **d} for i, d in enumerate([
        {"name": "vc4-hdmi-0: MAI PCM (hw:0,0)", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        {"name": "Headphones: bcm2835 (hw:1,0)", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
    ])]
    fake_sd.default_out = 0
    assert dev.find_device(None, "output")["index"] == 1
    fake_sd.devices = fake_sd.devices[:1]
    assert dev.find_device(None, "output")["index"] == 0  # HDMI is all there is


def test_no_devices_reports_cleanly(no_devices):
    assert dev.list_devices()["inputs"] == [] and dev.list_devices()["detail"] == "no audio devices found"
    with pytest.raises(AudioDeviceError, match="no output devices"):
        dev.find_device(None, "output")


# ------------------------------------------------------------------ SoundDeviceOutput
def test_output_negotiates_rate_and_stereo_and_applies_gain(fake_sd):
    out = SoundDeviceOutput("USB Audio", sample_rate=22050)
    out.open()
    assert out.status == dev.STATUS_OK and out.device_name.startswith("USB Audio")
    assert out.sample_rate == 48000  # 22050 unsupported -> device default
    ctrl = PlaybackControl(gain=lambda: 0.5)
    assert out.play(_tone(0.3), 22050, ctrl) is True
    stream = fake_sd.out_streams[-1]
    audio = stream.audio()
    assert stream.channels == 2 and audio.shape[1] == 2  # stereo-only USB speaker: duplicated mono
    lead = int(out.lead_in_seconds * 48000)
    assert lead > 0 and not np.any(audio[:lead])  # silent lead-in when the stream (re)starts
    assert len(audio) - lead == pytest.approx(0.3 * 48000, abs=2)  # resampled
    assert float(np.max(np.abs(audio))) == pytest.approx(0.25, abs=0.02)  # 0.5 amplitude * 0.5 gain
    out.release()
    assert stream.closed


def test_output_progress_and_stop(fake_sd):
    out = SoundDeviceOutput(None)
    out.open()
    seen: list[float] = []
    stop = threading.Event()

    def progress(t: float) -> None:
        seen.append(t)
        if t > 0.1:
            stop.set()

    ok = out.play(_tone(1.0), 22050, PlaybackControl(stop, progress=progress))
    assert ok is False and fake_sd.out_streams[-1].aborted >= 1
    assert seen and seen == sorted(seen)
    out.release()


def test_output_failure_mid_stream_then_reconnect(fake_sd):
    out = SoundDeviceOutput(None)
    out.open()
    fake_sd.fail_write_after = 2048
    with pytest.raises(AudioDeviceError, match="speaker failed"):
        out.play(_tone(1.0), 22050, PlaybackControl())
    assert out.status == dev.STATUS_DISCONNECTED and fake_sd.out_streams[-1].closed
    fake_sd.fail_write_after = None
    out.open()  # re-plugged
    assert out.status == dev.STATUS_OK
    assert out.play(_tone(0.1), 22050, PlaybackControl()) is True


def test_output_open_failure_is_reported(fake_sd):
    fake_sd.rates[1] = set()  # device refuses every rate
    out = SoundDeviceOutput(1)
    with pytest.raises(AudioDeviceError, match="no supported sample rate"):
        out.open()
    assert out.status == dev.STATUS_UNAVAILABLE


def test_refresh_portaudio_skipped_while_streams_open(fake_sd):
    assert dev.refresh_portaudio() is True and fake_sd.reinits == 1
    out = SoundDeviceOutput(None)
    out.open()
    out._ensure_stream()
    assert dev.refresh_portaudio() is False and fake_sd.reinits == 1
    out.release()
    assert dev.refresh_portaudio() is True


# ------------------------------------------------------------------ SoundDeviceInput
def test_input_streams_resampled_chunks_and_closes(fake_sd):
    inp = SoundDeviceInput("pnp", sample_rate=16000)
    inp.probe()
    assert inp.status == dev.STATUS_OK and not inp.is_open
    inp.open()
    assert inp.is_open
    got = []
    deadline = time.monotonic() + 3
    while sum(len(c) for c in got) < 8000 and time.monotonic() < deadline:
        c = inp.read(0.2)
        if c is not None:
            got.append(c)
    audio = np.concatenate(got)
    assert len(audio) >= 8000 and audio.dtype == np.float32
    assert fake_sd.in_streams[-1].samplerate == 48000  # 16 kHz unsupported -> device rate, resampled for us
    assert 0.05 < float(np.max(np.abs(audio))) < 0.11
    assert dev.dsp.rms(audio) == pytest.approx(0.1 / np.sqrt(2), rel=0.15)  # 440 Hz sine survives resampling
    inp.close()
    assert not inp.is_open and fake_sd.in_streams[-1].closed
    assert inp.read(0.01) is None


def test_input_stall_is_detected_as_disconnect(fake_sd):
    inp = SoundDeviceInput(None, stall_seconds=0.2)
    inp.open()
    fake_sd.input_dead = True  # USB mic unplugged: callbacks stop
    time.sleep(0.05)
    inp.flush()
    with pytest.raises(AudioDeviceError, match="stopped delivering"):
        for _ in range(20):
            inp.read(0.05)
    assert inp.status == dev.STATUS_DISCONNECTED and not inp.is_open


def test_input_missing_device(no_devices):
    inp = SoundDeviceInput("usb")
    with pytest.raises(AudioDeviceError):
        inp.open()
    assert inp.status == dev.STATUS_UNAVAILABLE


# ------------------------------------------------------------------ resolution (auto)
def test_auto_degrades_to_simulated_devices(no_devices):
    out = dev.resolve_output("auto", None, 22050, time_scale=0)
    assert isinstance(out.backend, MockOutput) and out.degraded and "no usable speaker" in out.note
    assert out.backend.status == dev.STATUS_SIMULATED
    inp = dev.resolve_input("auto", None)
    assert isinstance(inp.backend, MockInput) and inp.degraded


def test_explicit_real_backends_stay_unavailable_without_raising(no_devices):
    out = dev.resolve_output("sounddevice", "USB", 22050)
    assert isinstance(out.backend, SoundDeviceOutput) and out.backend.status == dev.STATUS_UNAVAILABLE
    inp = dev.resolve_input("real", None)
    assert isinstance(inp.backend, SoundDeviceInput) and inp.backend.status == dev.STATUS_UNAVAILABLE


def test_auto_prefers_real_devices(fake_sd):
    assert isinstance(dev.resolve_output("auto", None).backend, SoundDeviceOutput)
    assert isinstance(dev.resolve_input("auto", None).backend, SoundDeviceInput)
    assert isinstance(dev.resolve_output("none").backend, NullOutput)
    assert isinstance(dev.resolve_input("none").backend, NullInput)
    assert isinstance(dev.resolve_output("mock").backend, MockOutput)


# ------------------------------------------------------------------ pygame fallback
def test_pygame_dummy_driver_is_not_a_real_speaker():
    pytest.importorskip("pygame")
    out = PygameMixerOutput(None)
    with pytest.raises(AudioDeviceError, match="dummy|could not open"):
        out.open()
    assert out.status == dev.STATUS_UNAVAILABLE


def test_pygame_backend_plays_with_dummy_driver_when_allowed(monkeypatch):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    already = pygame.mixer.get_init()
    out = PygameMixerOutput(None, allow_dummy=True)
    try:
        out.open()
        assert out.status == dev.STATUS_OK
        ticks: list[float] = []
        assert out.play(_tone(0.15), 22050, PlaybackControl(gain=lambda: 0.3, progress=ticks.append)) is True
        assert ticks
        stop = threading.Event()
        stop.set()
        assert out.play(_tone(1.0), 22050, PlaybackControl(stop)) is False
    finally:
        if not already:
            out.close()


# ------------------------------------------------------------------ mocks
def test_mock_output_records_and_simulates_time():
    out = MockOutput(time_scale=0.0)
    t0 = time.monotonic()
    assert out.play(_tone(2.0), 22050, PlaybackControl(gain=lambda: 0.4, label="sound:x")) is True
    assert time.monotonic() - t0 < 0.2
    rec = out.played[-1]
    assert rec.label == "sound:x" and rec.completed and rec.duration == pytest.approx(2.0, abs=0.01)
    assert rec.peak == pytest.approx(0.2, abs=0.01)
    slow = MockOutput(time_scale=0.1)
    t0 = time.monotonic()
    slow.play(_tone(1.0), 22050, PlaybackControl())
    assert 0.07 < time.monotonic() - t0 < 0.5


def test_mock_output_simulated_failures():
    out = MockOutput(time_scale=0.0, fail_after_seconds=0.2)
    with pytest.raises(AudioDeviceError):
        out.play(_tone(1.0), 22050, PlaybackControl())
    assert out.status == dev.STATUS_DISCONNECTED and out.played[-1].completed is False
    missing = MockOutput(fail_on_open=True)
    with pytest.raises(AudioDeviceError):
        missing.open()


def test_mock_input_injected_speech_and_noise_floor():
    inp = MockInput(noise_level=0.001)
    assert inp.read(0.1) is None  # closed
    inp.open()
    inp.inject_speech(0.5, level=0.1, trailing_silence=0.2)
    n = inp.pending_chunks()
    assert n > 10
    chunks = [inp.read(0.1) for _ in range(n)]
    loud = max(dev.dsp.rms(c) for c in chunks)
    assert loud > 0.05
    t0 = time.monotonic()
    floor = inp.read(1.0)  # live noise floor is paced like a real mic
    assert floor is not None and dev.dsp.rms(floor) < 0.005 and time.monotonic() - t0 >= 0.02
    inp.close()
    assert inp.read(0.1) is None
