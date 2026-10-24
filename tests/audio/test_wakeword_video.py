"""The wake word while a video plays: it keeps listening with stricter rules, and "Hey LittleBot" pauses the
video (position kept, face back) before the earcon and live voice. The real AudioService (mock devices), the
real MediaController with a fake player window, a fake recogniser and a fake live voice."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media"))
from media_testkit import VID_A, FakeBackend  # type: ignore[import-not-found]  # noqa: E402
from test_wakeword import _listening, _not_listening, _sounds, rig, until  # noqa: E402,F401

from littlebot.core.events import Topics, collect  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402
from littlebot.media.controller import MediaController  # noqa: E402


async def _video(r: Any, *, title: str = "Bus Song") -> MediaController:
    """A video playing on the rig's robot (the player page is simulated by the test where needed)."""
    r.ctx.settings.set("profile", {"age_months": 30})
    r.backend = FakeBackend()
    media = MediaController(r.ctx, backend=r.backend, tick_seconds=3600)
    r.ctx.media = media
    await media.start()
    item = media.add_item(f"https://www.youtube.com/watch?v={VID_A}", title)
    await media.play(item["id"])
    state = media.player_state()
    await media.handle_player_event({"type": "playing", "load_id": state["load_id"], "video_id": VID_A,
                                     "player_state": 1, "current_time": 20.0})
    assert r.ctx.state.state == RobotState.PLAYING_MEDIA
    r.media = media
    return media


@pytest.fixture
async def vrig(rig):
    made: list[Any] = []

    async def _make(**kw: Any) -> Any:
        r = await rig(**kw)
        media = await _video(r)
        made.append(media)
        return r

    yield _make
    for media in made:
        await media.close()


def _spy_earcon(r: Any) -> list[tuple[str, bool, RobotState, float]]:
    """(sound, video held?, robot state, when) for each sound LittleBot plays."""
    seen: list[tuple[str, bool, RobotState, float]] = []
    real = r.audio.play_sound

    async def play_sound(name: str, **kw: Any) -> bool:
        seen.append((name, r.media.status()["held"] is not None, r.ctx.state.state, time.monotonic()))
        return await real(name, **kw)

    r.audio.play_sound = play_sound
    return seen


# ------------------------------------------------------------------ listening during a video
async def test_a_video_keeps_the_wake_word_listening_with_stricter_rules(vrig, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await vrig()
    await _listening(r)
    st = r.wake.status()
    assert st["during_video"] is True and st["video_phrases"] == ["hey little bot"]
    assert st["detail"] == "Listening for “hey little bot” (a video is playing)."
    r.rec.say("hey bot")  # "hey, but ..." / "hey buddy" in videos: not while a video plays
    r.rec.say("hey little bot", conf=0.6)  # below wake.video_min_confidence (0.7)
    await until(lambda: r.wake.video_ignored == 2, what="both ignored")
    await asyncio.sleep(0.2)
    assert r.live.calls == [] and r.wake.triggers == 0 and r.ctx.state.state == RobotState.PLAYING_MEDIA
    assert "wake word: 'hey bot' ignored while a video plays (only 'hey little bot' counts then)" in caplog.text
    assert "'hey little bot' ignored while a video plays (word confidence below wake.video_min_confidence 0.7)" \
        in caplog.text
    assert r.wake.status()["video_ignored"] == 2


async def test_hey_littlebot_pauses_the_video_then_the_earcon_then_live_voice(vrig):
    r = await vrig()
    await _listening(r)
    sounds = _spy_earcon(r)
    events = collect(r.ctx.bus, ["media.*", "state.changed"])
    r.rec.say("hey little bot", conf=0.9)
    await until(lambda: r.live.calls, what="live voice started")
    assert r.live.calls == [True]
    # the video was paused before the earcon, with the face back (IDLE -> LISTENING)
    assert sounds[0][:3] == ("listen", True, RobotState.LISTENING)
    st = r.media.status()
    assert st["state"] == "paused" and st["held"]["reason"] == "wake word"
    assert st["held"]["position_seconds"] >= 20.0
    await until(lambda: r.backend.closed >= 1, what="the player window closed")
    assert [e.data.get("new") for e in events if e.topic == Topics.STATE_CHANGED][:2] == ["IDLE", "LISTENING"]
    assert any(e.topic == Topics.MEDIA_PAUSED and e.data.get("held") for e in events)
    assert r.wake.status()["last_heard"]["video"] == "Bus Song"
    await _not_listening(r)  # live voice has the microphone now


async def test_the_earcon_waits_for_the_page_to_say_it_paused(vrig):
    r = await vrig(wake={"video_pause_wait_seconds": 2.0})
    media = r.media
    paused_at: list[float] = []

    async def page() -> None:  # like player.js: polls, pauses, reports the position
        while True:
            st = media.player_state()
            if st["action"] == "pause":
                await asyncio.sleep(0.3)  # YouTube takes a moment
                paused_at.append(time.monotonic())
                await media.handle_player_event({"type": "paused", "load_id": st["load_id"], "video_id": VID_A,
                                                 "player_state": 2, "current_time": 42.5})
                return
            await asyncio.sleep(0.05)

    task = asyncio.ensure_future(page())
    await _listening(r)
    sounds = _spy_earcon(r)
    r.rec.say("hey little bot")
    await until(lambda: r.live.calls, what="live voice started")
    await task
    assert sounds and sounds[0][0] == "listen" and sounds[0][3] >= paused_at[0]
    assert media.status()["held"]["position_seconds"] == 42.5


async def test_the_video_carries_on_when_live_voice_cannot_start(vrig):
    r = await vrig(error="not_signed_in")
    await _listening(r)
    r.rec.say("hey little bot")
    await until(lambda: "sound:uhoh" in _sounds(r.out), what="uhoh")
    await until(lambda: r.media.status()["state"] == "loading", what="the video carries on")
    assert r.media.status()["held"] is None and r.ctx.state.state == RobotState.PLAYING_MEDIA
    assert len(r.backend.opened) == 2  # the player window opened again
    assert r.media.player_state()["item"]["start_seconds"] >= 18


async def test_the_video_carries_on_when_nobody_says_anything(vrig):
    r = await vrig(wake={"video_no_words_seconds": 0.3})
    r.live.heard_user = False  # most likely the wake word heard the video itself
    await _listening(r)
    r.rec.say("hey little bot")
    await until(lambda: r.live.stopped, what="the call ended")
    assert r.live.stopped == ["nobody spoke (the wake word may have heard the video)"]
    await until(lambda: r.media.status()["state"] == "loading", what="the video carries on")
    assert r.ctx.state.state == RobotState.PLAYING_MEDIA


async def test_after_a_real_chat_the_video_waits_and_keep_watching_carries_on(vrig):
    r = await vrig()
    await _listening(r)
    r.rec.say("hey little bot")
    await until(lambda: r.live.active and r.live.phase == "listening", what="live voice connected")
    await r.live.end()  # they talked, and didn't say what to do with the video
    r.ctx.state.release(RobotState.LISTENING, "live voice ended")  # (the real live voice does this itself)
    await asyncio.sleep(0.6)
    st = r.media.status()
    assert st["state"] == "paused" and st["held"]["chat_active"] is False and st["held"]["stops_in_seconds"] > 100
    assert r.ctx.state.state == RobotState.IDLE  # the face is showing
    await _listening(r)  # and the wake word listens with the normal rules (no video plays)
    assert r.wake.status()["during_video"] is False
    # a second chat where they ask to keep watching (even if it ends before the voice's goodbye)
    r.rec.say("hey bot")
    await until(lambda: len(r.live.calls) == 2 and r.live.active, what="second call")
    r.live.video = "resume"
    await r.live.end()
    await until(lambda: r.media.status()["state"] == "loading", what="the video carries on")


async def test_without_a_video_nothing_changes(rig):
    r = await rig()
    await _listening(r)
    r.rec.say("hey bot")
    await until(lambda: r.live.calls)
    assert r.wake.status()["last_heard"].get("video") is None


# ------------------------------------------------------------------ turning the video down after "hey"
async def test_duck_after_hey_during_a_video(vrig):
    r = await vrig(wake={"video_duck": True, "video_duck_seconds": 0.4, "video_duck_volume": 0.25})
    await _listening(r)
    volume = r.media.player_state()["volume"]
    r.rec.partial_text = "hey"
    await until(lambda: r.wake.ducks == 1, what="ducked")
    assert r.media.player_state()["volume"] == pytest.approx(volume * 0.25)
    await asyncio.sleep(0.2)
    assert r.wake.ducks == 1  # the same words: once
    await until(lambda: r.media.player_state()["volume"] == volume, timeout=2.0, what="volume back")
    r.rec.partial_text = "hey little"
    await asyncio.sleep(0.3)
    assert r.wake.ducks == 1  # only the first word of a video phrase ducks


async def test_no_duck_by_default(vrig):
    r = await vrig()
    await _listening(r)
    r.rec.partial_text = "hey"
    await asyncio.sleep(0.4)
    assert r.wake.ducks == 0


async def test_nothing_is_carried_into_the_chat_during_a_video(vrig):
    """The video's soundtrack reaches the microphone: it must never be sent as what the person said."""
    r = await vrig()
    await _listening(r)
    sounds = _spy_earcon(r)
    r.rec.say("hey little bot what is that", conf=0.9, wake_ended_ago=0.5)
    await until(lambda: r.live.calls, what="live voice started")
    assert r.live.carries == [None]
    assert sounds and sounds[0][0] == "listen"  # and the earcon plays as usual
