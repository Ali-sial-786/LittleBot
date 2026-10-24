"""Live voice and the battery: a low battery is a quiet note in the call, an empty one a goodbye that ends it, and the
voice hears about the battery only when a monitor is fitted (a local WebRTC peer stands in for OpenAI)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402

from littlebot.chatgpt.live import CALL_ENDED, LiveVoiceService  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.power.monitor import cell_for_percent  # noqa: E402
from littlebot.power.protocol import Reading  # noqa: E402
from littlebot.power.service import PowerService  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class LendingAudio:
    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        pass

    def resume(self, owner: str) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


@pytest.fixture
def make_live(ctx, tmp_path):
    async def _make() -> tuple[LiveVoiceService, FakeOpenAI]:
        ctx.privacy.set(cloud_enabled=True)
        ctx.audio = LendingAudio()
        server, audio = FakeOpenAI(), FakeLiveAudio()
        svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                               audio_factory=lambda: audio, provider=FakeProvider())
        ctx.live = svc
        await svc.start()
        result = await svc.start_session(parent_present=True)
        assert result["ok"], result
        await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")
        return svc, server

    return _make


async def close(svc: LiveVoiceService, server: FakeOpenAI) -> None:
    await svc.stop()
    await server.close()


def fitted_power(ctx: Any) -> PowerService:
    power = PowerService(ctx)
    c = cell_for_percent(55)
    power.process(Reading(voltage=round(2 * c, 3), cell1=round(c, 3), temperature_c=25.0, charging=False,
                          charged=False, uptime_s=3, fw=1))
    ctx.power = power
    return power


def instructions(server: FakeOpenAI) -> str:
    return server.requests[0]["body"]["session"]["instructions"]


async def test_on_mains_power_the_voice_hears_nothing_about_a_battery(make_live, ctx):
    svc, server = await make_live()
    try:
        assert "battery" not in instructions(server).lower()
    finally:
        await close(svc, server)


async def test_with_a_battery_fitted_the_voice_knows_it_can_ask(make_live, ctx):
    fitted_power(ctx)
    svc, server = await make_live()
    try:
        assert "battery" in instructions(server).lower()
    finally:
        await close(svc, server)


async def test_a_low_battery_is_a_quiet_note_and_the_chat_goes_on(make_live, ctx):
    svc, server = await make_live()
    try:
        ctx.bus.publish(Topics.POWER_NOTE, {"text": "The battery is at 18%: mention it gently.", "goodbye": False},
                        source="power")
        await wait_for(lambda: "The battery is at 18%: mention it gently." in notes(server))
        await asyncio.sleep(0.3)
        assert svc.status().get("active") is True
    finally:
        await close(svc, server)


async def test_an_empty_battery_says_goodbye_and_ends_the_call(make_live, ctx):
    ended: list[dict[str, Any]] = []
    ctx.bus.subscribe(CALL_ENDED, lambda e: ended.append(dict(e.data)))
    svc, server = await make_live()
    try:
        ctx.bus.publish(Topics.POWER_NOTE, {"text": "The battery is empty. Say goodbye now.", "goodbye": True},
                        source="power")
        await wait_for(lambda: "The battery is empty. Say goodbye now." in notes(server))
        await wait_for(lambda: ended, timeout=20)
        assert ended[0]["reason"] == "battery"
    finally:
        await close(svc, server)
