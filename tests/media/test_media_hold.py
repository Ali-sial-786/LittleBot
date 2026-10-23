"""MediaController.hold: a video paused for a chat ("Hey LittleBot" while it plays) keeps its position,
gives the screen back to the face, carries on with resume() and stops a while after the chat."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from media_testkit import LIST_A, VID_A, VID_B, VID_C  # type: ignore[import-not-found]

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.media.controller import HELD_DETAIL, RESUME_REWIND_SECONDS


class FakeLive:
    """``ctx.live`` as the media controller sees it: only whether a call is on."""

    def __init__(self) -> None:
        self.active = False

    def status(self) -> dict[str, Any]:
        return {"active": self.active}


async def _playing(env, vid: str = VID_A, at: float = 0.0, **item: Any) -> dict[str, Any]:
    it = env.add(vid, **item)
    await env.media.play(it["id"])
    await env.page_event("playing", video_id=vid, player_state=1, current_time=at)
    return it


async def _hold_with_page(env, paused_at: float, *, delay: float = 0.05) -> bool:
    """hold() while the page answers the pause command like player.js: a "paused" report with the position."""

    async def page() -> None:
        while env.poll()["action"] != "pause":
            await asyncio.sleep(0.01)
        await asyncio.sleep(delay)
        await env.page_event("paused", video_id=VID_A, player_state=2, current_time=paused_at)

    env.poll()  # the page polls twice a second, so it is connected
    task = asyncio.ensure_future(asyncio.wait_for(page(), 3.0))
    ok = await env.media.hold("wake word", wait=2.0)
    await task
    return ok


# ------------------------------------------------------------------ hold
async def test_hold_keeps_the_position_and_gives_the_screen_back(env):
    item = await _playing(env, at=10.0)
    env.clock.advance(20)
    assert await _hold_with_page(env, 61.5)
    st = env.media.status()
    assert st["state"] == "paused" and st["item"]["id"] == item["id"]
    held = st["held"]
    assert held["position_seconds"] == 61.5 and held["reason"] == "wake word"
    assert held["chat_active"] is False and held["stops_in_seconds"] == pytest.approx(120)
    # the face is back: no media lease, RobotState IDLE, the player window closed, the page told to stop
    assert env.ctx.state.state == RobotState.IDLE and env.face_states() == ["hidden", "idle"]
    assert all(holder is None for holder in env.ctx.arbiter.snapshot().values())
    await env.settle()
    assert env.backend.closed >= 1 and env.backend.running is False
    assert env.poll()["action"] == "stop" and env.poll()["reason"] == "paused"
    paused = env.last(Topics.MEDIA_PAUSED).data
    assert paused == {"item_id": item["id"], "held": True, "reason": "wake word", "position_seconds": 61.5}
    assert env.last(Topics.MEDIA_STOPPED) is None
    # no screen time while it waits
    used = st["daily_used_seconds"]
    env.clock.advance(60)
    assert env.media.status()["daily_used_seconds"] == pytest.approx(used)
    assert await env.media.hold("again") is True  # already held: nothing changes
    assert await env.media.pause() is False


async def test_without_a_paused_report_the_position_is_estimated(env):
    await _playing(env, at=30.0)
    env.clock.advance(3)  # the page reported 30 s three seconds ago, while playing
    assert await env.media.hold("wake word", wait=0.1)
    assert env.media.status()["held"]["position_seconds"] == pytest.approx(33.0)
    assert env.media.status()["state"] == "paused"


async def test_hold_without_a_page_starts_from_the_items_start(env):
    item = env.add()
    await env.media.play(item["id"])
    assert await env.media.hold("wake word", wait=0.0)
    assert env.media.status()["held"]["position_seconds"] == 0.0
    assert await env.media.hold("x") is True


async def test_nothing_to_hold(env):
    assert await env.media.hold("wake word", wait=0.0) is False
    assert env.media.status()["held"] is None


# ------------------------------------------------------------------ resume
async def test_resume_opens_the_player_where_it_was(env):
    item = await _playing(env, at=5.0)
    plays = env.media.get_item(item["id"])["play_count"]
    assert await _hold_with_page(env, 61.5)
    await env.settle()
    opened = len(env.backend.opened)
    old_load = env.poll()["load_id"]
    assert await env.media.resume() is True
    st = env.media.status()
    assert st["state"] == "loading" and st["held"] is None
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA
    assert env.ctx.arbiter.holder(Resource.SCREEN).owner == "media"
    assert len(env.backend.opened) == opened + 1  # the kiosk is started again
    desired = env.poll()
    assert desired["action"] == "play" and desired["load_id"] != old_load
    assert desired["item"]["video_id"] == VID_A
    assert desired["item"]["start_seconds"] == int(61.5 - RESUME_REWIND_SECONDS)  # a moment earlier
    assert env.media.get_item(item["id"])["play_count"] == plays  # carrying on is not a new play
    resumed = env.last(Topics.MEDIA_RESUMED).data
    assert resumed["held"] is True and resumed["position_seconds"] == 59
    # the new load plays and counts screen time again
    await env.page_event("playing", video_id=VID_A, player_state=1, current_time=59.0)
    env.clock.advance(10)
    assert env.media.status()["state"] == "playing"
    assert env.media.status()["session_seconds"] == pytest.approx(10, abs=0.5)


async def test_resume_waits_while_live_voice_has_the_face(env):
    await _playing(env)
    assert await env.media.hold("wake word", wait=0.0)
    live = env.ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE],
                                   Priority.PARENT)
    env.ctx.state.transition(RobotState.LISTENING, "live voice")
    assert await env.media.resume() is False  # the chat is still on
    assert env.media.status()["held"] is not None and env.ctx.state.state == RobotState.LISTENING
    live.release()
    env.ctx.state.release(RobotState.LISTENING, "live voice ended")
    assert await env.media.resume() is True
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA


async def test_resume_after_the_time_ran_out_stops_it(env):
    env.ctx.settings.set("media.daily_limit_minutes", 1)
    await _playing(env)
    env.clock.advance(50)
    assert await env.media.hold("wake word", wait=0.0)
    env.ctx.settings.set("media.enabled", False)  # e.g. a parent switched videos off meanwhile
    await env.settle()
    assert env.media.status()["state"] == "idle"
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "stopped"


async def test_a_playlist_carries_on_at_the_video_that_was_playing(env):
    pl = env.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    await env.media.play(pl["id"])
    await env.page_event("ready", video_id=VID_A, playlist_ids=[VID_A, VID_B])
    await env.page_event("playing", video_id=VID_B, player_state=1, current_time=12.0)
    assert await env.media.hold("wake word", wait=0.0)
    assert await env.media.resume()
    item = env.poll()["item"]
    assert item["list_id"] == LIST_A and item["video_id"] == VID_B and item["start_seconds"] == 10


async def test_skip_or_a_new_video_while_held_takes_the_screen_back(env):
    a = await _playing(env)
    b = env.add(VID_B, "Other")
    assert await env.media.hold("wake word", wait=0.0)
    st = await env.media.skip()
    assert st["state"] == "loading" and st["item"]["id"] == b["id"] and st["held"] is None
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA
    assert await env.media.hold("wake word", wait=0.0)
    await env.media.play(a["id"])  # e.g. the voice was asked for another video
    assert env.media.status()["held"] is None and env.ctx.state.state == RobotState.PLAYING_MEDIA
    assert env.poll()["item"]["start_seconds"] == 0


# ------------------------------------------------------------------ stop / the countdown after the chat
async def test_stop_while_held(env):
    item = await _playing(env)
    assert await env.media.hold("wake word", wait=0.0)
    await env.media.stop()
    assert env.media.status()["state"] == "idle" and env.media.status()["held"] is None
    stopped = env.last(Topics.MEDIA_STOPPED).data
    assert stopped["item_id"] == item["id"] and stopped["reason"] == "stopped"
    assert env.ctx.state.state == RobotState.IDLE


async def test_stop_all_while_held(env):
    await _playing(env)
    assert await env.media.hold("wake word", wait=0.0)
    env.ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and env.media.status()["state"] == "idle"


async def test_a_held_video_stops_a_while_after_the_chat(env):
    env.ctx.live = live = FakeLive()
    await _playing(env)
    assert await env.media.hold("wake word", wait=0.0)
    live.active = True  # the chat
    env.clock.advance(9 * 60)  # longer than max_pause_minutes would allow for a parent's pause, too
    await env.media.tick()
    st = env.media.status()
    assert st["held"]["chat_active"] is True and st["held"]["stops_in_seconds"] is None
    live.active = False  # the chat ended
    env.clock.advance(119)
    await env.media.tick()
    assert env.media.status()["held"]["stops_in_seconds"] == pytest.approx(1)
    assert env.media.status()["state"] == "paused"
    env.clock.advance(2)
    await env.media.tick()
    assert env.media.status()["state"] == "idle"
    stopped = env.last(Topics.MEDIA_STOPPED).data
    assert stopped["reason"] == "stopped" and stopped["detail"] == HELD_DETAIL


async def test_chat_pause_minutes_is_configurable(make_env):
    e = make_env(media_cfg={"chat_pause_minutes": 0.5})
    await e.media.start()
    try:
        await _playing(e)
        assert await e.media.hold("wake word", wait=0.0)
        e.clock.advance(29)
        await e.media.tick()
        assert e.media.status()["state"] == "paused"
        e.clock.advance(2)
        await e.media.tick()
        assert e.media.status()["state"] == "idle"
    finally:
        await e.media.close()


async def test_while_held_the_closing_page_and_the_player_checks_do_not_count(make_env):
    env = make_env(media_cfg={"heartbeat_timeout_seconds": 5, "connect_timeout_seconds": 5})
    await env.media.start()
    try:
        await _playing(env)
        assert await env.media.hold("wake word", wait=0.0)
        await env.settle()
        # the kiosk closing: its page says it was hidden / navigated away; no heartbeats any more
        assert (await env.page_event("navigated_away"))["current"] is False
        env.clock.advance(60)
        await env.media.tick()
        assert env.media.status()["state"] == "paused" and env.media.last_error is None
        assert env.last(Topics.MEDIA_ERROR) is None
    finally:
        await env.media.close()


# ------------------------------------------------------------------ duck (the wake word heard "hey ...")
async def test_duck_turns_the_video_down_for_a_moment(env):
    await _playing(env)
    env.ctx.settings.set("media.volume", 0.6)
    assert env.poll()["volume"] == 0.6
    assert env.media.duck(0.25, 0.15) is True
    assert env.poll()["volume"] == pytest.approx(0.15)
    assert env.ctx.settings.get("media.volume") == 0.6  # nothing saved
    await asyncio.sleep(0.3)
    assert env.poll()["volume"] == 0.6
    # a hold ends a duck at once
    assert env.media.duck(0.25, 5.0)
    await env.settle()
    assert await env.media.hold("wake word", wait=0.0)
    assert env.poll()["volume"] == 0.6
    assert env.media.duck(0.25, 1.0) is False  # nothing playing (held)


async def test_duck_without_a_video(env):
    assert env.media.duck(0.3, 1.0) is False


async def test_status_while_held_is_what_the_dashboard_shows(env):
    env.ctx.live = live = FakeLive()
    await _playing(env, vid=VID_C, title="Bus Song")
    assert await env.media.hold("wake word", wait=0.0)
    live.active = True
    st = env.media.status()
    assert st["state"] == "paused" and st["item"]["title"] == "Bus Song"
    assert set(st["held"]) == {"reason", "position_seconds", "since_seconds", "chat_active", "stops_in_seconds"}
    assert st["held"]["chat_active"] is True
