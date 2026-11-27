"""Skill take-overs (littlebot/skills/takeover.py): claimed by a host, or started after LittleBot has spoken."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.state import RobotState
from littlebot.skills import takeover
from littlebot.testing import make_test_context


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(takeover, "QUIET_S", 0.03)
    monkeypatch.setattr(takeover, "POLL_S", 0.005)
    monkeypatch.setattr(takeover, "MAX_WAIT_S", 2.0)


@pytest.fixture
def ctx():
    c = make_test_context()
    yield c
    c.db.close()


def starter(log: list[str], name: str, result: Any = True, error: Exception | None = None):
    async def run() -> Any:
        if error is not None:
            raise error
        log.append(name)
        return result

    return run


async def wait_for(cond, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


async def test_a_host_claims_it_and_it_never_starts_by_itself(ctx):
    ran: list[str] = []
    takeover.offer(ctx, "music abc", starter(ran, "abc"))
    assert takeover.pending(ctx) == ["music abc"]
    claimed = takeover.claim(ctx)
    assert [t.label for t in claimed] == ["music abc"] and claimed[0].takeover is True
    assert takeover.pending(ctx) == [] and takeover.claim(ctx) == []
    await asyncio.sleep(0.1)
    assert ran == []
    assert await claimed[0].run() is True and ran == ["abc"]  # the host runs it


async def test_unclaimed_it_starts_once_littlebot_is_quiet(ctx):
    ran: list[str] = []
    ctx.state.transition(RobotState.THINKING, "test")
    takeover.offer(ctx, "music abc", starter(ran, "abc"))
    await asyncio.sleep(0.08)
    assert ran == []  # still answering
    ctx.state.transition(RobotState.SPEAKING, "test")
    await asyncio.sleep(0.08)
    assert ran == []
    ctx.state.transition(RobotState.IDLE, "test")
    await wait_for(lambda: ran == ["abc"])
    assert takeover.pending(ctx) == []


async def test_the_newest_wins_and_drop_forgets(ctx):
    ran: list[str] = []
    ctx.state.transition(RobotState.SPEAKING, "test")
    takeover.offer(ctx, "music one", starter(ran, "one"))
    takeover.offer(ctx, "music two", starter(ran, "two"))
    assert takeover.pending(ctx) == ["music two"]
    ctx.state.transition(RobotState.IDLE, "test")
    await wait_for(lambda: ran == ["two"])
    await asyncio.sleep(0.05)
    assert ran == ["two"]
    ctx.state.transition(RobotState.SPEAKING, "test")
    takeover.offer(ctx, "music three", starter(ran, "three"))
    assert takeover.drop(ctx, "music ") is True and takeover.drop(ctx, "music ") is False
    ctx.state.transition(RobotState.IDLE, "test")
    await asyncio.sleep(0.1)
    assert ran == ["two"]


async def test_it_gives_up_waiting_after_max_wait(ctx, monkeypatch):
    monkeypatch.setattr(takeover, "MAX_WAIT_S", 0.1)
    ran: list[str] = []
    ctx.state.transition(RobotState.THINKING, "test")  # never finishes
    takeover.offer(ctx, "music x", starter(ran, "x"))
    await wait_for(lambda: ran == ["x"], timeout=1.0)


class FakeLive:
    def __init__(self, lease: Any) -> None:
        self.lease = lease
        self.stopped: list[str] = []

    async def stop_session(self, reason: str = "stopped by parent") -> dict[str, Any]:
        self.stopped.append(reason)
        self.lease.release()
        return {"ok": True}


async def test_a_voice_chat_holding_the_speaker_is_ended_after_it_spoke(ctx):
    lease = ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE],
                                Priority.PARENT)
    ctx.live = FakeLive(lease)
    backs: list[int] = []

    async def speaker_back() -> bool:
        backs.append(1)
        return True

    ctx.audio.speaker_back = speaker_back
    assert takeover.voice_chat(ctx) == "live_voice"
    ran: list[str] = []
    ctx.state.transition(RobotState.LISTENING, "test")
    takeover.offer(ctx, "music abc", starter(ran, "abc"))
    await asyncio.sleep(0.1)
    assert ran == [] and ctx.live.stopped == []  # quiet, but the voice hasn't said its line yet
    ctx.state.transition(RobotState.SPEAKING, "test")
    await asyncio.sleep(0.03)
    ctx.state.transition(RobotState.LISTENING, "test")
    await wait_for(lambda: ran == ["abc"])
    assert ctx.live.stopped == [takeover.HANDOVER_REASON]
    assert takeover.voice_chat(ctx) is None and backs == [1]


async def test_a_conversation_below_media_is_not_ended(ctx):
    ctx.arbiter.acquire("conversation", [Resource.AUDIO_OUT], Priority.CONVERSATION)
    assert takeover.voice_chat(ctx) is None
    assert await takeover.end_voice_chat(ctx) is False


async def test_failures_are_reported_and_never_raise(ctx):
    why: list[str] = []
    takeover.offer(ctx, "music a", starter([], "a", error=PermissionError("Videos are turned off.")),
                   on_fail=why.append)
    await wait_for(lambda: why == ["Videos are turned off."])
    takeover.offer(ctx, "music b", starter([], "b", result=False), on_fail=why.append)
    await wait_for(lambda: len(why) == 2)
    assert why[1] == "it couldn't start right now"


async def test_wait_until_said_with_a_clock():
    ctx = make_test_context()
    try:
        assert await takeover.wait_until_said(ctx, max_wait=1.0, quiet=0.01, poll=0.002) is True
        ctx.state.transition(RobotState.SPEAKING, "test")
        assert await takeover.wait_until_said(ctx, max_wait=0.03, quiet=0.01, poll=0.002) is False
    finally:
        ctx.db.close()


def test_offer_without_a_loop_only_queues():
    ctx = make_test_context()
    try:
        t = takeover.offer(ctx, "music z", starter([], "z"))
        assert t.fallback is None and takeover.pending(ctx) == ["music z"]
    finally:
        ctx.db.close()
