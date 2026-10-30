"""LittleBot's own end-of-turn (littlebot/assistant/stt/endpoint.py), with synthetic audio: tone bursts for speech,
near-silence for pauses. Timing comes from the samples fed, so nothing here sleeps."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from littlebot.assistant.speech import SpeechError, STTEvent, STTStream
from littlebot.assistant.stt.endpoint import EndpointedSTT, Endpointer, pause_needed_ms

RATE = 16000


def tone(ms: float, amp: float = 0.1, hz: float = 220.0) -> np.ndarray:
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return (amp * np.sqrt(2) * np.sin(2 * np.pi * hz * t)).astype(np.float32)  # RMS = amp


def quiet(ms: float, amp: float = 0.001) -> np.ndarray:
    rng = np.random.default_rng(int(ms))
    return (amp * rng.standard_normal(int(RATE * ms / 1000))).astype(np.float32)


class FakeBatch:
    """A batch provider: says ``reply`` for any turn, keeps how long each turn's audio was."""

    name = "fake"

    def __init__(self, reply: str = "hello there", *, fail: str = "", gate: asyncio.Event | None = None) -> None:
        self.reply = reply
        self.fail = fail
        self.gate = gate
        self.turns: list[int] = []

    def missing(self) -> str | None:
        return None

    async def transcribe(self, samples: np.ndarray) -> str:
        self.turns.append(len(samples))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise SpeechError(self.fail)
        return self.reply


class FakeDecoder:
    def __init__(self, partials: list[str], final: str) -> None:
        self.partials = list(partials)
        self.final_text = final
        self.fed = 0
        self.closed = False

    async def accept(self, samples: np.ndarray) -> str:
        self.fed += len(samples)
        return self.partials.pop(0) if len(self.partials) > 1 else (self.partials[0] if self.partials else "")

    async def final(self) -> str:
        return self.final_text

    async def aclose(self) -> None:
        self.closed = True


class FakeFollowing(FakeBatch):
    """A provider that follows the words while they are said (like Vosk)."""

    def __init__(self, partials: list[str], final: str) -> None:
        super().__init__(final)
        self.partials, self.final = partials, final
        self.decoders: list[FakeDecoder] = []

    def decoder(self) -> FakeDecoder:
        self.decoders.append(FakeDecoder(self.partials, self.final))
        return self.decoders[-1]


class Events(list):
    def __call__(self, event: STTEvent) -> None:
        self.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self]


async def feed_frames(ep: Endpointer, audio: np.ndarray, frame_ms: int = 20) -> None:
    n = RATE * frame_ms // 1000
    for i in range(0, len(audio), n):
        await ep.feed(audio[i:i + n])


async def pause_until_end(ep: Endpointer, events: Events, limit_ms: int = 4000) -> int:
    """Feed quiet 20 ms at a time until end_of_turn; how many ms of quiet that took."""
    for ms in range(20, limit_ms + 20, 20):
        await ep.feed(quiet(20))
        await ep.idle()
        if "end_of_turn" in events.kinds():
            return ms
    return -1


# ---------------------------------------------------------------- the pause length
def test_pause_needed_ms():
    assert pause_needed_ms("", 650) == 650  # no words to go by
    assert pause_needed_ms("what's the weather today", 650) == pytest.approx(455)  # a whole sentence
    assert pause_needed_ms("Is it raining?", 650) == pytest.approx(455)
    assert pause_needed_ms("set a timer for", 650) == pytest.approx(1170)  # more is coming
    assert pause_needed_ms("tell me about the moon and", 650) == pytest.approx(1170)
    assert pause_needed_ms("um", 650) == pytest.approx(1170)
    assert pause_needed_ms("I was thinking, um", 650) == pytest.approx(1170)
    assert pause_needed_ms("can you", 650) == 650  # too short to tell
    assert pause_needed_ms("so what do you", 650) == 650  # "you": not the end, not clearly more
    assert pause_needed_ms("and", 1500) == 2000  # 1.8x, capped at 2 s
    assert pause_needed_ms("and", 2500) == 2500  # never shorter than the plain pause


# ---------------------------------------------------------------- turns
async def test_turn_ends_after_end_of_turn_ms_of_quiet():
    events, provider = Events(), FakeBatch("what's the weather")
    ep = Endpointer(provider, events, end_of_turn_ms=650)
    await feed_frames(ep, quiet(400))
    assert events == [] and ep.state == "waiting"
    await feed_frames(ep, tone(900))
    assert events.kinds() == ["start_of_turn"]
    assert await pause_until_end(ep, events) == 660  # the first 20 ms frame at or past 650 ms
    assert events[-1] == STTEvent("end_of_turn", text="what's the weather")
    assert ep.state == "waiting"
    # the turn's audio: <= 300 ms from before the start, the speech, 300 ms of the pause
    assert RATE * 1.15 <= provider.turns[0] <= RATE * 1.55


async def test_short_pauses_inside_a_turn_do_not_end_it():
    events, provider = Events(), FakeBatch()
    ep = Endpointer(provider, events, end_of_turn_ms=650)
    for _ in range(3):
        await feed_frames(ep, tone(400))
        await feed_frames(ep, quiet(500))  # a breath between phrases
    await ep.idle()
    assert events.kinds() == ["start_of_turn"] and provider.turns == []
    assert await pause_until_end(ep, events) > 0
    assert events.kinds() == ["start_of_turn", "end_of_turn"]


async def test_feeding_odd_sizes_gives_the_same_timing():
    events = Events()
    ep = Endpointer(FakeBatch(), events, end_of_turn_ms=650)
    audio = np.concatenate([quiet(300), tone(800), quiet(640)])
    sizes, i = (37, 501, 160, 999, 1), 0
    while i < len(audio):
        n = sizes[i % len(sizes)]
        await ep.feed(audio[i:i + n])
        i += n
    await ep.idle()
    assert events.kinds() == ["start_of_turn"]  # 640 ms < 650 ms
    await feed_frames(ep, quiet(40))
    await ep.idle()
    assert events.kinds() == ["start_of_turn", "end_of_turn"]


async def test_a_whole_sentence_ends_sooner_and_a_trailing_and_later():
    for partial, expected in (("what time is it in tokyo", 460), ("put milk and eggs and", 1180)):
        events = Events()
        provider = FakeFollowing([partial], final=partial)
        ep = Endpointer(provider, events, end_of_turn_ms=650)
        await feed_frames(ep, tone(1000))
        assert events.kinds() == ["start_of_turn", "partial"] and events[1].text == partial
        assert await pause_until_end(ep, events) == expected
        assert events[-1].text == partial
        assert provider.turns == []  # the decoder had the words already: no second transcription
        assert provider.decoders[0].closed


async def test_partials_are_sent_as_they_change():
    events = Events()
    provider = FakeFollowing(["what", "what is", "what is", "what is the time"], final="what is the time")
    ep = Endpointer(provider, events)
    await feed_frames(ep, tone(1200))
    assert [e.text for e in events if e.kind == "partial"] == ["what", "what is", "what is the time"]
    await pause_until_end(ep, events)
    assert events[-1] == STTEvent("end_of_turn", text="what is the time")


async def test_a_click_is_not_a_turn():
    events, provider = Events(), FakeBatch()
    ep = Endpointer(provider, events)
    await feed_frames(ep, np.concatenate([quiet(300), tone(40, amp=0.3), quiet(1500)]))
    await ep.idle()
    assert events == [] and provider.turns == []
    await feed_frames(ep, np.concatenate([tone(120, amp=0.3), quiet(1500)]))  # a bang: starts, too little voice
    await ep.idle()
    assert events.kinds() == ["start_of_turn"] and provider.turns == []


async def test_no_words_means_no_end_of_turn():
    events, provider = Events(), FakeBatch("")
    ep = Endpointer(provider, events)
    await feed_frames(ep, np.concatenate([tone(600), quiet(1000)]))
    await ep.idle()
    assert events.kinds() == ["start_of_turn"] and len(provider.turns) == 1
    provider.reply = "second try"
    await feed_frames(ep, np.concatenate([tone(600), quiet(1000)]))
    await ep.idle()
    assert events.kinds() == ["start_of_turn", "start_of_turn", "end_of_turn"]


async def test_max_turn_seconds_ends_a_long_turn():
    events, provider = Events(), FakeBatch("a long story")
    ep = Endpointer(provider, events, max_turn_seconds=3.0)
    await feed_frames(ep, tone(2800))
    await ep.idle()
    assert events.kinds() == ["start_of_turn"]
    await feed_frames(ep, tone(400))
    await ep.idle()
    assert [e.text for e in events if e.kind == "end_of_turn"] == ["a long story"]
    assert provider.turns[0] == pytest.approx(3.0 * RATE, abs=RATE * 0.03)
    assert events.kinds().count("start_of_turn") == 2  # still talking: what follows is the next turn


async def test_finish_ends_the_turn_now():
    events = Events()
    ep = Endpointer(FakeBatch("stop there"), events)
    await feed_frames(ep, tone(700))
    await ep.finish()
    assert events.kinds() == ["start_of_turn", "end_of_turn"]
    await ep.finish()  # nothing more to end
    assert len(events) == 2


async def test_reset_drops_a_turn_being_transcribed():
    gate = asyncio.Event()
    events, provider = Events(), FakeBatch("old words", gate=gate)
    ep = Endpointer(provider, events)
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    assert ep._task is not None  # noqa: SLF001 - being transcribed
    await ep.reset()
    gate.set()
    await ep.idle()
    await asyncio.sleep(0)
    assert events.kinds() == ["start_of_turn"]
    provider.reply = "new words"
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await ep.idle()
    assert events[-1] == STTEvent("end_of_turn", text="new words")


async def test_reset_mid_turn_starts_over():
    events, provider = Events(), FakeBatch()
    ep = Endpointer(provider, events)
    await feed_frames(ep, tone(500))
    await ep.reset()
    assert ep.state == "waiting"
    await feed_frames(ep, quiet(1000))
    await ep.idle()
    assert provider.turns == [] and events.kinds() == ["start_of_turn"]


async def test_a_failed_transcription_is_an_error_event():
    events = Events()
    ep = Endpointer(FakeBatch(fail="network"), events)
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await ep.idle()
    assert events[-1] == STTEvent("error", detail="network")


async def test_noise_floor_raises_the_threshold_in_a_noisy_room():
    events = Events()
    ep = Endpointer(FakeBatch(), events, threshold=0.015)
    await feed_frames(ep, quiet(2000, amp=0.012))  # a fan: below the threshold, but not silence
    assert ep.noise_floor == pytest.approx(0.012, rel=0.15)
    assert ep.start_threshold == pytest.approx(0.036, rel=0.15)
    await feed_frames(ep, tone(500, amp=0.025))  # as loud as the fan's 2x: not speech here
    assert events == []
    await feed_frames(ep, tone(500, amp=0.1))
    assert events.kinds() == ["start_of_turn"]


async def test_turns_go_out_in_order_and_audio_is_dropped():
    first_done = asyncio.Event()

    class Ordered(FakeBatch):
        async def transcribe(self, samples: np.ndarray) -> str:
            self.turns.append(len(samples))
            if len(self.turns) == 1:
                await first_done.wait()  # the first turn's words are slow to come back
            return f"turn {len(self.turns)}"

    events, provider = Events(), Ordered()
    ep = Endpointer(provider, events)
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))  # listening went on meanwhile
    for _ in range(5):
        await asyncio.sleep(0)
    assert events.kinds() == ["start_of_turn", "start_of_turn"]  # the second waits for the first
    first_done.set()
    await ep.idle()
    assert [e.text for e in events if e.kind == "end_of_turn"] == ["turn 1", "turn 2"]
    assert ep._turn == [] and ep._pending == []  # noqa: SLF001 - nothing of the turns is kept


async def test_closed_endpointer_ignores_audio():
    events = Events()
    ep = Endpointer(FakeBatch(), events)
    await ep.aclose()
    await feed_frames(ep, tone(800))
    assert events == []


async def test_async_handlers_and_a_failing_handler():
    got = []

    async def handler(event: STTEvent) -> None:
        got.append(event.kind)
        if event.kind == "start_of_turn":
            raise RuntimeError("the session's bug")

    ep = Endpointer(FakeBatch("still works"), handler)
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await ep.idle()
    assert got == ["start_of_turn", "end_of_turn"]


async def test_endpointed_provider_opens_streams():
    class Warmable(FakeBatch):
        warmed = 0

        async def warm(self) -> None:
            self.warmed += 1

    inner = Warmable()
    provider = EndpointedSTT(inner, end_of_turn_ms=400, max_turn_seconds=5, threshold=0.02)
    assert provider.name == "fake" and provider.streaming is False and provider.missing() is None
    stream = await provider.open(Events())
    assert isinstance(stream, STTStream) and inner.warmed == 1
    assert stream.end_of_turn_ms == 400 and stream.threshold == 0.02


async def test_a_handler_may_reset_from_inside_end_of_turn():
    ep: Endpointer | None = None
    got = []

    async def handler(event: STTEvent) -> None:
        got.append(event.kind)
        if event.kind == "end_of_turn":
            assert ep is not None
            await ep.reset()  # the session answered: the next words are a new turn

    ep = Endpointer(FakeBatch("first"), handler)
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await ep.idle()
    await feed_frames(ep, np.concatenate([tone(600), quiet(800)]))
    await ep.idle()
    assert got == ["start_of_turn", "end_of_turn", "start_of_turn", "end_of_turn"]
