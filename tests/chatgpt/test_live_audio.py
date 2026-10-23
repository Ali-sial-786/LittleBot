"""LiveAudio (the real mic/speaker bridge) with a fake PortAudio: rates, half-duplex gate, mute, no backlog."""

from __future__ import annotations

import asyncio
import fractions
from typing import Any

import numpy as np
import pytest

av = pytest.importorskip("av")

from littlebot.audio import devices as dev  # noqa: E402
from littlebot.chatgpt.live_audio import FRAME, RATE, LiveAudio  # noqa: E402

DEVICES = [
    {"index": 0, "name": "USB PnP Sound Device", "hostapi": "ALSA", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 44100.0, "default_input": True, "default_output": False},
    {"index": 1, "name": "UACDemoV1.0", "hostapi": "ALSA", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0, "default_input": False, "default_output": True},
]


class FakeStream:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.started = self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeSD:
    def __init__(self, mic_rates: tuple[int, ...] = (44100,), out_rates: tuple[int, ...] = (48000,)) -> None:
        self.mic_rates, self.out_rates = mic_rates, out_rates
        self.streams: list[FakeStream] = []

    def check_input_settings(self, *, device: int, channels: int, samplerate: int, dtype: str) -> None:
        if samplerate not in self.mic_rates:
            raise ValueError("rate")

    def check_output_settings(self, *, device: int, channels: int, samplerate: int, dtype: str) -> None:
        if samplerate not in self.out_rates or channels != 2:
            raise ValueError("layout")

    def InputStream(self, **kw: Any) -> FakeStream:  # noqa: N802
        s = FakeStream(**kw)
        self.streams.append(s)
        return s

    def OutputStream(self, **kw: Any) -> FakeStream:  # noqa: N802
        s = FakeStream(**kw)
        self.streams.append(s)
        return s


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(dev, "query_devices", lambda: [dict(d) for d in DEVICES])
    clock = Clock()
    sd = FakeSD()
    audio = LiveAudio(mic="USB PnP", speaker="UACDemo", volume=lambda: 0.5, sd=sd, now=clock, echo_tail_s=0.45)
    return audio, sd, clock


def voice_frame(n: int = 960, amp: int = 8000) -> Any:
    pcm = (np.sin(np.arange(n) / 48000 * 2 * np.pi * 440) * amp).astype(np.int16)
    stereo = np.repeat(pcm, 2).reshape(1, -1)  # aiortc's Opus decoder gives packed stereo s16
    f = av.AudioFrame.from_ndarray(stereo, format="s16", layout="stereo")
    f.sample_rate, f.pts, f.time_base = 48000, 0, fractions.Fraction(1, 48000)
    return f


async def test_opens_both_devices_at_supported_rates(setup):
    audio, sd, _ = setup
    audio.open(asyncio.get_running_loop())
    try:
        assert audio.mic_rate == 44100 and audio.out_rate == 48000
        out, inp = sd.streams
        assert out.kw["device"] == 1 and out.kw["channels"] == 2 and out.started
        assert inp.kw["device"] == 0 and inp.kw["samplerate"] == 44100 and inp.started
        assert audio.device_names == ("USB PnP Sound Device", "UACDemoV1.0")
    finally:
        audio.close()
    assert all(s.closed for s in sd.streams)


async def test_mic_is_resampled_to_48k_20ms_frames(setup):
    audio, sd, _ = setup
    audio.open(asyncio.get_running_loop())
    try:
        block = (np.sin(np.arange(882) / 44100 * 2 * np.pi * 300) * 10000).astype(np.int16).reshape(-1, 1)
        for _ in range(8):
            audio._record_cb(block, 882, None, None)
        frames = [await asyncio.wait_for(audio.read_frame(), 1) for _ in range(6)]
        assert all(f.dtype == np.int16 and len(f) == FRAME for f in frames)
        assert np.abs(frames[-1]).max() > 5000  # the speech made it through
        assert audio.mic_level > 0.1
    finally:
        audio.close()


async def test_half_duplex_mutes_the_mic_while_the_voice_plays(setup):
    audio, sd, clock = setup
    audio.open(asyncio.get_running_loop())
    try:
        loud_mic = np.full((882, 1), 12000, dtype=np.int16)
        audio.play_frame(voice_frame())
        out = np.zeros((960, 2), dtype=np.float32)
        audio._play_cb(out, 960, None, None)  # the speaker plays the model's voice
        assert audio.speaking and not audio.mic_open
        assert np.abs(out).max() > 0.05 and np.abs(out).max() <= 0.5 * 8000 / 32768 + 0.01  # volume applied
        for _ in range(4):
            audio._record_cb(loud_mic, 882, None, None)
        assert not (await asyncio.wait_for(audio.read_frame(), 1)).any()  # silence goes to OpenAI, not the echo
        clock.t += 0.5  # echo tail over
        assert (await asyncio.wait_for(audio.read_frame(), 1)).any()
    finally:
        audio.close()


async def test_mute_flushes_and_silences(setup):
    audio, sd, _ = setup
    audio.open(asyncio.get_running_loop())
    try:
        for _ in range(10):
            audio.play_frame(voice_frame())
        audio.mute()
        out = np.ones((960, 2), dtype=np.float32)
        audio._play_cb(out, 960, None, None)
        assert not out.any()
        audio.play_frame(voice_frame())  # nothing more is queued after a mute
        assert audio._out_queued == 0
    finally:
        audio.close()


async def test_backlog_is_capped(setup):
    audio, sd, _ = setup
    audio.open(asyncio.get_running_loop())
    try:
        for _ in range(200):  # 4 s arrive at once
            audio.play_frame(voice_frame())
        assert audio._out_queued <= RATE * 1.0 + 960
    finally:
        audio.close()


# ------------------------------------------------------------------ lip-sync for the face
class TimeInfo:
    """PortAudio's callback timing: this block reaches the speaker ``delay`` s after now."""

    def __init__(self, delay: float) -> None:
        self.currentTime = 50.0
        self.outputBufferDacTime = 50.0 + delay


async def test_mouth_follows_what_is_heard_not_what_arrived(setup):
    from littlebot.chatgpt.live_audio import MOUTH_LEAD_S
    from littlebot.face.mouth import VISEMES

    audio, sd, clock = setup
    audio.open(asyncio.get_running_loop())
    try:
        assert audio.mouth == (0.0, "closed")
        audio.play_frame(voice_frame())  # 20 ms of the model's voice
        out = np.zeros((960, 2), dtype=np.float32)
        audio._play_cb(out, 960, TimeInfo(0.2), None)  # the speaker plays this 200 ms from now
        assert audio.mouth == (0.0, "closed")  # not heard yet: the mouth waits for the sound
        clock.t += 0.2 - MOUTH_LEAD_S - 0.01
        assert audio.mouth == (0.0, "closed")
        clock.t += 0.02
        level, shape = audio.mouth  # heard now (a little early, for the face's own delay)
        assert level > 0.5 and shape in VISEMES and shape != "closed"
        audio._play_cb(out, 960, TimeInfo(0.2), None)  # the queue ran dry: silence
        clock.t += 0.3
        assert audio.mouth == (0.0, "closed")
    finally:
        audio.close()


async def test_mouth_without_timing_info_uses_the_stream_latency(setup):
    audio, sd, clock = setup
    audio.open(asyncio.get_running_loop())
    try:
        audio._out_stream.latency = 0.1
        audio.play_frame(voice_frame())
        out = np.zeros((960, 2), dtype=np.float32)
        audio._play_cb(out, 960, None, None)
        assert audio.mouth == (0.0, "closed")
        clock.t += 0.1
        assert audio.mouth[0] > 0.5
        audio._out_stream.latency = 7.0  # nonsense: ignored
        assert audio._output_delay(None) == 0.0
        assert audio._output_delay(TimeInfo(-1.0)) == 0.0
    finally:
        audio.close()


async def test_mute_closes_the_mouth(setup):
    audio, sd, clock = setup
    audio.open(asyncio.get_running_loop())
    try:
        audio.play_frame(voice_frame())
        audio._play_cb(np.zeros((960, 2), dtype=np.float32), 960, None, None)
        assert audio.mouth[0] > 0.5
        audio.mute()
        assert audio.mouth == (0.0, "closed")
    finally:
        audio.close()
    assert audio.mouth == (0.0, "closed")


async def test_lip_sync_trouble_never_breaks_the_voice(setup):
    audio, sd, clock = setup
    audio.open(asyncio.get_running_loop())
    try:
        class Broken:
            sample_rate = audio.out_rate

            def process(self, block):
                raise RuntimeError("boom")

        audio._visemes = Broken()
        audio.play_frame(voice_frame())
        out = np.zeros((960, 2), dtype=np.float32)
        audio._play_cb(out, 960, None, None)
        assert np.abs(out).max() > 0.05  # the voice still played
        assert audio.speaking
    finally:
        audio.close()
