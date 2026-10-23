"""Wake word ("Hey LittleBot" -> live voice): a fake recogniser on a mock microphone shared with the
real AudioService. No vosk, no model, no hardware, no network."""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Any

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

    def say(self, text: str, conf: float = 1.0) -> None:
        with self.lock:
            self.results.append({"text": text, "result": [{"word": w, "conf": conf} for w in text.split()]})

    def accept(self, pcm16: bytes) -> dict[str, Any] | None:
        with self.lock:
            self.fed += len(pcm16) // 2
            return self.results.popleft() if self.results else None

    def reset(self) -> None:
        self.resets += 1


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

    def __init__(self, ctx: Any, *, error: str | None = None, connect: bool = True) -> None:
        self.ctx = ctx
        self.error = error
        self.connect = connect
        self.calls: list[bool] = []
        self.active = False
        self.phase: str | None = None
        self.last_call: dict[str, Any] | None = None
        self._task: asyncio.Task[Any] | None = None

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "phase": self.phase, "last_call": self.last_call}

    async def start_session(self, *, parent_present: bool) -> dict[str, Any]:
        self.calls.append(parent_present)
        if self.error:
            return {"ok": False, "error": self.error, "detail": "Sign in with ChatGPT first."}
        self.active, self.phase = True, "connecting"
        self._task = asyncio.get_running_loop().create_task(self._run())
        return {"ok": True}

    async def _run(self) -> None:
        audio = self.ctx.audio
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


async def test_pauses_while_a_video_plays(rig):
    r = await rig()
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
        assert not any(s.active for s in fake_sd.in_streams)  # free for live voice's own stream
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
