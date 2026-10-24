"""Hand-over from a voice chat to an activity on the whole robot: the activity's first line plays in full.

The real composition (LittleBot.build_subsystems): the real AudioService on a fake PortAudio (USB
speaker), the real activity engine and the real LiveVoiceService calling a local WebRTC peer that
stands in for OpenAI. The voice asks the robot's tools to start "Again! Again!"; the call ends and
the activity starts. Like a USB speaker that the call's own stream has only just let go of, the
fake speaker fails a sound started in the first moments after the call closed its stream: the
sound plays for a moment and then stops (and the speaker is then taken for unplugged). The
hand-over waits until the speaker plays again, so the activity's first line is heard in full.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

pytest.importorskip("aiortc")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, signed_in_auth  # noqa: E402

import littlebot.chatgpt.live as live_mod  # noqa: E402
from littlebot.app.robot import LittleBot  # noqa: E402
from littlebot.audio.devices import STATUS_OK  # noqa: E402
from littlebot.audio.service import AudioService  # noqa: E402
from littlebot.audio.stt import MockSTT  # noqa: E402
from littlebot.audio.tts import MockTTS  # noqa: E402
from littlebot.brain.providers.base import BaseProvider, ProviderReply  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
from littlebot.core.context import build_core_context  # noqa: E402
from littlebot.core.events import Topics, collect  # noqa: E402
from littlebot.hardware.system import MockSystemMonitor  # noqa: E402
from littlebot.testing import make_test_config  # noqa: E402

pytestmark = pytest.mark.timeout(90)

FIRST_LINE = "Tap again, and I'll do my trick!"  # "Again! Again!" starts with this


class StartsAnActivity(BaseProvider):
    """The text model behind the voice's hand-offs: starts the activity it was asked for."""

    name = "chatgpt"
    is_cloud = True
    model = "fake"
    supports_tools = True

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: Any = None, run_tool: Any = None, max_tool_rounds: int = 6, images: Any = None
                    ) -> ProviderReply:
        await run_tool("start_activity", json.dumps({"activity": "again_again"}))
        return ProviderReply(text="Let's play Again Again!", model="fake")


class LettingGoLiveAudio(FakeLiveAudio):
    """The call's sound card. Closing it lets go of the USB speaker, which takes ``settle`` seconds:
    meanwhile a sound starts but fails after its first block ("played for a moment, then stopped")."""

    def __init__(self, fake_sd: Any, settle: float = 0.5) -> None:
        super().__init__()
        self.sd, self.settle = fake_sd, settle

    def close(self) -> None:
        super().close()
        self.sd.fail_write_after = 2048  # the lead-in and one block of a new sound get through, then it fails
        timer = threading.Timer(self.settle, lambda: setattr(self.sd, "fail_write_after", None))
        timer.daemon = True
        timer.start()


def wait_until(pred: Callable[[], Any], timeout: float = 15.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or pred}")


@pytest.fixture
def robot(fake_sd, tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 0.3)  # the fake voice never "speaks": end the call soon
    cfg = make_test_config(dashboard={"enabled": False}, data_dir=str(tmp_path),
                           audio={"speaker": {"backend": "sounddevice"}})
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = AudioService(ctx, tts=MockTTS(), stt=MockSTT())  # the real speaker backend on the fake PortAudio
    server = FakeOpenAI()
    ctx.live = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                                audio_factory=lambda: LettingGoLiveAudio(fake_sd), provider=StartsAnActivity())
    bot = LittleBot(ctx)
    bot.build_subsystems()
    bot.start()
    bot.rig = type("Rig", (), dict(server=server, sd=fake_sd))
    yield bot
    bot.runtime.run(server.close(), timeout=10)
    bot.shutdown("test end")


def test_an_activity_started_from_a_voice_chat_says_its_first_line_in_full(robot):
    ctx, server, sd = robot.ctx, robot.rig.server, robot.rig.sd
    events = collect(ctx.bus, [Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED, Topics.ACTIVITY_STARTED,
                               Topics.ACTIVITY_FINISHED])
    assert robot.runtime.run(ctx.live.start_session(parent_present=True), timeout=10)["ok"]
    wait_until(lambda: server.channel_ready.is_set() and ctx.live.status().get("phase") == "listening",
               what="call connected")
    assert ctx.audio.suspended == "live voice"

    async def ask() -> None:
        server.send({"type": "delegation.created", "item": {
            "id": "d1", "type": "delegation", "target": "client",
            "content": [{"type": "input_text", "text": "Start the Again Again activity"}]}})

    robot.runtime.run(ask(), timeout=5)
    wait_until(lambda: any(e.topic == Topics.ACTIVITY_FINISHED for e in events), what="the activity ran")
    assert ctx.live.status()["last_call"]["handed_over"] == ["activity again_again"]
    assert ctx.audio.suspended is None
    first = next(e.data for e in events if e.topic == Topics.AUDIO_SPEECH_FINISHED)
    assert first == {"text": FIRST_LINE, "interrupted": False}  # was cut off right after it began
    assert ctx.audio._out.status == STATUS_OK  # and not taken for unplugged (silence for 5 s)
    # the whole line reached the speaker, after the call had let go of it
    lines = [s for s in sd.out_streams if len(s.audio()) > 0.5 * s.samplerate]
    assert lines, "the first line never reached the speaker"
    finished = [e.data for e in events if e.topic == Topics.AUDIO_SPEECH_FINISHED]
    assert all(not f["interrupted"] for f in finished), finished
