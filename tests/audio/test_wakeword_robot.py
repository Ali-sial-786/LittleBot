"""Wake word on the whole robot: the real composition (LittleBot.build_subsystems) with the real
AudioService on a fake PortAudio (USB speaker + USB microphone), the real LiveVoiceService calling a
local WebRTC peer that stands in for OpenAI, the real behaviours and a mock camera. Only the
keyword recogniser (no vosk model) and the live call's sound card (FakeLiveAudio) are fakes.

"Hey bot" -> earcon on the speaker -> live call connects -> the voice reaches the speaker -> after the
call the wake word listens again; awake (IDLE, with a person in front of the camera) and asleep.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np
import pytest

pytest.importorskip("aiortc")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402
from test_wakeword import FakeRecognizer  # noqa: E402

from littlebot.app.robot import LittleBot  # noqa: E402
from littlebot.audio.devices import SoundDeviceInput, SoundDeviceOutput  # noqa: E402
from littlebot.audio.service import AudioService  # noqa: E402
from littlebot.audio.stt import MockSTT  # noqa: E402
from littlebot.audio.tts import MockTTS  # noqa: E402
from littlebot.audio.wakeword import WakeWordService  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
from littlebot.core.arbiter import Resource  # noqa: E402
from littlebot.core.context import build_core_context  # noqa: E402
from littlebot.core.events import Topics, collect  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402
from littlebot.hardware.system import MockSystemMonitor  # noqa: E402
from littlebot.testing import make_test_config  # noqa: E402

pytestmark = pytest.mark.timeout(90)


def wait_until(pred: Callable[[], Any], timeout: float = 10.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or pred}")


class CheckedLiveAudio(FakeLiveAudio):
    """The live call's sound card. Like the USB devices' ALSA hw: it can only open once LittleBot's
    own speaker and microphone streams are closed."""

    def __init__(self, fake_sd: Any) -> None:
        super().__init__()
        self.sd = fake_sd
        self.conflicts: list[str] = []

    def open(self, loop: Any) -> None:
        if any(not s.closed for s in self.sd.out_streams):
            self.conflicts.append("speaker still open")
        if any(s.active for s in self.sd.in_streams):
            self.conflicts.append("microphone still open")
        super().open(loop)


@pytest.fixture
def bot(fake_sd, tmp_path):
    """The whole robot, started; ``bot.rig`` holds the parts the tests look at."""
    made: list[LittleBot] = []

    def _make() -> LittleBot:
        cfg = make_test_config(dashboard={"enabled": False},
                               wake={"cooldown_seconds": 0.3, "phrases": ["hey little bot", "hey littlebot",
                                                                          "hey lil bot", "hey bot", "lil bot",
                                                                          "little bot"]},
                               audio={"microphone": {"backend": "real"}, "speaker": {"backend": "sounddevice"}})
        ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
        ctx.privacy.set(cloud_enabled=True)
        ctx.audio = AudioService(ctx, tts=MockTTS(), stt=MockSTT())  # real backends on the fake PortAudio
        server = FakeOpenAI()
        call_audio = CheckedLiveAudio(fake_sd)
        ctx.live = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                                    audio_factory=lambda: call_audio, provider=FakeProvider())
        rec = FakeRecognizer()
        ctx.wake = WakeWordService(ctx, recognizer_factory=lambda _rate: rec)  # a "real" (fake PortAudio) mic
        robot = LittleBot(ctx)
        robot.build_subsystems()
        robot.rig = type("Rig", (), dict(server=server, call_audio=call_audio, rec=rec, sd=fake_sd))
        made.append(robot)
        robot.start()
        return robot

    yield _make
    for robot in made:
        robot.runtime.run(robot.rig.server.close(), timeout=10)
        robot.shutdown("test end")


def _listening(robot: LittleBot) -> bool:
    sd = robot.rig.sd
    return bool(robot.ctx.wake.status()["listening"] and sd.in_streams and sd.in_streams[-1].active)


def _earcon_heard(fake_sd: Any, since: int) -> bool:
    """Some of the speaker streams opened since ``since`` got real (non-silent) samples."""
    for s in fake_sd.out_streams[since:]:
        a = s.audio()
        if len(a) and float(np.abs(a).max()) > 0.01:
            return True
    return False


def _wake_and_talk(robot: LittleBot, phrase: str = "hey bot") -> list[Any]:
    """Say the wake phrase; check the earcon, the call, the voice and the hand-overs. Returns the events."""
    ctx, rig = robot.ctx, robot.rig
    wait_until(lambda: _listening(robot), what="wake word listening on the USB microphone")
    events = collect(ctx.bus, [Topics.AUDIO_SOUND_STARTED, Topics.AUDIO_SOUND_FINISHED, Topics.STATE_CHANGED,
                               "audio.wake_word"])
    out_before = len(rig.sd.out_streams)
    rig.rec.say(phrase)
    wait_until(lambda: ctx.wake.triggers == 1, what="wake phrase heard")
    # the earcon really played on the speaker, to the end
    wait_until(lambda: any(e.topic == Topics.AUDIO_SOUND_FINISHED and e.data["name"] == "listen" for e in events),
               what="earcon finished")
    done = [e.data for e in events if e.topic == Topics.AUDIO_SOUND_FINISHED and e.data["name"] == "listen"]
    assert done == [{"name": "listen", "interrupted": False}]
    assert _earcon_heard(rig.sd, out_before), "the earcon never reached the speaker"
    assert not any(e.data.get("name") == "uhoh" for e in events if e.topic == Topics.AUDIO_SOUND_STARTED)
    # live voice connects with the speaker and microphone handed over
    wait_until(lambda: ctx.live.status().get("phase") in ("listening", "speaking", "thinking"),
               timeout=20, what="live call connected")
    assert rig.call_audio.opened and rig.call_audio.conflicts == []
    assert ctx.audio.suspended == "live voice"
    assert not any(s.active for s in rig.sd.in_streams) and all(s.closed for s in rig.sd.out_streams)
    assert ctx.arbiter.holder(Resource.MICROPHONE).owner == "live_voice"
    assert ctx.state.state in (RobotState.LISTENING, RobotState.SPEAKING, RobotState.THINKING)
    assert not ctx.wake.status()["listening"]
    # the voice (the peer's tone) reaches the speaker
    wait_until(lambda: rig.call_audio.frames > 20, timeout=15, what="voice frames played")
    assert rig.call_audio.peak > 1000
    wait_until(lambda: (ctx.wake.status()["last_heard"] or {}).get("result") == "connected", what="wake: connected")
    return events


def _hang_up_and_listen_again(robot: LittleBot) -> None:
    ctx, rig = robot.ctx, robot.rig
    robot.runtime.run(ctx.live.stop_session("stopped by parent"), timeout=15)
    assert not ctx.live.status()["active"] and ctx.audio.suspended is None
    assert ctx.state.state == RobotState.IDLE
    wait_until(lambda: _listening(robot), what="wake word listening again after the call")
    assert rig.call_audio.closed


def test_wake_word_while_awake_with_a_person_present(bot, caplog):
    caplog.set_level(logging.INFO, logger="littlebot")
    robot = bot()
    ctx = robot.ctx
    seen = collect(ctx.bus, [Topics.VISION_PERSON_PRESENT])
    for _ in range(15):
        ctx.vision.simulate_face(0.5, 0.5)
        time.sleep(0.05)
    wait_until(lambda: seen, what="person present")
    assert ctx.state.state == RobotState.IDLE
    assert isinstance(ctx.audio.background_input(), SoundDeviceInput) and isinstance(ctx.audio._out, SoundDeviceOutput)
    events = _wake_and_talk(robot)
    states = [e.data["new"] for e in events if e.topic == Topics.STATE_CHANGED]
    assert states[0] == "LISTENING"
    log = caplog.text
    assert "wake word heard (phrase=hey bot)" in log
    assert "wake word: earcon 'listen' played" in log
    assert "wake word: live voice started" in log
    assert "wake word: live voice connected" in log
    _hang_up_and_listen_again(robot)
    assert "wake word: listening again" in caplog.text
    # and a second time, still awake
    robot.rig.rec.say("hey little bot")
    wait_until(lambda: robot.ctx.wake.triggers == 2 and ctx.live.status()["active"], what="second call")
    wait_until(lambda: ctx.live.status().get("phase") in ("listening", "speaking", "thinking"), timeout=20,
               what="second call connected")
    assert robot.rig.call_audio.conflicts == []


def test_wake_word_during_a_video_pauses_it_and_keep_watching_carries_on(bot, caplog, monkeypatch):
    import littlebot.chatgpt.live as live_mod

    caplog.set_level(logging.INFO, logger="littlebot")
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 0.6)  # the peer's tone never pauses for a goodbye
    robot = bot()
    ctx, rig = robot.ctx, robot.rig
    ctx.settings.set("profile", {"age_months": 30})
    item = ctx.media.add_item("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Bus Song")
    robot.runtime.run(ctx.media.play(item["id"]))
    assert ctx.state.state == RobotState.PLAYING_MEDIA
    wait_until(lambda: ctx.face.status()["state"] == "hidden", what="face hidden for the video")
    wait_until(lambda: _listening(robot), what="the wake word listens during the video")
    assert ctx.wake.status()["during_video"] is True
    rig.rec.say("hey little bot")
    wait_until(lambda: ctx.wake.triggers == 1, what="wake phrase heard")
    wait_until(lambda: ctx.live.status().get("phase") in ("listening", "speaking", "thinking"), timeout=20,
               what="live call connected")
    st = ctx.media.status()
    assert st["state"] == "paused" and st["held"]["reason"] == "wake word"
    assert ctx.face.status()["state"] != "hidden"  # the face is back
    assert "wake word: earcon 'listen' played" in caplog.text
    wait_until(lambda: any("is paused on LittleBot's screen" in m["content"][0]["text"] for m in rig.server.received
                           if m.get("type") == "session.context.append"), what="the voice was told")

    async def keep_watching() -> None:
        rig.server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "keep watching"}})

    robot.runtime.run(keep_watching())
    wait_until(lambda: not ctx.live.status()["active"], timeout=15, what="the call ended")
    wait_until(lambda: ctx.state.state == RobotState.PLAYING_MEDIA, timeout=10, what="the video carries on")
    assert ctx.media.status()["held"] is None and ctx.live.status()["last_call"]["handed_over"] == ["video resume"]
    assert rig.call_audio.conflicts == []
    wait_until(lambda: _listening(robot), what="listening again, during the video")
    robot.runtime.run(ctx.media.stop())


def test_said_in_one_breath_the_words_reach_the_voice_without_an_earcon(bot, caplog):
    """ "Hey LittleBot, what's the weather?": the words after the wake phrase (the fake USB microphone's tone)
    are the first thing the far end hears, and no earcon plays over them."""
    caplog.set_level(logging.INFO, logger="littlebot")
    robot = bot()
    ctx, rig = robot.ctx, robot.rig
    wait_until(lambda: _listening(robot), what="wake word listening on the USB microphone")
    wait_until(lambda: rig.rec.fed >= 2 * 16000, what="2 s heard")
    events = collect(ctx.bus, [Topics.AUDIO_SOUND_STARTED])
    rig.rec.say("hey little bot what is the weather", wake_ended_ago=1.0)
    wait_until(lambda: ctx.wake.triggers == 1, what="wake phrase heard")
    wait_until(lambda: ctx.live.status().get("phase") in ("listening", "speaking", "thinking"), timeout=20,
               what="live call connected")

    def tone() -> list[int]:
        return [i for i, f in enumerate(list(rig.server.heard)) if np.abs(f).max() > 1000]

    wait_until(lambda: len(tone()) >= 50, timeout=15, what="the carried words reached the voice")
    assert tone()[0] <= 3  # first
    assert not any(e.data.get("name") == "listen" for e in events)  # no earcon over their words
    log = caplog.text
    assert "wake word: no earcon, they kept talking" in log
    assert "wake word: kept " in log and "live voice: microphone on " in log and "after the wake phrase" in log
    assert rig.call_audio.conflicts == []  # the wake word let go of the microphone first
    _hang_up_and_listen_again(robot)


def test_wake_word_while_asleep(bot):
    robot = bot()
    ctx = robot.ctx
    assert robot.sleep()
    wait_until(lambda: ctx.face.status()["state"] == "sleeping", what="asleep")
    events = _wake_and_talk(robot, "hey little bot")
    states = [e.data["new"] for e in events if e.topic == Topics.STATE_CHANGED]
    assert states[0] == "LISTENING"  # SLEEPING -> LISTENING: the eyes open for the call
    _hang_up_and_listen_again(robot)
