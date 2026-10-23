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
