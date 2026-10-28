"""A hand-off answered by Claude (text.provider: claude) still goes through the live voice's safety checks."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402
from test_live import LendingAudio, answers, connect, delegate, wait_for  # noqa: E402

from littlebot.brain import text_model  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
from littlebot.testing import FakeAnthropic, make_test_context  # noqa: E402

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def claude_live(tmp_path, monkeypatch):
    ctx = make_test_context(text={"provider": "claude"}, claude={"model": "claude-haiku-5-5"})
    ctx.config.anthropic_api_key = SecretStr("sk-ant-test-0123456789abcdef")
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = LendingAudio()
    client = FakeAnthropic()
    monkeypatch.setattr(text_model, "client_factory", lambda: client)
    server = FakeOpenAI()
    chatgpt = FakeProvider()
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=FakeLiveAudio, provider=text_model.text_provider(ctx, chatgpt,
                                                                                           purpose="handoff"))
    await svc.start()
    yield svc, server, client, chatgpt
    await svc.stop()
    await server.close()
    ctx.db.close()


async def test_a_dose_from_claude_becomes_check_the_label(claude_live):
    from littlebot.safety.content import DOSE_FALLBACK

    svc, server, client, chatgpt = claude_live
    client.turns = [{"text": "For a 10 month old, give 2.5 ml of infant paracetamol."}]
    await connect(svc, server)
    delegate(server, "m1", "How much paracetamol can I give my baby?")
    await wait_for(lambda: answers(server, "m1"))
    assert answers(server, "m1") == [DOSE_FALLBACK]
    assert client.calls and not chatgpt.calls  # Claude answered; the check happened afterwards anyway


async def test_claude_failing_mid_call_lets_chatgpt_answer_the_hand_off(claude_live):
    class Overloaded(Exception):
        status_code = 529

    svc, server, client, chatgpt = claude_live
    client.turns = [{"error": Overloaded()}]
    chatgpt.text = "The Eiffel Tower is about 330 metres tall."
    await connect(svc, server)
    delegate(server, "e1", "How tall is the Eiffel Tower in Paris, roughly, compared to Big Ben?")
    await wait_for(lambda: answers(server, "e1"))
    assert answers(server, "e1") == [chatgpt.text]
