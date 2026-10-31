"""Barge-in: a grown-up talking over LittleBot's voice (with PipeWire's echo cancelling) stops it.

The detector on synthetic signals (a grown-up over the voice's echo, a baby, a cough, a short "mm", the echo
alone), then the whole flow through LiveAudio with a fake PortAudio driven 20 ms at a time on a fake clock:
the voice fades out and waits, their words go to ChatGPT from just before they started, and then either
ChatGPT hears them (the rest of that turn is dropped) or nothing comes (a false alarm: the voice plays on).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import numpy as np
import pytest

av = pytest.importorskip("av")

from littlebot.audio import devices as dev  # noqa: E402
from littlebot.chatgpt.live_audio import (  # noqa: E402
    FRAME,
    RATE,
    BargeInDetector,
    BargeInSettings,
    LiveAudio,
    estimate_pitch,
    is_go_ahead,
)

FRAME_S = FRAME / RATE


def voiced(f0: float, secs: float, level: float, *, seed: int = 1, vibrato: float = 0.03, syll: float = 4.0) -> np.ndarray:
    """A voiced, speech-like sound: harmonics of ``f0`` with a little vibrato and syllables, at ``level`` RMS."""
    rng = np.random.default_rng(seed)
    n = int(secs * RATE)
    t = np.arange(n) / RATE
    f = f0 * (1 + vibrato * np.sin(2 * np.pi * 1.3 * t))
    ph = 2 * np.pi * np.cumsum(f) / RATE
    x = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
    x = x * (0.55 + 0.45 * np.sin(2 * np.pi * syll * t) ** 2) + 0.05 * rng.standard_normal(n)
    return (x / np.sqrt(np.mean(x * x)) * level).astype(np.float32)


def frames_of(x: np.ndarray) -> list[np.ndarray]:
    return [x[i * FRAME:(i + 1) * FRAME] for i in range(len(x) // FRAME)]


# --------------------------------------------------------------------------- the detector alone
VOICE = voiced(210, 4.0, 0.12)  # LittleBot's voice as the speaker plays it
SPEAKER_LEVEL = float(np.sqrt(np.mean(VOICE ** 2))) * 1.4  # the loudest it played lately (syllables)
ECHO = 0.05 * np.roll(VOICE, 3 * FRAME)  # what echo cancelling leaves of it at the microphone (-26 dB), a bit late
NOISE = (0.002 * np.random.default_rng(7).standard_normal(len(VOICE))).astype(np.float32)


def at(sound: np.ndarray, start_s: float = 2.0) -> np.ndarray:
    out = np.zeros_like(VOICE)
    i = int(start_s * RATE)
    out[i:i + len(sound)] = sound[:len(out) - i]
    return out


def first_fire(mic: np.ndarray, settings: BargeInSettings | None = None, prepare: Any = None) -> float | None:
    """Seconds into ``mic`` at which the detector decides someone talks over the voice, else None."""
    det = BargeInDetector(settings or BargeInSettings())
    t0 = t = 100.0
    for f in frames_of(mic):
        if prepare is not None:
            prepare(det, t - t0)
        if det.step(f, SPEAKER_LEVEL, t, voice_on=True):
            return t - t0
        t += FRAME_S
    return None


def test_a_grown_up_talking_over_the_echo_interrupts_after_about_300_ms():
    fired = first_fire(ECHO + NOISE + at(voiced(120, 1.0, 0.05)))
    assert fired is not None and 2.2 <= fired <= 2.35  # they started at 2.0 s: 0.3 s of speech (live_barge_in_speech_ms)
    # a woman's voice too, and a deep one
    assert first_fire(ECHO + NOISE + at(voiced(230, 1.0, 0.05, seed=3))) is not None
    assert first_fire(ECHO + NOISE + at(voiced(90, 1.0, 0.05, seed=4))) is not None


def patchy(f0: float, secs: float, level: float, voiced_share: float, *, seed: int = 11) -> np.ndarray:
    """Speech as the echo-cancelled microphone hears it: only ``voiced_share`` of each 100 ms clearly pitched, the
    rest breathy and noisy (consonants, the canceller working during double talk) at the same loudness."""
    pitched = voiced(f0, secs, level, seed=seed)
    noisy = (np.random.default_rng(seed).standard_normal(len(pitched)) * level).astype(np.float32)
    block = int(0.1 * RATE)
    on = (np.arange(len(pitched)) % block) < int(voiced_share * block)
    return np.where(on, pitched, noisy).astype(np.float32)


def test_real_speech_with_few_clearly_pitched_frames_still_interrupts():
    # on the robot a parent talking over the voice was only 30-56 % clearly pitched and didn't get through
    # (here 15 % of each 100 ms is pitched; the detector's 40 ms pitch window stretches that to about a third)
    assert first_fire(ECHO + NOISE + at(patchy(165, 1.3, 0.05, 0.15))) is not None
    assert first_fire(ECHO + NOISE + at(patchy(450, 1.3, 0.08, 0.15))) is None  # the baby, just as patchy: no
    assert first_fire(ECHO + NOISE + at(patchy(165, 1.3, 0.05, 0.05))) is None  # hardly pitched at all: noise


@pytest.mark.parametrize(("label", "sound"), [
    ("the baby's babble (450 Hz)", voiced(450, 1.2, 0.08, vibrato=0.1, syll=3)),
    ("a high squeal (330 Hz)", voiced(330, 1.2, 0.06, seed=5)),
    ("a cough", (np.random.default_rng(2).standard_normal(int(0.25 * RATE))
                 * np.exp(-np.arange(int(0.25 * RATE)) / (0.06 * RATE)) * 0.3).astype(np.float32)),
    ('a short "mm"', voiced(130, 0.25, 0.05, syll=0.5)),
    ("a quiet TV across the room", voiced(140, 1.5, 0.004, seed=6)),
])
def test_what_must_not_interrupt(label, sound):
    assert first_fire(ECHO + NOISE + at(sound)) is None, label


def test_the_voice_s_own_echo_never_interrupts():
    assert first_fire(ECHO + NOISE) is None
    assert first_fire(0.6 * np.roll(VOICE, 3 * FRAME) + NOISE) is None  # poor echo cancelling: much louder echo
    long_voice = voiced(210, 20.0, 0.12, seed=9)
    gains = 10 ** (np.random.default_rng(3).normal(0, 6, len(long_voice) // FRAME) / 20)  # +-6 dB frame to frame
    echo = (0.05 * long_voice[:len(gains) * FRAME].reshape(len(gains), FRAME) * gains[:, None]).reshape(-1)
    det = BargeInDetector()
    t = 100.0
    for f in frames_of(echo.astype(np.float32)):
        assert not det.step(f, float(np.sqrt(np.mean(long_voice ** 2))) * 1.4, t, voice_on=True)
        t += FRAME_S
    assert 0.01 < det.coupling < 0.1  # it measured what is left of the voice (0.05 of it)


def test_nothing_interrupts_before_the_echo_is_measured():
    adult_at_once = voiced(120, 1.0, 0.05)
    mic = ECHO + NOISE
    mic[:len(adult_at_once)] += adult_at_once
    fired = first_fire(mic)
    assert fired is None or fired >= 0.3  # the first WARMUP_S of the call only measures


def test_voice_recognition_makes_a_grown_up_quicker_and_a_child_never():
    adult = ECHO + NOISE + at(voiced(120, 1.0, 0.05))
    alone = first_fire(adult)

    def known(grown_up: bool):
        def prepare(det: BargeInDetector, _t: float) -> None:
            if det.run is not None:
                det.run.grown_up, det.run.vetoed = grown_up, not grown_up
        return prepare

    quick = first_fire(adult, prepare=known(True))
    assert alone is not None and quick is not None and quick <= alone - 0.1 + 1e-6  # FAST_SPEECH_S, not 0.3 s
    assert first_fire(adult, prepare=known(False)) is None  # a child with a low voice: still no


def test_a_grown_up_who_didnt_get_through_is_logged_with_why(caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    assert first_fire(ECHO + NOISE + at(voiced(130, 0.2, 0.05))) is None  # "mm": too short
    assert "someone talked over the voice for 0.2" in caplog.text and "too short" in caplog.text
    caplog.clear()
    assert first_fire(ECHO + NOISE + at(voiced(450, 1.2, 0.08, vibrato=0.1, syll=3))) is None  # the baby
    assert "talked over the voice" not in caplog.text


def test_settings_change_what_counts():
    adult = ECHO + NOISE + at(voiced(120, 1.0, 0.05))
    slow = first_fire(adult, BargeInSettings(speech_s=0.8))
    assert slow is not None and slow >= 2.75
    assert first_fire(adult, BargeInSettings(over_echo_db=40.0)) is None  # must be far louder than the echo
    assert first_fire(adult, BargeInSettings(max_pitch_hz=110.0)) is None  # 120 Hz counted as a child


def test_settings_from_config():
    from littlebot.core.config import ChatGPTConfig

    s = BargeInSettings.from_config(ChatGPTConfig(live_barge_in="on", live_barge_in_speech_ms=300,
                                                  live_barge_in_false_alarm_ms=900))
    assert (s.mode, s.speech_s, s.false_alarm_s, s.max_pitch_hz) == ("on", 0.3, 0.9, 260.0)
    assert BargeInSettings.from_config(object()) == BargeInSettings()


@pytest.mark.parametrize("f0", [85, 120, 200, 250, 300, 450, 600])
def test_pitch_estimate(f0):
    low = voiced(f0, 0.04, 0.1).reshape(-1, 6).mean(axis=1)  # as the detector sees it: 8 kHz, 40 ms
    pitch, voicing = estimate_pitch(low, 8000)
    assert abs(pitch - f0) / f0 < 0.03 and voicing > 0.8  # no octave errors: a baby is not a grown-up


def test_pitch_of_noise_and_silence():
    noise = np.random.default_rng(1).standard_normal(320).astype(np.float32)
    assert estimate_pitch(noise, 8000)[1] < 0.5
    assert estimate_pitch(np.zeros(320, np.float32), 8000) == (0.0, 0.0)


@pytest.mark.parametrize("text", ["Oops, go ahead.", "sorry, carry on", "Never mind, keep going!", "oh sorry you go ahead",
                                  "Continue.", "I wasn't talking to you", "my bad, please continue", "Go on then.",
                                  "Sorry to interrupt.", "It’s okay, go ahead", "talking to the baby"])
def test_go_ahead_phrases(text):
    assert is_go_ahead(text)


@pytest.mark.parametrize("text", ["", "Wait, what's a giraffe?", "go ahead and tell me about cows",
                                  "stop", "oops", "no, I want the other story", "keep going with the dinosaur one please"])
def test_real_turns_are_not_go_ahead(text):
    assert not is_go_ahead(text)


# --------------------------------------------------------------------------- the whole flow through LiveAudio
DEVICES = [
    {"index": 0, "name": "USB PnP Sound Device", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 44100.0},
    {"index": 1, "name": "UACDemoV1.0", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0},
    {"index": 2, "name": "littlebot_ec_mic", "hostapi": 0, "max_input_channels": 64, "max_output_channels": 64,
     "default_samplerate": 48000.0},
    {"index": 3, "name": "littlebot_ec_speaker", "hostapi": 0, "max_input_channels": 64, "max_output_channels": 64,
     "default_samplerate": 48000.0},
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
    """PortAudio reaching PipeWire: any rate, mono."""

    def __init__(self) -> None:
        self.streams: list[FakeStream] = []

    def check_input_settings(self, **kw: Any) -> None:
        if kw["samplerate"] != 48000:
            raise ValueError("rate")

    def check_output_settings(self, **kw: Any) -> None:
        if kw["samplerate"] != 48000:
            raise ValueError("rate")

    def InputStream(self, **kw: Any) -> FakeStream:  # noqa: N802
        self.streams.append(FakeStream(**kw))
        return self.streams[-1]

    OutputStream = InputStream


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@dataclasses.dataclass
class Match:  # what ctx.voices.identify returns (littlebot/voices)
    person_id: str
    name: str
    role: str
    score: float
    is_child: bool


SPEECH_START = 50  # frames: the voice plays alone for 1 s first (the echo is measured)


class Call:
    """Drives LiveAudio like PortAudio and the call do, one 20 ms frame at a time."""

    def __init__(self, audio: LiveAudio, clock: Clock) -> None:
        self.audio, self.clock = audio, clock
        voice = voiced(210, 30.0, 0.25, seed=11)
        self.voice = [np.clip(f * 32768, -32768, 32767).astype(np.int16) for f in frames_of(voice)]
        self.vi = 0  # the next frame of the voice from the network
        self.mics: list[np.ndarray] = []  # what the microphone heard
        self.sent: list[np.ndarray] = []  # what went to ChatGPT
        self.played: list[np.ndarray] = []  # what the speaker played
        self.events: list[str] = []
        self.noise = np.random.default_rng(5)
        audio.setup_barge_in(on_change=lambda what, info: self.events.append(what))

    async def tick(self, *, voice: bool = True, user: np.ndarray | None = None) -> np.ndarray:
        if voice:
            f = av.AudioFrame.from_ndarray(self.voice[self.vi].reshape(1, -1), format="s16", layout="mono")
            f.sample_rate = RATE
            self.audio.play_frame(f)
            self.vi += 1
        out = np.zeros((FRAME, 1), dtype=np.float32)
        self.audio._play_cb(out, FRAME, None, None)
        self.played.append(out[:, 0].copy())
        mic = 0.1 * out[:, 0] + 0.002 * self.noise.standard_normal(FRAME)  # echo cancelling left 10 % of it
        if user is not None:
            mic = mic + user
        block = np.clip(mic * 32768, -32768, 32767).astype(np.int16)
        self.mics.append(block)
        self.audio._record_cb(block.reshape(-1, 1), FRAME, None, None)
        sent = await asyncio.wait_for(self.audio.read_frame(), 1)
        self.sent.append(sent)
        await asyncio.sleep(0)  # voice recognition (a task) gets its turn
        self.clock.t += FRAME_S
        return sent

    async def warm_up(self) -> None:
        for _ in range(SPEECH_START):
            assert not (await self.tick()).any()  # the gate is shut while the voice plays
        assert self.audio.speaking and not self.audio.mic_open and not self.audio.interrupted

    async def talk_over(self, secs: float, f0: float = 120.0, *, until_interrupted: bool = False, **kw: Any) -> int | None:
        """They talk; returns the frame (counted from when they started) at which the voice stopped for them."""
        cut = None
        for i, u in enumerate(frames_of(voiced(f0, secs, 0.08, seed=13))):
            await self.tick(user=u, **kw)
            if cut is None and self.audio.interrupted:
                cut = i
                if until_interrupted:
                    break
        return cut


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setattr(dev, "query_devices", lambda: [dict(d) for d in DEVICES])
    monkeypatch.setattr(dev, "_ec_mode", "auto")
    clock = Clock()

    def make(**kw: Any) -> tuple[LiveAudio, Call]:
        audio = LiveAudio(mic=None, speaker=None, volume=lambda: 0.5, sd=FakeSD(), now=clock,
                          barge_in=BargeInSettings(**kw))
        audio.open(asyncio.get_running_loop())
        return audio, Call(audio, clock)

    return make


async def test_cutting_in_stops_the_voice_and_sends_them_from_their_first_word(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    try:
        assert audio.echo_cancelled and audio.barge_in_on and audio.device_names[0] == "littlebot_ec_mic"
        u = voiced(120, 0.02, 0.08)
        assert np.array_equal(await c.tick(voice=False, user=u), c.mics[-1])  # nothing playing: the microphone is open
        await c.warm_up()
        cut = await c.talk_over(0.6)
        assert cut is not None and 13 <= cut <= 19  # about 0.3 s of their speech
        assert c.events == ["interrupted"] and audio.barge_ins == 1
        assert not audio.speaking and audio.mic_open and audio.interrupted
        assert "live voice: interrupted by a grown-up after 0.3 s of speech (pitch 12" in caplog.text
        # the voice faded out in FADE_S (the next ~3 blocks) and is silent since
        k = SPEECH_START + 1 + cut  # the frame read when it decided
        source = c.voice[k].astype(np.float32) / 32768 * 0.5  # what it would have played (tick 0 had no voice)
        ramp = np.clip(1 - np.arange(1, FRAME + 1) / (0.05 * RATE), 0, 1)
        assert np.abs(c.played[k]).max() > 0.05 and np.allclose(c.played[k + 1], source * ramp, atol=1e-4)
        assert all(not p.any() for p in c.played[k + 4:])
        # ChatGPT gets them from just before they started (the pre-roll), every frame in order, none twice
        first = next(i for i, m in enumerate(c.mics) if np.array_equal(m, c.sent[k]))
        start = SPEECH_START + 1
        assert start - 7 <= first <= start
        n = len(c.sent) - k
        assert all(np.array_equal(c.sent[k + j], c.mics[first + j]) for j in range(n))
        # the voice that kept coming is held, not played
        assert audio._out_queued == (len(c.played) - (k + 4)) * FRAME
    finally:
        audio.close()


async def test_their_words_drop_the_rest_of_that_turn_and_the_next_one_plays(call):
    audio, c = call()
    try:
        await c.warm_up()
        await c.talk_over(1.0)
        assert audio.barge_in_heard("Wait, what is a", False) is None  # ChatGPT heard them
        assert c.events == ["interrupted", "heard"] and audio._out_queued == 0
        for u in frames_of(voiced(120, 0.5, 0.08, seed=2)):  # still talking; the turn they cut into still comes
            await c.tick(user=u)
        assert audio.interrupted and not any(p.any() for p in c.played[-25:])
        assert audio.barge_in_heard("Wait, what is a giraffe?", True) is None  # a real turn: no note
        for _ in range(10):
            await c.tick()  # the end of that turn: still dropped
        assert audio.interrupted and not any(p.any() for p in c.played[-10:])
        for _ in range(20):
            await c.tick(voice=False)  # a pause: ChatGPT is thinking
        for _ in range(10):
            await c.tick()  # its answer
        assert not audio.interrupted and audio.speaking and not audio.mic_open
        assert all(p.any() for p in c.played[-6:])  # fades back in and plays
        assert not any(s.any() for s in c.sent[-8:])  # the gate is shut again while it talks
        assert audio.false_alarms == 0
    finally:
        audio.close()


async def test_oops_go_ahead_asks_the_voice_to_carry_on(call):
    audio, c = call()
    try:
        assert audio.barge_in_heard("oops, go ahead", True) is None  # nobody cut in
        await c.warm_up()
        await c.talk_over(0.6)
        assert audio.barge_in_heard("oops", False) is None
        assert audio.barge_in_heard("Oops, go ahead.", True) == "go ahead"
        assert audio.barge_in_heard("Go ahead.", True) is None  # only their first words after cutting in
    finally:
        audio.close()


async def test_drop_voice_empties_what_was_held_and_listens(call):
    audio, c = call()
    try:
        await c.warm_up()
        assert await c.talk_over(0.5) is not None and audio.interrupted and audio._out_queued > 0
        audio.drop_voice()  # "stop": the call silences the voice
        assert audio._out_queued == 0 and not audio.interrupted
        for _ in range(40):
            await c.tick(voice=False)
        assert audio.mic_open and not audio.speaking
    finally:
        audio.close()


async def test_a_false_alarm_lets_the_held_voice_play_on(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    try:
        await c.warm_up()
        cut = await c.talk_over(0.8)  # someone talking on for a while after it stopped, but no words came
        assert cut is not None
        k = SPEECH_START + 1 + cut
        while "carried_on" not in c.events:
            await c.tick()  # no words from ChatGPT
            assert len(c.sent) - k < 250, "never carried on"
        held = len(c.sent) - k
        assert 0.9 * 2.5 / FRAME_S <= held <= 2.9 / FRAME_S  # live_barge_in_false_alarm_ms
        assert c.events == ["interrupted", "carried_on"] and audio.false_alarms == 1
        assert "false alarm (no words within 2." in caplog.text and "the voice carried on" in caplog.text
        assert not audio.interrupted and audio.speaking
        for _ in range(8):
            await c.tick()
        assert not any(s.any() for s in c.sent[-8:])  # the gate is shut again
        # it plays on from where it stopped: what it plays now is the voice from about 1.2 s ago
        now = c.played[-1]
        j = next(i for i, v in enumerate(c.voice[:c.vi]) if np.allclose(now, v.astype(np.float32) / 32768 * 0.5, atol=1e-4))
        assert c.vi - j >= 50
        assert audio.barge_in_heard("go ahead", True) is None  # too late for that
    finally:
        audio.close()


async def test_quiet_the_moment_the_voice_stopped_is_its_own_echo_and_it_carries_on_soon(call, caplog):
    """On the robot (2026-10-31) its own voice leaking through held the answer 2.5 s: a person goes on talking once
    the voice stops; an echo (or a bump) ends with it."""
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    try:
        await c.warm_up()
        cut = await c.talk_over(0.5, until_interrupted=True)  # the sound stops as the voice is held
        assert cut is not None
        k = len(c.sent)
        while "carried_on" not in c.events:
            await c.tick()
            assert len(c.sent) - k < 250, "never carried on"
        held = len(c.sent) - k
        assert 0.5 / FRAME_S <= held <= 0.8 / FRAME_S  # ECHO_HOLD_S, not live_barge_in_false_alarm_ms
        assert "its own echo" in caplog.text and audio.false_alarms == 1
        assert not audio.interrupted and audio.speaking
    finally:
        audio.close()


async def test_someone_who_goes_on_talking_is_still_waited_for(call):
    audio, c = call()
    try:
        await c.warm_up()
        cut = await c.talk_over(2.0, until_interrupted=True)
        assert cut is not None
        await c.talk_over(1.0)  # they go on talking, no words yet
        assert audio.interrupted and "carried_on" not in c.events
    finally:
        audio.close()


async def test_voice_recognition_saying_child_lets_the_voice_carry_on(call):
    asked: list[tuple[int, int]] = []

    async def identify(samples: np.ndarray, rate: int) -> Match:
        asked.append((len(samples), rate))
        return Match("p2", "Mia", "child", 0.9, is_child=True)

    audio, c = call()
    audio.setup_barge_in(identify=identify, on_change=lambda what, info: c.events.append(what))
    try:
        await c.warm_up()
        assert await c.talk_over(1.0) is None  # a low-voiced child: no interruption
        assert c.events == [] and audio.barge_ins == 0 and asked and asked[0][1] == 16000
        assert 0.15 * 16000 <= asked[0][0] <= 0.4 * 16000  # asked about the first ~0.2 s of it
    finally:
        audio.close()


async def test_voice_recognition_saying_grown_up_interrupts_sooner(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")

    async def identify(samples: np.ndarray, rate: int) -> Match:
        return Match("p1", "Ali", "parent", 0.8, is_child=False)

    audio, c = call()
    plain_audio, plain = call()
    audio.setup_barge_in(identify=identify, on_change=lambda what, info: c.events.append(what))
    try:
        await c.warm_up()
        quick = await c.talk_over(0.6, until_interrupted=True)
        await plain.warm_up()
        slow = await plain.talk_over(0.6, until_interrupted=True)
        assert quick is not None and slow is not None and quick < slow
        assert "voice Ali)" in caplog.text
    finally:
        audio.close()
        plain_audio.close()


async def test_voice_recognition_trouble_never_matters(call):
    async def identify(samples: np.ndarray, rate: int) -> None:
        raise RuntimeError("no model")

    audio, c = call()
    audio.setup_barge_in(identify=identify, on_change=lambda what, info: c.events.append(what))
    try:
        await c.warm_up()
        assert await c.talk_over(0.6) is not None  # the detector decides alone
    finally:
        audio.close()


async def test_live_barge_in_off_keeps_it_one_at_a_time(call):
    audio, c = call(mode="off")
    try:
        assert audio.echo_cancelled and not audio.barge_in_on
        await c.warm_up()
        assert await c.talk_over(1.0) is None
        assert not any(s.any() for s in c.sent[SPEECH_START + 1:]) and c.events == []
    finally:
        audio.close()


async def test_a_stalled_stream_reopens_with_barge_in_still_on(call):
    audio, c = call()
    try:
        await c.warm_up()
        det = audio._detector
        coupling = det.coupling
        c.clock.t += 2.1
        assert audio.stalled() == "microphone"
        await asyncio.to_thread(audio.reopen)
        assert audio.stalled() is None and audio.barge_in_on and audio._detector is det
        assert det.coupling == coupling  # the echo stays measured
        await c.warm_up()
        assert await c.talk_over(0.6) is not None
    finally:
        audio.close()
    assert not audio.interrupted


# --------------------------------------------------------------------------- stop words (stop_words.py)
class FakeStopWords:
    """What littlebot/chatgpt/stop_words.StopWords does for LiveAudio, with the stop word given by the test."""

    def __init__(self) -> None:
        self.pushed = self.quiets = 0
        self.hit: tuple[str, float] | None = None
        self.closed = False

    def push(self, frame: np.ndarray, at: float | None = None) -> None:
        self.pushed += 1

    def quiet(self) -> None:
        self.quiets += 1

    def take(self) -> tuple[str, float] | None:
        hit, self.hit = self.hit, None
        return hit

    def close(self) -> None:
        self.closed = True


async def short_word(c: Call, words: FakeStopWords | None, said: str = "stop", *, f0: float = 120.0,
                     late: int = 3) -> None:
    """A quick "stop!" over the voice (0.16 s, too short for live_barge_in_speech_ms), recognised ``late`` frames
    after it was said (the time stamp is when its audio was heard)."""
    for u in frames_of(voiced(f0, 0.16, 0.08, seed=13)):
        await c.tick(user=u)
    said_at = c.clock.t
    for i in range(max(10, late + 5)):
        if i == late and words is not None:
            words.hit = (said, said_at)  # the recogniser's words so far have it a moment after it was said
        await c.tick()


async def test_a_quick_stop_word_interrupts_however_short(call, caplog):
    # "it should look for words like stop even if they're shorter" (a parent, 2026-10-29)
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words)
        assert audio.interrupted and c.events == ["interrupted", "stop_word"] and words.pushed > 0
        assert "live voice: interrupted: they said 'stop' (recognised in " in caplog.text
    finally:
        audio.close()
    assert words.closed


async def test_a_stop_word_recognised_late_still_stops_it(call):
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words, late=60)  # the recogniser on the robot lagged over a second behind
        assert c.events == ["interrupted", "stop_word"]
    finally:
        audio.close()


async def test_a_stop_word_in_a_babys_squeal_does_not(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words, f0=480.0)
        assert c.events == [] and not audio.interrupted
        assert "heard 'stop', but it sounded like a child or its own voice (4" in caplog.text
    finally:
        audio.close()


async def test_a_shouted_stop_higher_than_a_grown_ups_pitch_still_stops_it(call, caplog):
    # on the robot (2026-10-29) the parent's "stop!" had no frame at a grown-up's pitch: turned down three times
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words, f0=300.0)
        assert c.events == ["interrupted", "stop_word"]
        assert "live voice: interrupted: they said 'stop' (recognised in " in caplog.text and " pitch 0 Hz" not in \
            caplog.text
    finally:
        audio.close()


async def test_a_shh_is_never_judged_by_its_pitch(call, caplog):
    # on the robot (2026-10-31) a parent's "shh" measured 579 Hz (a hiss has no real pitch) and was turned down
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words, "shh", f0=480.0)
        assert c.events == ["interrupted", "stop_word"]
        assert "they said 'shh'" in caplog.text
    finally:
        audio.close()


async def test_its_own_hang_on_heard_back_higher_than_a_grown_up_does_not(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        await short_word(c, words, "hang on", f0=300.0)
        assert c.events == [] and "heard 'hang on', but it sounded like a child or its own voice" in caplog.text
    finally:
        audio.close()


async def test_a_stop_word_the_voice_just_said_itself_does_not(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words,
                         voice_said=lambda: "Okay. Let's stop there for today, and")
    try:
        await c.warm_up()
        await short_word(c, words)
        assert c.events == [] and "heard 'stop', but the voice itself just said it" in caplog.text
    finally:
        audio.close()


async def test_the_same_short_sound_without_a_stop_word_does_not_interrupt(call):
    audio, c = call()
    try:
        await c.warm_up()
        await short_word(c, None)
        assert not audio.interrupted and c.events == []
    finally:
        audio.close()


async def test_the_voice_saying_wait_itself_does_not_stop_it(call, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    audio, c = call()
    words = FakeStopWords()
    audio.setup_barge_in(on_change=lambda what, info: c.events.append(what), stop_words=words)
    try:
        await c.warm_up()
        words.hit = ("wait", c.clock.t)  # heard in what is left of its own echo: nothing louder than the echo
        for _ in range(5):
            await c.tick()
        assert not audio.interrupted and c.events == []
        assert "heard 'wait' while the voice talked (" in caplog.text
        assert "but nothing louder than its own echo then: it carried on" in caplog.text
        for _ in range(40):
            await c.tick(voice=False)  # the voice stopped: what was being recognised is forgotten
        assert words.quiets > 0
    finally:
        audio.close()


class FakeRecognizer:
    def __init__(self, partials: list[str]) -> None:
        self.partials = list(partials)
        self.accepted = self.resets = 0

    def accept(self, pcm16: bytes) -> dict[str, Any] | None:
        self.accepted += 1
        return None

    def partial(self) -> str:
        return self.partials.pop(0) if self.partials else ""

    def reset(self) -> None:
        self.resets += 1


async def test_stop_words_finds_the_phrase_in_the_words_so_far():
    import time

    from littlebot.chatgpt.stop_words import StopWords, match

    rec = FakeRecognizer(["", "the bus", "okay stop"])
    phrases = [("stop", ("stop",)), ("okay stop", ("okay", "stop")), ("hold on", ("hold", "on"))]
    words = StopWords(lambda: (rec, phrases), rate=RATE)
    try:
        for _ in range(15):  # three batches of 0.1 s
            words.push(np.zeros(FRAME, dtype=np.int16))
        deadline = time.monotonic() + 3
        while words.heard == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        hit = words.take()
        assert hit is not None and hit[0] == "okay stop" and words.take() is None and rec.resets == 1
    finally:
        words.close()
    assert match("so hold on a second".split(), phrases) == "hold on" and match(["holding"], phrases) is None


class SureRecognizer(FakeRecognizer):
    """Also forces its result out (VoskKeywordRecognizer.finish), with word confidences."""

    def __init__(self, partials: list[str], results: list[dict[str, Any]]) -> None:
        super().__init__(partials)
        self.results = list(results)

    def finish(self) -> dict[str, Any] | None:
        return self.results.pop(0) if self.results else None


def _result(*words: tuple[str, float]) -> dict[str, Any]:
    return {"text": " ".join(w for w, _ in words), "result": [{"word": w, "conf": c} for w, c in words]}


async def test_a_stop_word_counts_only_when_sure_and_not_inside_a_sentence():
    import time

    from littlebot.chatgpt.stop_words import StopWords, sure

    phrases = [("stop", ("stop",)), ("that's enough", ("that's", "enough"))]
    # heard on the robot (2026-10-31) while it answered: "what's the capital of Australia" as "that's enough"
    assert sure(_result(("what", 1.0), ("the", 0.6), ("that's", 0.3), ("enough", 0.4)), phrases) is None
    assert sure(_result(("tell", 1.0), ("me", 1.0), ("a", 1.0), ("lot", 1.0), ("stop", 0.92)), phrases) is None
    assert sure(_result(("what", 1.0), ("stop", 1.0)), phrases) is None
    assert sure(_result(("okay", 1.0), ("stop", 1.0)), phrases) == "stop"
    assert sure(_result(("hey", 1.0), ("little", 1.0), ("bot", 1.0), ("that's", 0.9), ("enough", 1.0)),
                phrases) == "that's enough"
    rec = SureRecognizer(["", "that's enough", "", "stop"],
                         [_result(("what", 1.0), ("that's", 0.3), ("enough", 0.4)), _result(("stop", 1.0))])
    words = StopWords(lambda: (rec, phrases), rate=RATE)
    try:
        for _ in range(25):  # five batches of 0.1 s
            words.push(np.zeros(FRAME, dtype=np.int16))
        deadline = time.monotonic() + 3
        while words.heard == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        hit = words.take()
        assert hit is not None and hit[0] == "stop" and words.heard == 1 and not rec.results
    finally:
        words.close()


def test_no_stop_words_without_the_speech_model(tmp_path):
    from littlebot.chatgpt.stop_words import StopWords

    assert StopWords.from_model(tmp_path / "no-model") is None
