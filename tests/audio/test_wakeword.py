"""Wake word ("Hey LittleBot" -> live voice): a fake recogniser on a mock microphone shared with the
real AudioService. No vosk, no model, no hardware, no network."""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Any

import numpy as np
import pytest

from littlebot.audio import wakeword
from littlebot.audio.devices import MockInput, MockOutput
from littlebot.audio.errors import AudioDeviceError
from littlebot.audio.stt import MockSTT
from littlebot.audio.tts import MockTTS
from littlebot.audio.wakeword import WakeWordService, build_grammar, match_wake_phrase, normalise_phrase
from littlebot.core.events import collect
from littlebot.core.service import ServiceStatus
from littlebot.core.state import RobotState
from littlebot.testing import FakeAudio, make_test_context


# ------------------------------------------------------------------ fakes
class FakeRecognizer:
    """Returns the results queued with ``say`` (one per accept call); counts what it was fed."""

    sample_rate = 16000

    def __init__(self) -> None:
        self.results: collections.deque[dict[str, Any]] = collections.deque()
        self.fed = 0
        self.resets = 0
        self.lock = threading.Lock()
        self.partial_text = ""  # the words so far (VoskKeywordRecognizer.partial)

    def say(self, text: str, conf: float = 1.0, *, wake_ended_ago: float | None = None, wake_words: int = 3) -> None:
        """``wake_ended_ago``: give word times like Vosk (seconds since the first sample it was fed), with the
        first ``wake_words`` words ending that long before the audio fed so far ends."""
        with self.lock:
            result: dict[str, Any] = {"text": text, "result": [{"word": w, "conf": conf} for w in text.split()]}
            if wake_ended_ago is not None:
                result["wake_ended_ago"] = (wake_ended_ago, wake_words)
            self.results.append(result)

    def accept(self, pcm16: bytes) -> dict[str, Any] | None:
        with self.lock:
            self.fed += len(pcm16) // 2
            if not self.results:
                return None
            result = self.results.popleft()
            timing = result.pop("wake_ended_ago", None)
            if timing is not None:
                ago, n = timing
                end = self.fed / self.sample_rate - ago
                for i, w in enumerate(result["result"]):
                    w["start"], w["end"] = max(0.0, end - 0.3 * (n - i)), end + 0.2 * max(0, i - n + 1)
            return result

    def reset(self) -> None:
        self.resets += 1

    def partial(self) -> str:
        return self.partial_text


class ExclusiveMic(MockInput):
    """Like a USB microphone's ALSA hw device: a second open() while open fails."""

    def __init__(self) -> None:
        super().__init__(chunk_seconds=0.02)
        self.conflicts = 0
        self.lock = threading.Lock()
        self.openers: list[str] = []

    def open(self) -> None:
        with self.lock:
            if self._open:
                self.conflicts += 1
                raise AudioDeviceError("Device or resource busy")
            super().open()
            self.openers.append(threading.current_thread().name)


class FakeLive:
    """Enough of LiveVoiceService; like the real one it borrows the audio devices while active."""

    def __init__(self, ctx: Any, *, error: str | None = None, connect: bool = True,
                 take_mic_after: float = 0.0) -> None:
        self.ctx = ctx
        self.error = error
        self.connect = connect
        self.take_mic_after = take_mic_after  # the real call borrows the devices at once; a slow start waits
        self.calls: list[bool] = []
        self.carries: list[Any] = []  # what the wake word carried into each call
        self.active = False
        self.phase: str | None = None
        self.last_call: dict[str, Any] | None = None
        self.heard_user = True  # the person said something in the call
        self.video: str | None = None  # what they asked about a paused video: resume | stop
        self.stopped: list[str] = []
        self._task: asyncio.Task[Any] | None = None

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "phase": self.phase, "last_call": self.last_call,
                "heard_user": self.heard_user}

    async def stop_session(self, reason: str = "stopped by parent") -> dict[str, Any]:
        self.stopped.append(reason)
        await self.end()
        return {"ok": True}

    async def start_session(self, *, parent_present: bool, carry: Any = None) -> dict[str, Any]:
        self.calls.append(parent_present)
        self.carries.append(carry)
        if self.error:
            return {"ok": False, "error": self.error, "detail": "Sign in with ChatGPT first."}
        self.active, self.phase = True, "connecting"
        self._task = asyncio.get_running_loop().create_task(self._run())
        return {"ok": True}

    async def _run(self) -> None:
        audio = self.ctx.audio
        if self.take_mic_after:
            await asyncio.sleep(self.take_mic_after)
        await audio.suspend("live voice")
        await asyncio.sleep(0.05)
        if self.connect:
            self.phase = "listening"
            return
        audio.resume("live voice")
        self.active, self.phase = False, None
        self.last_call = {"connected": False, "error": "Could not connect to ChatGPT live voice."}

    async def end(self) -> None:
        if self.active:
            self.ctx.audio.resume("live voice")
            self.last_call = {"connected": True, "heard_user": self.heard_user, "video": self.video}
        self.active, self.phase = False, None


async def until(pred, timeout: float = 4.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or pred}")
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
async def rig():
    """Real AudioService (mock speaker, exclusive mock mic) + WakeWordService with a fake recogniser."""
    made: list[Any] = []

    async def _make(*, error: str | None = None, connect: bool = True, start_wake: bool = True, **cfg: Any):
        cfg.setdefault("wake", {})
        cfg["wake"] = {"cooldown_seconds": 0.3, **cfg["wake"]}
        ctx = make_test_context(fakes=False, **cfg)
        from littlebot.audio.service import AudioService

        mic = ExclusiveMic()
        out = MockOutput(time_scale=0.0)
        audio = AudioService(ctx, output=out, input=mic, tts=MockTTS(), stt=MockSTT())
        ctx.audio = audio
        live = FakeLive(ctx, error=error, connect=connect)
        ctx.live = live
        rec = FakeRecognizer()
        wake = WakeWordService(ctx, recognizer_factory=lambda _rate: rec, allow_simulated_mic=True)
        ctx.wake = wake
        await audio.start()
        if start_wake:
            await wake.start()
        r = type("Rig", (), dict(ctx=ctx, audio=audio, mic=mic, out=out, live=live, rec=rec, wake=wake))
        made.append(r)
        return r

    yield _make
    for r in made:
        await r.wake.stop()
        await r.live.end()
        await r.audio.stop()
        r.ctx.db.close()


def _sounds(out: MockOutput) -> list[str]:
    return [p.label for p in out.played]


async def _listening(r: Any) -> None:
    await until(lambda: r.wake.status()["listening"] and r.mic.is_open, what="wake word listening")


async def _not_listening(r: Any, paused: str | None = None) -> None:
    await until(lambda: not r.wake.status()["listening"] and not r.mic.is_open, what="wake word paused")
    if paused is not None:
        await until(lambda: r.wake.status()["paused"] == paused, what=f"paused={paused}")


# ------------------------------------------------------------------ pure helpers
@pytest.mark.parametrize("raw,norm", [
    ("Hey LittleBot!", "hey little bot"), ("hey littlebot", "hey little bot"), ("HEY, Little  Bot.", "hey little bot"),
    ("Hey Bot", "hey bot"), ("", ""),
])
def test_normalise_phrase(raw, norm):
    assert normalise_phrase(raw) == norm


def test_build_grammar_dedupes_drops_unknown_words_and_adds_distractors():
    known = {"hey", "little", "bot", "look", "robot", "bob"}
    grammar, usable, dropped = build_grammar(["hey little bot", "hey LittleBot", "hey bot", "hey zorblax"],
                                             known=lambda w: w in known)
    assert usable == [("hey little bot", ("hey", "little", "bot")), ("hey bot", ("hey", "bot"))]
    assert dropped == ["hey zorblax"]
    assert grammar[:2] == ["hey little bot", "hey bot"] and grammar[-1] == "[unk]"
    assert {"look", "robot", "bob"} <= set(grammar) and "zorblax" not in grammar
    assert set(grammar) - {"hey little bot", "hey bot", "[unk]"} <= known  # only words the model knows
    plain, _, _ = build_grammar(["hey bot"], distractors=False)
    assert plain == ["hey bot", "[unk]"]


def _res(*words, conf=1.0):
    return {"text": " ".join(words), "result": [{"word": w, "conf": conf} for w in words]}


def test_match_wake_phrase():
    _, phrases, _ = build_grammar(["hey little bot", "hey bot"], distractors=False)
    assert match_wake_phrase(_res("hey", "little", "bot"), phrases) == "hey little bot"
    assert match_wake_phrase(_res("hey", "bot"), phrases) == "hey bot"
    assert match_wake_phrase(_res("[unk]", "hey", "little", "bot", "[unk]"), phrases) == "hey little bot"
    assert match_wake_phrase(_res("hey", "[unk]", "bot"), phrases) is None  # words must be consecutive
    assert match_wake_phrase(_res("little", "bot"), phrases) is None
    assert match_wake_phrase(_res("hey"), phrases) is None
    assert match_wake_phrase(_res("bot", "hey"), phrases) is None
    assert match_wake_phrase(_res("hey", "bot", conf=0.4), phrases, 0.5) is None
    assert match_wake_phrase(_res("hey", "bot", conf=0.6), phrases, 0.5) == "hey bot"
    assert match_wake_phrase({"text": "hey little bot"}, phrases) == "hey little bot"  # no word details
    assert match_wake_phrase({}, phrases) is None


# ------------------------------------------------------------------ detection
async def test_wake_phrase_plays_earcon_and_starts_live_voice_once(rig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig()
    await _listening(r)
    events = collect(r.ctx.bus, ["audio.wake_word", "state.changed"])
    r.rec.say("hey little bot")
    await until(lambda: r.live.calls, what="live voice started")
    assert r.live.calls == [True]  # parent_present=True: the parent turned the wake word on
    assert "sound:listen" in _sounds(r.out)
    assert "sound:uhoh" not in _sounds(r.out)
    assert r.ctx.state.state == RobotState.LISTENING
    assert [e.data for e in events if e.topic == "audio.wake_word"] == [{"phrase": "hey little bot"}]
    assert any(e.data.get("new") == "LISTENING" and e.data.get("reason") == "wake word" for e in events)
    assert "wake word heard (phrase=hey little bot)" in caplog.text
    # live voice borrows the microphone: the wake word lets go of it and hears nothing meanwhile
    await _not_listening(r)
    assert r.audio.suspended == "live voice"
    r.rec.say("hey bot")
    await asyncio.sleep(0.6)
    assert r.live.calls == [True] and r.wake.triggers == 1
    assert r.mic.conflicts == 0
    st = r.wake.status()
    assert st["last_heard"]["phrase"] == "hey little bot" and st["last_heard"]["result"] in ("started", "connected")
    assert st["paused"] in ("live_voice", "mic_in_use", "cooldown")


async def test_listening_again_after_the_live_call(rig):
    r = await rig()
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: r.live.calls and r.audio.suspended)
    await _not_listening(r)
    await r.live.end()
    await _listening(r)
    assert r.mic.conflicts == 0


async def test_live_voice_blocked_plays_uhoh_and_does_not_loop(rig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig(error="not_signed_in")
    await _listening(r)
    r.rec.say("hey little bot")
    await until(lambda: "sound:uhoh" in _sounds(r.out), what="uhoh")
    assert _sounds(r.out).index("sound:listen") < _sounds(r.out).index("sound:uhoh")
    assert r.live.calls == [True]
    assert "live voice could not start (not_signed_in" in caplog.text
    await until(lambda: r.ctx.state.state == RobotState.IDLE, what="state back to idle")
    st = r.wake.status()
    assert st["last_heard"]["result"] == "blocked" and st["last_heard"]["error"] == "not_signed_in"
    await _listening(r)  # back after the cooldown, but no new attempt without a new phrase
    await asyncio.sleep(0.5)
    assert r.live.calls == [True] and r.wake.triggers == 1


async def test_failed_connect_plays_uhoh(rig):
    r = await rig(connect=False)
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: "sound:uhoh" in _sounds(r.out), what="uhoh after a failed connect")
    assert r.wake.status()["last_heard"]["result"] == "failed"
    await _listening(r)


async def test_log_says_why_the_earcon_was_not_heard(rig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig()
    r.out.fail_on_open = True  # the USB speaker is gone
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: r.live.calls, what="live voice started anyway")
    assert "wake word: earcon 'listen' not played (simulated speaker, volume 50%, speaker unavailable: " \
           "simulated: speaker missing; see the audio log)" in caplog.text
    assert "wake word: starting live voice" in caplog.text and "wake word: live voice started" in caplog.text
    await until(lambda: "wake word: live voice connected" in caplog.text, what="connected logged")
    await r.live.end()
    await _listening(r)
    assert "wake word: listening again" in caplog.text
    # volume 0: it "played", and the log says nothing could be heard
    q = await rig()
    q.audio.set_volume(0.0)
    await _listening(q)
    q.rec.say("hey bot")
    await until(lambda: q.live.calls, what="live voice started")
    assert "earcon 'listen' played (simulated speaker, volume 0%, speaker simulated: simulated speaker (nothing is " \
           "heard): the volume is 0, so nothing was heard)" in caplog.text


async def test_log_says_when_the_microphone_works_again(rig, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    monkeypatch.setattr(wakeword, "RETRY_SECONDS", 0.05)
    r = await rig(start_wake=False)
    real_open, fails = r.mic.open, [1, 2]

    def flaky_open() -> None:
        if fails:
            fails.pop()
            raise AudioDeviceError("Device or resource busy")
        real_open()

    r.mic.open = flaky_open
    await r.wake.start()
    await _listening(r)
    assert caplog.text.count("could not open the microphone") == 1  # only the first failure is a warning
    assert "wake word: the microphone works again (after 2 tries)" in caplog.text


async def test_a_stuck_speaker_never_holds_up_live_voice(rig, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    monkeypatch.setattr(wakeword, "EARCON_TIMEOUT", 0.2)
    r = await rig(wake={"sound": "chime"})  # 1.35 s, played in real time: "stuck" for longer than the timeout
    r.out.time_scale = 1.0
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: r.live.calls, timeout=1.0, what="live voice started without waiting for the speaker")
    assert "earcon 'chime' not played within 0 s (the speaker seems stuck); going on" in caplog.text
    await until(lambda: "wake word: live voice connected" in caplog.text, what="connected")
    await r.live.end()
    await _listening(r)


async def test_debounce_no_retrigger_within_cooldown(rig):
    r = await rig(error="cloud_off", wake={"cooldown_seconds": 1.5})
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: r.live.calls)
    r.rec.say("hey bot")  # straight away again
    await asyncio.sleep(0.8)
    assert r.live.calls == [True] and r.wake.status()["paused"] == "cooldown"
    await _listening(r)  # after the cooldown it listens again; the queued result is long gone or ignored
    r.rec.results.clear()
    r.rec.say("hey little bot")
    await until(lambda: len(r.live.calls) == 2)


async def test_other_words_and_low_confidence_do_not_trigger(rig):
    r = await rig(wake={"min_confidence": 0.7})
    await _listening(r)
    for text, conf in (("hey look", 1.0), ("little bot", 1.0), ("hey [unk] bot", 1.0), ("hey bot", 0.5)):
        r.rec.say(text, conf)
    await until(lambda: not r.rec.results, what="results consumed")
    await asyncio.sleep(0.2)
    assert r.live.calls == [] and r.wake.triggers == 0 and r.wake.status()["listening"]


# ------------------------------------------------------------------ carrying what was said into the call
def test_wake_phrase_end_uses_the_recognisers_word_times():
    words = ("hey", "little", "bot")
    timed = {"text": "hey little bot what [unk]", "result": [
        {"word": "hey", "start": 0.0, "end": 1.05, "conf": 1.0}, {"word": "little", "start": 1.05, "end": 1.38},
        {"word": "bot", "start": 1.38, "end": 1.8}, {"word": "what", "start": 1.89, "end": 2.19},
        {"word": "[unk]", "start": 2.31, "end": 2.9}]}
    assert wakeword.wake_phrase_end(timed, words) == (1.8, 2)
    assert wakeword.wake_phrase_end({"text": "look hey little bot", "result": [
        {"word": "look", "end": 0.4}, {"word": "hey", "end": 0.7}, {"word": "little", "end": 0.9},
        {"word": "bot", "end": 1.2}]}, words) == (1.2, 0)
    assert wakeword.wake_phrase_end(_res("hey", "little", "bot", "now"), words) == (None, 1)  # no word times
    assert wakeword.wake_phrase_end({"text": "hey little bot"}, words) == (None, 0)
    assert wakeword.wake_phrase_end(_res("hey", "little", "bot", conf=0.3), words, 0.5) == (None, 0)


def test_preroll_keeps_only_the_sentence_being_recognised():
    from littlebot.audio.carry import PreRoll

    pre = PreRoll(10, max_seconds=1.0, keep_seconds=0.3)  # 10 samples a second: easy numbers
    at = 0
    for i in range(15):  # 1.5 s in 0.1 s blocks: only the last second (and a block) stays
        pre.add(at, np.full(1, i, dtype=np.float32))
        at += 1
    assert 1.0 <= pre.seconds <= 1.1 and float(pre.since(None)[-1][0]) == 14
    assert [float(b[0]) for b in pre.since(12)] == [12, 13, 14]  # from sample 12 on
    pre.decided()  # the recogniser decided: no wake phrase in it
    assert pre.seconds <= 0.4
    pre.clear()
    assert pre.since(None) == []


def test_speech_carry_is_handed_over_once_and_dropping_forgets_it():
    from littlebot.audio.carry import SpeechCarry

    c = SpeechCarry(16000, quiet_level=0.01)
    assert c.add(np.full(1600, 0.1, dtype=np.float32)) and c.quiet_s == 0.0
    assert c.add(np.zeros(3200, dtype=np.float32)) and c.quiet_s == pytest.approx(0.2)
    assert c.seconds == pytest.approx(0.3) and c.speech_seconds == pytest.approx(0.1)
    c.seal("live voice took the microphone")
    assert not c.add(np.ones(10, dtype=np.float32)) and c.seconds == pytest.approx(0.3)  # nothing after sealing
    assert len(c.take()) == 4800 and len(c.take()) == 0  # once
    d = SpeechCarry(16000)
    d.add(np.ones(100, dtype=np.float32))
    d.drop("live voice could not start")
    assert d.dropped and len(d.take()) == 0 and not d.add(np.ones(10, dtype=np.float32))


async def test_said_in_one_breath_no_earcon_and_what_followed_goes_to_live_voice(rig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig()
    await _listening(r)
    await until(lambda: r.rec.fed >= 1.5 * 16000, what="1.5 s heard")  # some audio before the sentence: not carried
    r.rec.say("hey little bot what is the weather", wake_ended_ago=0.6)
    await until(lambda: r.live.calls, what="live voice started")
    carry = r.live.carries[0]
    await until(lambda: not carry.is_open, what="the microphone handed over")
    assert "sound:listen" not in _sounds(r.out)  # they asked already: no earcon
    assert "wake word: no earcon, they kept talking" in caplog.text
    assert carry.kept_talking and carry.phrase == "hey little bot" and carry.state == "sealed"
    # from where the wake phrase ended (0.6 s before the result, the recogniser's step earlier), to the hand-over
    assert 0.6 <= carry.lead_s <= 0.6 + wakeword.WAKE_CUT_MARGIN + 0.15
    assert carry.seconds >= carry.lead_s and carry.why == "live voice took the microphone"
    assert "wake word: kept" in caplog.text and "ms after the wake phrase" in caplog.text
    await _not_listening(r)
    assert r.audio.suspended == "live voice" and r.mic.conflicts == 0


async def test_only_the_wake_phrase_plays_the_earcon_and_still_carries(rig):
    r = await rig()
    await _listening(r)
    await until(lambda: r.rec.fed >= 16000, what="1 s heard")
    r.rec.say("hey little bot", wake_ended_ago=0.5)  # then a pause: they wait for the sound
    await until(lambda: r.live.calls, what="live voice started")
    carry = r.live.carries[0]
    assert "sound:listen" in _sounds(r.out) and not carry.kept_talking
    await until(lambda: not carry.is_open, what="the microphone handed over")
    assert carry.state == "sealed" and carry.lead_s <= 0.5 + wakeword.WAKE_CUT_MARGIN + 0.15


async def test_without_word_times_the_whole_sentence_is_carried(rig):
    r = await rig()
    await _listening(r)
    r.rec.say("hey look")  # a sentence without the wake phrase: forgotten when the recogniser decides
    await until(lambda: not r.rec.results, what="decided")
    await asyncio.sleep(0.4)
    r.rec.say("hey little bot")  # no word times (some recognisers): the sentence so far, wake phrase and all
    await until(lambda: r.live.calls, what="live voice started")
    lead = r.live.carries[0].lead_s
    assert 0.3 <= lead <= 1.2  # since "hey look" was decided (a pause of ~0.3 s kept), never more


async def test_carried_words_are_dropped_when_live_voice_cannot_start(rig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig(error="not_signed_in")
    await _listening(r)
    await until(lambda: r.rec.fed >= 16000, what="1 s heard")
    r.rec.say("hey little bot what is the weather", wake_ended_ago=0.6)
    await until(lambda: "sound:uhoh" in _sounds(r.out), what="uhoh")
    carry = r.live.carries[0]
    assert carry.dropped and carry.why == "live voice could not start" and len(carry.take()) == 0
    assert "wake word: kept" not in caplog.text
    await _listening(r)  # after the cooldown
    assert r.mic.conflicts == 0


async def test_what_littlebot_plays_while_carrying_is_never_carried(rig):
    r = await rig()
    r.live.take_mic_after = 0.6  # live voice takes its time to borrow the microphone
    r.out.time_scale = 1.0  # sounds take real time
    await _listening(r)
    r.rec.say("hey little bot what is that", wake_ended_ago=0.3)
    await until(lambda: r.live.carries, what="live voice started")
    carry = r.live.carries[0]
    play = asyncio.ensure_future(r.audio.play_sound("chime"))  # 1.35 s: LittleBot makes a sound meanwhile
    await until(lambda: r.audio.mic_muted(), what="the sound plays")
    held = carry.seconds
    await until(lambda: not carry.is_open, what="the microphone handed over")
    assert carry.seconds - held < 0.1  # nothing heard while it played was kept (else ~0.6 s more)
    await play


def test_while_they_talk_the_microphone_is_handed_over_at_their_next_pause():
    from littlebot.audio.carry import SpeechCarry

    class Talking:
        """``talk`` seconds of talking, then quiet; real-time chunks of 32 ms."""

        sample_rate = 16000
        is_open = True

        def __init__(self, talk: float) -> None:
            self.t0, self.talk = time.monotonic(), talk

        def read(self, timeout: float) -> np.ndarray:
            time.sleep(0.032)
            loud = time.monotonic() - self.t0 < self.talk
            return np.full(512, 0.2 if loud else 0.0, dtype=np.float32)

    ctx = make_test_context()
    try:
        wake = WakeWordService(ctx, recognizer_factory=lambda _rate: FakeRecognizer())
        wake._audio_busy, wake._busy_reason = True, "lent"  # live voice asks for it at once
        carry = SpeechCarry(16000, quiet_level=0.01)
        carry.add(np.full(1600, 0.2, dtype=np.float32))  # they were talking when the recogniser decided
        t0 = time.monotonic()
        wake._carry_on(Talking(0.15), 16000, carry)
        took = time.monotonic() - t0
        assert 0.15 + wakeword.HANDOVER_QUIET_SECONDS <= took < wakeword.HANDOVER_WAIT_SECONDS  # at the pause
        assert carry.state == "sealed" and "at a pause" in carry.why and carry.speech_seconds >= 0.2
        endless = SpeechCarry(16000, quiet_level=0.01)
        endless.add(np.full(1600, 0.2, dtype=np.float32))
        t0 = time.monotonic()
        wake._carry_on(Talking(10.0), 16000, endless)  # no pause (a noisy room): not longer than the limit
        assert wakeword.HANDOVER_WAIT_SECONDS <= time.monotonic() - t0 < wakeword.HANDOVER_WAIT_SECONDS + 0.15
        assert "while they were still talking" in endless.why
        quiet = SpeechCarry(16000, quiet_level=0.01)
        quiet.add(np.zeros(1600, dtype=np.float32))  # already quiet: handed over straight away
        t0 = time.monotonic()
        wake._carry_on(Talking(0.15), 16000, quiet)
        assert time.monotonic() - t0 < 0.05 and quiet.seconds == pytest.approx(0.1)
    finally:
        ctx.db.close()


# ------------------------------------------------------------------ microphone sharing / pauses
async def test_listen_takes_the_microphone_from_the_wake_word(rig):
    r = await rig()
    await _listening(r)
    assert r.wake.status()["state"] == "listening"
    text_task = asyncio.ensure_future(r.audio.listen(timeout=0.3))
    await until(lambda: not r.wake.status()["listening"], what="wake word paused for listen()")
    assert await text_task is None  # nothing said; but the capture thread got the mic
    assert r.mic.conflicts == 0
    assert len(r.mic.openers) >= 2 and r.mic.openers[0] == "littlebot-wake" and r.mic.openers[1] != "littlebot-wake"
    await _listening(r)
    assert r.mic.openers[-1] == "littlebot-wake" and r.mic.conflicts == 0
    # injected (simulated) speech still reaches listen()
    r.audio.inject_utterance("banana")
    assert await r.audio.listen(timeout=1.0) == "banana"
    await _listening(r)
    assert r.mic.conflicts == 0


async def test_microphone_test_and_suspend_pause_it(rig):
    r = await rig()
    await _listening(r)
    res = await r.audio.test_microphone(0.3)
    assert "simulated" in res["detail"]  # the mock mic isn't real; still handed over cleanly
    await _listening(r)
    await r.audio.suspend("live voice")
    assert not r.mic.is_open and not r.wake.status()["listening"]  # closed before suspend() returned
    await asyncio.sleep(0.6)
    assert not r.mic.is_open and r.wake.status()["paused"] == "mic_in_use"
    r.audio.resume("live voice")
    await _listening(r)
    assert r.mic.conflicts == 0


async def test_pauses_while_live_voice_is_active(rig):
    r = await rig()
    await _listening(r)
    r.live.active = True  # started from the dashboard, before it borrowed the devices
    await _not_listening(r, "live_voice")
    r.live.active = False
    await _listening(r)


async def test_pauses_while_a_video_plays_when_switched_off_for_videos(rig):
    r = await rig(wake={"during_videos": False})
    await _listening(r)
    assert r.ctx.state.transition(RobotState.PLAYING_MEDIA, "test video")
    await _not_listening(r, "video")
    r.rec.say("hey bot")
    await asyncio.sleep(0.3)
    assert r.live.calls == []
    r.ctx.state.transition(RobotState.IDLE, "video over")
    await _listening(r)


async def test_privacy_microphone_switch(rig):
    r = await rig()
    await _listening(r)
    r.ctx.privacy.set(microphone_enabled=False)
    await _not_listening(r, "microphone_off")
    assert r.wake.status()["detail"].startswith("Paused: the microphone is turned off")
    r.ctx.privacy.set(microphone_enabled=True)
    await _listening(r)


async def test_ignores_audio_while_littlebot_makes_sound(rig):
    r = await rig()
    r.out.time_scale = 1.0  # sounds take real time now
    await _listening(r)
    await until(lambda: r.rec.fed > 0, what="audio fed")
    resets = r.rec.resets
    play = asyncio.ensure_future(r.audio.play_sound("chime"))  # 1.35 s
    await until(lambda: r.audio.mic_muted(), what="output busy")
    await asyncio.sleep(0.15)
    fed = r.rec.fed
    await asyncio.sleep(0.8)
    assert r.audio.mic_muted() and r.rec.fed == fed  # nothing fed while the speaker plays
    assert r.rec.resets > resets
    await play
    await until(lambda: r.rec.fed > fed, what="feeding again after the sound")


async def test_disabled_setting(rig):
    r = await rig(wake={"enabled": False})
    await until(lambda: r.wake.status()["state"] == "disabled")
    await asyncio.sleep(0.3)
    await until(lambda: r.wake.health()["status"] == ServiceStatus.DISABLED.value, what="health: disabled")
    assert r.mic.opens == 0
    r.ctx.settings.set("wake.enabled", True)
    await _listening(r)
    r.ctx.settings.set("wake.enabled", False)
    await _not_listening(r, "disabled")
    r.rec.say("hey bot")
    await asyncio.sleep(0.3)
    assert r.live.calls == []
    st = r.wake.status()
    assert st["enabled"] is False and st["state"] == "disabled" and st["listening"] is False


async def test_wake_enabled_defaults_on():
    ctx = make_test_context()
    try:
        assert ctx.settings.get("wake.enabled") is True
        assert ctx.config.wake.phrases == ["hey little bot", "hey littlebot", "hey bot"]
    finally:
        ctx.db.close()


async def test_stop_releases_the_microphone(rig):
    r = await rig()
    await _listening(r)
    await r.wake.stop()
    assert not r.mic.is_open and r.wake.status()["state"] == "stopped"
    assert r.wake.health()["status"] == ServiceStatus.STOPPED.value


async def test_audio_stop_takes_the_microphone_back(rig):
    r = await rig()
    await _listening(r)
    await r.audio.stop()
    await _not_listening(r)
    assert r.mic.conflicts == 0


# ------------------------------------------------------------------ availability
async def test_missing_model_is_reported_not_crashing(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    monkeypatch.setattr(wakeword, "vosk_installed", lambda: True)
    ctx = make_test_context(fakes=False, wake={"model_path": str(tmp_path / "no-such-model")})
    from littlebot.audio.service import AudioService

    mic = ExclusiveMic()
    ctx.audio = AudioService(ctx, output=MockOutput(time_scale=0.0), input=mic, tts=MockTTS(), stt=MockSTT())
    wake = WakeWordService(ctx, allow_simulated_mic=True)
    try:
        await ctx.audio.start()
        await wake.start()
        await until(lambda: wake.status()["state"] == "unavailable")
        st = wake.status()
        assert st["available"] is False and st["model_missing"] is True and st["unavailable"] == "model_missing"
        assert "download_models.py vosk-en" in st["detail"] and st["listening"] is False
        await until(lambda: wake.health()["status"] == ServiceStatus.DEGRADED.value, what="health: degraded")
        await asyncio.sleep(0.3)
        assert mic.opens == 0  # never opened the microphone
        assert "wake word unavailable" in caplog.text
        ctx.settings.set("wake.enabled", False)  # off: still says what is missing (for the page)
        await until(lambda: wake.status()["state"] == "disabled")
        st = wake.status()
        assert st["available"] is False and "vosk-en" in st["unavailable_detail"] and st["detail"] == "Off."
        await until(lambda: wake.health()["status"] == ServiceStatus.DISABLED.value, what="health: disabled")
    finally:
        await wake.stop()
        await ctx.audio.stop()
        ctx.db.close()


async def test_without_vosk_installed(monkeypatch):
    monkeypatch.setattr(wakeword, "vosk_installed", lambda: False)
    ctx = make_test_context(fakes=False)
    from littlebot.audio.service import AudioService

    ctx.audio = AudioService(ctx, output=MockOutput(time_scale=0.0), input=MockInput(), tts=MockTTS(), stt=MockSTT())
    wake = wakeword.create_service(ctx)
    try:
        await ctx.audio.start()
        await wake.start()
        await until(lambda: wake.status()["state"] == "unavailable")
        st = wake.status()
        assert st["unavailable"] == "not_installed" and "local-stt" in st["detail"] and not st["model_missing"]
    finally:
        await wake.stop()
        await ctx.audio.stop()
        ctx.db.close()


async def test_unloadable_model_is_reported(rig):
    def broken(_rate):
        raise RuntimeError("Failed to create a model")

    r = await rig(start_wake=False)
    wake = WakeWordService(r.ctx, recognizer_factory=broken, allow_simulated_mic=True)
    try:
        await wake.start()
        await until(lambda: wake.status()["state"] == "unavailable")
        assert wake.status()["unavailable"] == "load_failed" and "Failed to create a model" in wake.status()["detail"]
        assert r.mic.opens == 0
    finally:
        await wake.stop()


async def test_with_a_fake_audio_service_it_is_unavailable():
    ctx = make_test_context()  # FakeAudio: no microphone sharing
    assert isinstance(ctx.audio, FakeAudio)
    wake = wakeword.create_service(ctx)
    try:
        await wake.start()
        await until(lambda: wake.status()["state"] == "unavailable")
        assert wake.status()["unavailable"] == "no_audio"
    finally:
        await wake.stop()
        ctx.db.close()


async def test_simulated_microphone_is_not_used_by_default(rig):
    r = await rig(start_wake=False)
    wake = WakeWordService(r.ctx, recognizer_factory=lambda _rate: r.rec)
    try:
        await wake.start()
        await until(lambda: wake.status()["paused"] == "no_microphone")
        assert r.mic.opens == 0 and "simulated microphone" in wake.status()["detail"]
    finally:
        await wake.stop()


# ------------------------------------------------------------------ scripts/download_models.py vosk-en
@pytest.fixture
def dl():
    import importlib.util
    import sys
    from pathlib import Path

    name = "littlebot_download_models_wake"
    path = Path(__file__).resolve().parents[2] / "scripts" / "download_models.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(name, None)


def _zip(path, files: dict[str, bytes]) -> bytes:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path.read_bytes()


def test_download_models_vosk_archive_is_verified_and_unpacked(dl, tmp_path):
    import dataclasses
    import hashlib

    from littlebot.audio.stt.vosk import model_present

    real = dl.ARCHIVES["vosk-en"]
    assert real.dirname == "vosk-model-small-en-us-0.15" and len(real.sha256) == 64 and len(real.urls) >= 2
    assert "vosk-en" in dl.ARCHIVES and "vosk-en" not in dl.MODELS
    from littlebot.core.config import WakeConfig

    assert WakeConfig().model_path == f"models/{real.dirname}"  # where the wake word looks for it

    src = tmp_path / "m.zip"
    payload = _zip(src, {"m/conf/model.conf": b"--beam=10\n", "m/am/final.mdl": b"x" * 100, "m/README": b"hi"})
    arc = dataclasses.replace(real, dirname="m", filename="m.zip", sha256=hashlib.sha256(payload).hexdigest(),
                              size=len(payload), urls=[(tmp_path / "missing.zip").as_uri(), src.as_uri()])
    dest = tmp_path / "models"
    assert dl.download_archive(arc, dest, quiet=True)
    assert sorted(p.name for p in dest.iterdir()) == ["m"]  # unpacked; the zip and staging are gone
    assert (dest / "m" / "am" / "final.mdl").read_bytes() == b"x" * 100
    assert dl.archive_ready(arc, dest) and model_present(dest / "m")
    assert dl.download_archive(arc, dest, quiet=True)  # already there
    assert dl.download_archive(arc, dest, force=True, quiet=True)  # replaced in place
    assert sorted(p.name for p in dest.iterdir()) == ["m"]

    bad = dataclasses.replace(arc, sha256="0" * 64)
    assert not dl.download_archive(bad, tmp_path / "other", force=True, quiet=True)
    assert list((tmp_path / "other").iterdir()) == []

    evil_src = tmp_path / "evil.zip"
    evil = _zip(evil_src, {"m/conf/model.conf": b"", "m/../../escape.txt": b"no"})
    evil_arc = dataclasses.replace(arc, sha256=hashlib.sha256(evil).hexdigest(), size=len(evil),
                                   urls=[evil_src.as_uri()])
    assert not dl.download_archive(evil_arc, tmp_path / "evil", quiet=True)
    assert not (tmp_path / "escape.txt").exists() and list((tmp_path / "evil").iterdir()) == []


def test_download_models_cli_knows_vosk(dl, tmp_path, capsys, monkeypatch):
    assert dl.main(["--list", "--dest", str(tmp_path)]) == 0
    assert "vosk-en" in capsys.readouterr().out
    fetched: list[str] = []
    monkeypatch.setattr(dl, "download_archive", lambda a, dest, **kw: fetched.append(a.dirname) or True)
    assert dl.main(["vosk-en", "--dest", str(tmp_path)]) == 0
    assert fetched == ["vosk-model-small-en-us-0.15"]


# ------------------------------------------------------------------ the real PortAudio backend (fake PortAudio)
async def test_real_input_backend_is_shared_never_opened_twice(fake_sd):
    """With the real SoundDeviceInput: the wake word, listen() and live voice take turns on one stream."""
    from littlebot.audio.devices import SoundDeviceInput
    from littlebot.audio.service import AudioService

    refused: list[int] = []
    original = fake_sd.InputStream

    def exclusive(**kw):  # like ALSA hw: "Device unavailable" while another stream is running
        if any(s.active for s in fake_sd.in_streams):
            refused.append(kw.get("device"))
            raise fake_sd.PortAudioError("Device unavailable [PaErrorCode -9985]")
        return original(**kw)

    fake_sd.InputStream = exclusive
    ctx = make_test_context(fakes=False, wake={"cooldown_seconds": 0.3}, audio={"microphone": {"backend": "auto"}})
    audio = AudioService(ctx, output=MockOutput(time_scale=0.0), tts=MockTTS(), stt=MockSTT())
    ctx.audio = audio
    ctx.live = FakeLive(ctx)
    rec = FakeRecognizer()
    wake = WakeWordService(ctx, recognizer_factory=lambda _rate: rec)  # a real microphone: no simulation flag
    try:
        await audio.start()
        assert isinstance(audio.background_input(), SoundDeviceInput)
        await wake.start()
        await until(lambda: wake.status()["listening"] and rec.fed > 8000, what="listening on the USB mic")
        assert fake_sd.in_streams[-1].samplerate == 48000  # 48 kHz device, resampled to 16 kHz for the recogniser
        assert await audio.listen(timeout=0.3) is None  # push to talk gets the same device
        await until(lambda: wake.status()["listening"], what="back after listen()")
        rec.say("hey little bot")
        await until(lambda: ctx.live.calls and audio.suspended == "live voice", what="live voice")
        # free for live voice's own stream (at a pause: the fake microphone's tone never pauses, so after the wait)
        await until(lambda: not any(s.active for s in fake_sd.in_streams), timeout=wakeword.HANDOVER_WAIT_SECONDS + 1,
                    what="the microphone handed over")
        await ctx.live.end()
        await until(lambda: wake.status()["listening"], what="back after the call")
        assert refused == [] and len(fake_sd.in_streams) >= 3
        assert sum(s.active for s in fake_sd.in_streams) == 1
    finally:
        await wake.stop()
        await ctx.live.end()
        await audio.stop()
        ctx.db.close()
    assert not any(s.active for s in fake_sd.in_streams)
