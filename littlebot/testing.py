"""Deterministic fakes for every subsystem protocol, plus a test context builder.

Used by the test-suite and handy for developing one subsystem without the
others. Every fake records the calls it receives in ``.calls`` as
``(method, args, kwargs)`` tuples and publishes the same bus events the real
implementation would, so integration-style tests stay realistic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from littlebot.core.config import LittleBotConfig, load_config
from littlebot.core.context import RobotContext, build_core_context
from littlebot.core.events import EventBus, Topics
from littlebot.hardware.system import MockSystemMonitor


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for (n, a, k) in self.calls if n == name]


class FakeFace(_Recorder):
    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__()
        self.bus = bus
        self.expression = "neutral"
        self.state = "idle"
        self.gaze = (0.5, 0.5)
        self.eyes_closed = False

    def set_expression(self, name: str, *, hold: float | None = None) -> None:
        self._rec("set_expression", name, hold=hold)
        self.expression = name
        if self.bus:
            self.bus.publish(Topics.FACE_EXPRESSION, {"expression": name}, source="face")

    def look_at(self, x: float, y: float, *, hold: float | None = None) -> None:
        self._rec("look_at", x, y, hold=hold)
        self.gaze = (x, y)

    def track_gaze(self, x: float, y: float) -> None:
        self._rec("track_gaze", x, y)
        self.gaze = (x, y)

    def clear_gaze(self) -> None:
        self._rec("clear_gaze")

    def blink(self, double: bool = False) -> None:
        self._rec("blink", double=double)

    def close_eyes(self) -> None:
        self._rec("close_eyes")
        self.eyes_closed = True

    def open_eyes(self) -> None:
        self._rec("open_eyes")
        self.eyes_closed = False

    def set_state(self, state: str) -> None:
        self._rec("set_state", state)
        self.state = state

    def play_animation(self, name: str) -> None:
        self._rec("play_animation", name)

    def status(self) -> dict[str, Any]:
        return {"expression": self.expression, "state": self.state, "gaze": {"x": self.gaze[0], "y": self.gaze[1]},
                "eyes_closed": self.eyes_closed, "fps": 30.0, "visible": self.state != "hidden"}


class FakeAudio(_Recorder):
    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__()
        self.bus = bus
        self.volume = 0.5
        self.spoken: list[str] = []
        self.sounds: list[str] = []
        self._utterances: asyncio.Queue[str] | None = None
        self._pending_text: list[str] = []
        self.speaking = False

    async def say(self, text: str, *, rate: float | None = None) -> bool:
        self._rec("say", text, rate=rate)
        self.spoken.append(text)
        if self.bus:
            self.bus.publish(Topics.AUDIO_SPEECH_STARTED, {"text": text, "duration_estimate": 0.0}, source="audio")
        self.speaking = True
        await asyncio.sleep(0)
        self.speaking = False
        if self.bus:
            self.bus.publish(Topics.AUDIO_SPEECH_FINISHED, {"text": text, "interrupted": False}, source="audio")
        return True

    async def play_speech(self, samples: Any, sample_rate: int, text: str = "", *, wait: bool = True) -> bool:
        self._rec("play_speech", text, sample_rate=sample_rate)
        self.spoken.append(text)
        await asyncio.sleep(0)
        return True

    def sound_samples(self, name: str) -> Any:
        import numpy as np  # noqa: PLC0415

        return (np.zeros(480, dtype=np.float32), 48000)

    async def play_sound(self, name: str, *, wait: bool = True, volume: float | None = None) -> bool:
        self._rec("play_sound", name, wait=wait, volume=volume)
        self.sounds.append(name)
        if self.bus:
            self.bus.publish(Topics.AUDIO_SOUND_STARTED, {"name": name}, source="audio")
            self.bus.publish(Topics.AUDIO_SOUND_FINISHED, {"name": name}, source="audio")
        await asyncio.sleep(0)
        return True

    async def play_melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 100, wait: bool = True) -> bool:
        self._rec("play_melody", notes, tempo_bpm=tempo_bpm, wait=wait)
        await asyncio.sleep(0)
        return True

    def stop_output(self) -> None:
        self._rec("stop_output")

    async def listen(self, *, timeout: float | None = None) -> str | None:
        self._rec("listen", timeout=timeout)
        if self._pending_text:
            return self._pending_text.pop(0)
        return None

    def inject_utterance(self, text: str) -> None:
        self._rec("inject_utterance", text)
        self._pending_text.append(text)

    def set_volume(self, volume: float) -> float:
        self.volume = max(0.0, min(0.8, float(volume)))
        return self.volume

    def list_devices(self) -> dict[str, Any]:
        return {"inputs": [], "outputs": []}

    async def test_speaker(self) -> dict[str, Any]:
        return {"ok": True, "backend": "fake", "device": None, "detail": "fake"}

    async def test_microphone(self, seconds: float = 2.0) -> dict[str, Any]:
        return {"ok": True, "backend": "fake", "device": None, "peak": 0.0, "rms": 0.0, "detail": "fake"}

    async def record_clip(self, seconds: float = 20.0) -> dict[str, Any]:
        """Like AudioService.record_clip: ``clip`` (samples, rate) is what the next recording "hears"."""
        self._rec("record_clip", seconds)
        clip = getattr(self, "clip", None)
        if clip is None:
            return {"ok": False, "samples": None, "rate": 0, "detail": "fake: nothing to record"}
        return {"ok": True, "samples": clip[0], "rate": clip[1], "detail": "fake"}

    def status(self) -> dict[str, Any]:
        return {"speaker": {"backend": "fake", "device": None, "status": "ok", "detail": "fake"},
                "microphone": {"backend": "fake", "device": None, "status": "ok", "detail": "fake"},
                "volume": self.volume, "max_volume": 0.8, "speaking": self.speaking, "listening": False,
                "tts_provider": "fake", "stt_provider": "fake"}


class FakeMotion(_Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.estop = False

    def gestures(self) -> list[str]:
        return ["wave", "look_left", "look_right", "raise_arms", "celebrate", "dance", "shake", "neutral"]

    async def run_gesture(self, name: str, *, priority: int | None = None, owner: str | None = None) -> bool:
        self._rec("run_gesture", name)
        await asyncio.sleep(0)
        return not self.estop and name in self.gestures()

    async def move_joint(self, joint: str, angle: float, *, speed_dps: float | None = None,
                         priority: int | None = None, owner: str | None = None) -> bool:
        self._rec("move_joint", joint, angle, speed_dps=speed_dps)
        return not self.estop

    async def neutral(self, *, priority: int | None = None, owner: str | None = None) -> bool:
        self._rec("neutral")
        return not self.estop

    def cancel(self) -> None:
        self._rec("cancel")

    def emergency_stop(self, source: str = "unknown") -> None:
        self._rec("emergency_stop", source)
        self.estop = True

    def reset_emergency_stop(self) -> bool:
        self.estop = False
        return True

    def status(self) -> dict[str, Any]:
        joints = {
            "head": {"angle": 90.0, "target": 90.0, "min": 50.0, "max": 130.0, "neutral": 90.0, "channel": 0, "position": 0.0},
            "left_arm": {"angle": 20.0, "target": 20.0, "min": 10.0, "max": 150.0, "neutral": 20.0, "channel": 1, "position": 0.0},
            "right_arm": {"angle": 160.0, "target": 160.0, "min": 30.0, "max": 170.0, "neutral": 160.0, "channel": 2, "position": 0.0},
        }
        return {"backend": "fake", "physical_enabled": False, "armed": False, "estop": self.estop,
                "moving": False, "gesture": None, "simulation_only": True, "reason": "fake motion",
                "joints": joints}


# ---------------------------------------------------------------- voices (littlebot/voices/)
SPEAKER_HZ = tuple(200 + 50 * k for k in range(8))  # fake "speaker" k talks as a steady tone at SPEAKER_HZ[k]
SPEAKER_COMMON = 0.2  # what the fake speakers' voiceprints share (two of them: cosine similarity ~0.15)


def speaker_tone(k: int, seconds: float, rate: int = 16000, *, level: float = 0.2, silence: float = 0.0,
                 int16: bool = False) -> Any:
    """Fake speaker ``k`` "talking" for ``seconds`` (then ``silence`` seconds of quiet), mono at ``rate``."""
    import numpy as np  # noqa: PLC0415

    t = np.arange(int(seconds * rate)) / rate
    x = (level * np.sin(2 * np.pi * SPEAKER_HZ[k] * t)).astype(np.float32)
    if silence:
        x = np.concatenate([x, np.zeros(int(silence * rate), dtype=np.float32)])
    return (x * 32767).astype(np.int16) if int16 else x


class FakeSpeakerEngine:
    """A deterministic stand-in for littlebot.voices.engine.SpeakerEngine (no sherpa-onnx, no model).

    The voiceprint of a sound is the mix of the fake speakers' voiceprints, weighted by how loud each
    speaker's tone (``SPEAKER_HZ``) is in it: one speaker gives exactly theirs (cosine 1.0 with itself,
    ~0.15 with anyone else), two speakers at once a blend (close to both: unsure).
    ``table`` overrides a speaker's voiceprint (any length-``dim`` vector)."""

    name = "fake-speaker.onnx"

    def __init__(self, *, dim: int = 192, table: dict[int, Any] | None = None) -> None:
        import numpy as np  # noqa: PLC0415

        self.dim = dim
        self.calls = 0
        self.seconds: list[float] = []
        common = np.full(dim, 1.0 / np.sqrt(dim), dtype=np.float32)
        self.table = {}
        for k in range(len(SPEAKER_HZ)):
            own = np.zeros(dim, dtype=np.float32)
            own[k % dim] = 1.0
            own -= np.dot(own, common) * common  # orthogonal to the common part
            own /= np.linalg.norm(own)
            self.table[k] = np.sqrt(SPEAKER_COMMON) * common + np.sqrt(1 - SPEAKER_COMMON) * own
        for k, v in (table or {}).items():
            self.table[k] = np.asarray(v, dtype=np.float32)

    def embed(self, samples: Any) -> Any:
        import numpy as np  # noqa: PLC0415

        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        self.calls += 1
        self.seconds.append(len(x) / 16000)
        spectrum = np.abs(np.fft.rfft(x))
        hz = np.fft.rfftfreq(len(x), 1 / 16000)
        v = np.zeros(self.dim, dtype=np.float32)
        for k, f in enumerate(SPEAKER_HZ):
            near = np.abs(hz - f) <= 5
            weight = float(spectrum[near].max()) if near.any() else 0.0
            if weight > 1e-3 * max(1.0, float(spectrum.max())):
                v += weight * self.table[k]
        if not np.any(v):
            v[-1] = 1.0  # noise: like nobody
        return v / np.linalg.norm(v)


class FakeDecisionModel:
    """Stands in for a decision model (littlebot/decide): ``answer(state, questions) -> {name: Answer}``.
    ``use_decision_model(ctx, model)`` puts it in (provider jev, cloud on)."""

    name = "fake"

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[tuple[Any, dict[str, Any]]] = []

    async def ask(self, state: Any, questions: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self.asked.append((state, questions))
        return self.answer(state, questions)


def decision_answers(value: str, confidence: float = 0.9) -> Any:
    """For FakeDecisionModel: every choice answers ``value`` (with ``confidence``); every yes/no question answers
    ``confidence`` as the probability of yes."""
    from littlebot.decide.base import Answer, Choice  # noqa: PLC0415

    def answer(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        return {name: Answer("choice", value, confidence, {value: confidence}) if isinstance(q, Choice)
                else Answer("yesno", confidence, abs(2 * confidence - 1)) for name, q in questions.items()}

    return answer


def use_decision_model(ctx: RobotContext, answer: Any, *, modes: dict[str, str] | None = None) -> FakeDecisionModel:
    from littlebot.decide import get_decider  # noqa: PLC0415

    ctx.privacy.set(cloud_enabled=True)
    ctx.settings.set("decide.provider", "jev")
    if modes is not None:
        ctx.settings.set("decide.modes", modes)
    model = FakeDecisionModel(answer)
    get_decider(ctx).client = model
    return model


class FakeAnthropic:
    """Stands in for ``anthropic.AsyncAnthropic`` (littlebot/brain/providers/claude.py): each ``messages.stream``
    or ``messages.create`` plays the next scripted turn. A turn: ``{"text": str | [pieces], "tools": [(id, name,
    input)], "stop": stop_reason, "delay": seconds before the first event, "error": an exception to raise}``.
    ``calls`` holds every request's arguments (deep-copied), ``models_listed`` the connection warm-ups."""

    def __init__(self, turns: list[dict[str, Any]] | None = None) -> None:
        self.turns = list(turns or [])
        self.calls: list[dict[str, Any]] = []
        self.models_listed = 0
        self.messages = _FakeMessages(self)
        self.models = _FakeModels(self)

    def _next(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        import copy  # noqa: PLC0415

        self.calls.append(copy.deepcopy(kwargs))
        return self.turns.pop(0) if self.turns else {"text": "Okay!"}

    async def close(self) -> None:
        return None


class _FakeModels:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner

    async def list(self, **kw: Any) -> list[Any]:
        self.owner.models_listed += 1
        return []


def _fake_message(turn: dict[str, Any]) -> Any:
    from types import SimpleNamespace  # noqa: PLC0415

    text = turn.get("text", "")
    pieces = text if isinstance(text, list) else ([text] if text else [])
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}] if turn.get(
        "thinking") else []
    if pieces:
        content.append({"type": "text", "text": "".join(pieces)})
    for call_id, name, args in turn.get("tools", ()):
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
    stop = turn.get("stop") or ("tool_use" if turn.get("tools") else "end_turn")
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=turn.get("cached", 0),
                            cache_creation_input_tokens=0)
    return pieces, SimpleNamespace(content=content, stop_reason=stop, usage=usage, model="claude-fake")


class _FakeStream:
    def __init__(self, turn: dict[str, Any]) -> None:
        self.turn = turn
        self.pieces, self.final = _fake_message(turn)

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        return self._events()

    async def _events(self) -> Any:
        from types import SimpleNamespace  # noqa: PLC0415

        if self.turn.get("delay"):
            await asyncio.sleep(self.turn["delay"])
        if self.turn.get("error") is not None:
            raise self.turn["error"]
        for piece in self.pieces:
            yield SimpleNamespace(type="text", text=piece)
        for call_id, name, _args in self.turn.get("tools", ()):
            yield SimpleNamespace(type="content_block_start", content_block={"type": "tool_use", "id": call_id,
                                                                             "name": name})

    async def get_final_message(self) -> Any:
        return self.final


class _FakeMessages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner

    def stream(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self.owner._next(kwargs))

    async def create(self, **kwargs: Any) -> Any:
        turn = self.owner._next(kwargs)
        if turn.get("delay"):
            await asyncio.sleep(turn["delay"])
        if turn.get("error") is not None:
            raise turn["error"]
        return _fake_message(turn)[1]


def make_test_config(**overrides: Any) -> LittleBotConfig:
    """Config for tests: offline-safe, headless, fast activity timing, no real devices."""
    base = {
        "display": {"headless": True},
        "camera": {"backend": "mock"},
        "audio": {"microphone": {"backend": "mock"}, "speaker": {"backend": "mock"},
                  "tts": {"provider": "mock"}, "stt": {"provider": "mock"}},
        "motion": {"backend": "mock", "physical_enabled": False},
        "media": {"player": "none"},
        "activities": {"time_scale": 0.0},
        "logging": {"level": "WARNING"},
        "power": {"monitor": "none"},  # never a battery monitor plugged into the test machine
        # ChatGPT as the text model unless a test chooses Claude (config/default.yaml ships Claude first)
        "text": {"provider": "chatgpt"},
        "claude": {"model": None, "fallback_models": [], "thinking": "adaptive"},
    }
    from littlebot.core.config import deep_merge

    return load_config(mode=overrides.pop("mode", "simulation"), overrides=deep_merge(base, overrides),
                       environ={}, use_dotenv=False, include_local=False)


def make_test_context(*, fakes: bool = True, **config_overrides: Any) -> RobotContext:
    """In-memory DB, mock monitor and (optionally) fake face/audio/motion."""
    cfg = make_test_config(**config_overrides)
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    # Never read or write a real ChatGPT sign-in: a file path that only exists if a test signs in.
    import tempfile  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from littlebot.chatgpt.auth import ChatGPTAuth  # noqa: PLC0415

    ctx.extras["chatgpt_auth"] = ChatGPTAuth(Path(tempfile.gettempdir()) / f"littlebot-test-{uuid.uuid4().hex}" /
                                             "chatgpt_auth.json")
    if fakes:
        ctx.face = FakeFace(ctx.bus)
        ctx.audio = FakeAudio(ctx.bus)
        ctx.motion = FakeMotion()
    return ctx


# ---------------------------------------------------------------------------
# The assistant (littlebot/assistant): call audio, speech-to-text and text-to-speech fakes
# ---------------------------------------------------------------------------

class FakeCallAudio:
    """Stands in for ``chatgpt.live_audio.LiveAudio`` in assistant sessions: silence from the "microphone",
    whatever is played kept in ``played`` (and heard at once: nothing stays queued)."""

    barge_in_on = False

    def __init__(self) -> None:
        import numpy as np  # noqa: PLC0415

        self._np = np
        self.opened = self.closed = False
        self.played: list[tuple[int, int]] = []  # (samples, rate) per play_pcm call
        self.heard_words: list[tuple[str, bool]] = []
        self.thinking = 0
        self.muted = False
        self.dropped = 0
        self.interrupted = False
        self.speaking = False
        self.queued_s = 0.0
        self.mouth = (0.0, "closed")
        self.level = 0.0
        self.on_change: Any = None

    def open(self, loop: Any) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        pass

    def stalled(self) -> str | None:
        return None

    async def read_frame(self) -> Any:
        await asyncio.sleep(0.01)
        return self._np.zeros(960, dtype=self._np.int16)

    def play_pcm(self, samples: Any, rate: int = 48000) -> None:
        self.played.append((len(samples), int(rate)))

    def play_thinking(self) -> bool:
        self.thinking += 1
        return True

    def setup_barge_in(self, **hooks: Any) -> None:
        self.on_change = hooks.get("on_change")

    def barge_in_heard(self, text: str, final: bool) -> str | None:
        self.heard_words.append((text, final))
        return None

    def mute(self) -> None:
        self.muted = True

    def unmute(self) -> None:
        self.muted = False

    def drop_voice(self) -> None:
        self.dropped += 1


class FakeSTTStream:
    def __init__(self, provider: "FakeSTT", on_event: Any) -> None:
        self.provider = provider
        self.on_event = on_event
        self.fed = 0
        self.closed = False
        self.resets = 0

    async def feed(self, samples: Any) -> None:
        self.fed += len(samples)

    async def finish(self) -> None:
        pass

    async def reset(self) -> None:
        self.resets += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeSTT:
    """A speech-to-text provider driven by the test: ``say("text")`` is a whole turn (start, words, end)."""

    streaming = True

    def __init__(self, name: str = "fake-stt", *, fail_open: bool = False) -> None:
        self.name = name
        self.fail_open = fail_open
        self.stream: FakeSTTStream | None = None

    def missing(self) -> str | None:
        return None

    async def open(self, on_event: Any) -> FakeSTTStream:
        if self.fail_open:
            from littlebot.assistant.speech import SpeechError  # noqa: PLC0415

            raise SpeechError("network")
        self.stream = FakeSTTStream(self, on_event)
        return self.stream

    def say(self, text: str) -> None:
        from littlebot.assistant.speech import STTEvent  # noqa: PLC0415

        assert self.stream is not None, "no stream open"
        self.stream.on_event(STTEvent("start_of_turn"))
        self.stream.on_event(STTEvent("partial", text))
        self.stream.on_event(STTEvent("end_of_turn", text))


class FakeTTS:
    """Text-to-speech that makes 10 ms of silence per character (48 kHz) and remembers what it was asked."""

    name = "fake-tts"
    sample_rate = 48000
    last_provider = "fake-tts"

    def __init__(self) -> None:
        self.said: list[str] = []

    def missing(self) -> str | None:
        return None

    async def stream(self, text: str) -> Any:
        import numpy as np  # noqa: PLC0415

        self.said.append(text)
        yield np.zeros(max(1, len(text)) * 480, dtype=np.float32)

    async def warm(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"providers": [self.name]}
