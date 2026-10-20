"""MediaController: gating, limits, leases/state, preemption, STOP_ALL, skip, volume, player events."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from media_testkit import LIST_A, VID_A, VID_B, VID_C  # type: ignore[import-not-found]

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.interfaces import MediaAPI
from littlebot.core.service import Service
from littlebot.core.state import RobotState
from littlebot.media import create_service
from littlebot.media.controller import MediaController
from littlebot.media.oembed import OEmbedResult


# ------------------------------------------------------------------ wiring
def test_create_service_implements_protocols(ctx):
    media = create_service(ctx)
    assert isinstance(media, MediaController)
    assert isinstance(media, MediaAPI)
    assert isinstance(media, Service)
    assert media.backend.name == "none"  # tests config: media.player = none
    st = media.status()
    for key in ("state", "item", "session_seconds", "session_limit_seconds", "daily_used_seconds",
                "daily_limit_seconds", "player", "volume"):
        assert key in st
    assert st["state"] == "idle" and st["item"] is None
    assert st["session_limit_seconds"] == 15 * 60 and st["daily_limit_seconds"] == 30 * 60
    for key in ("backend", "url", "connected", "last_event"):
        assert key in st["player"]
    assert st["player"]["url"] == f"http://127.0.0.1:{ctx.config.dashboard.port}/player?token={media.player_token}"
    assert len(media.player_token) >= 32
    assert create_service(ctx).player_token != media.player_token  # random per instance


async def test_service_lifecycle(env):
    assert env.media.health()["status"] == "running"
    env.ctx.bus.publish(Topics.SYSTEM_SHUTDOWN, {"reason": "test"})
    await env.media.stop()  # how app/robot.py stops services
    assert env.media.health()["status"] == "stopped"


async def test_offline_service_is_disabled(make_env):
    e = make_env(mode="offline")
    await e.media.start()
    assert e.media.health()["status"] == "disabled"
    item = e.add()
    with pytest.raises(PermissionError, match="offline"):
        await e.media.play(item["id"])
    await e.media.close()


async def test_degraded_when_backend_unavailable(make_env, fake_backend_cls):
    class Missing(fake_backend_cls):
        @property
        def available(self) -> bool:
            return False

    e = make_env(backend=Missing())
    await e.media.start()
    assert e.media.health()["status"] == "degraded"
    await e.media.close()


# ------------------------------------------------------------------ allowlist via controller
async def test_add_item_validates(env):
    item = env.media.add_item(f"https://youtu.be/{VID_A}", "Twinkle")
    assert env.media.list_items()[0]["id"] == item["id"]
    with pytest.raises(ValueError, match="already"):
        env.media.add_item(f"https://www.youtube.com/watch?v={VID_A}")
    with pytest.raises(ValueError, match="channel"):
        env.media.add_item("https://www.youtube.com/@cocomelon")
    pl = env.media.add_item(f"https://www.youtube.com/watch?v={VID_B}&list={LIST_A}", kind="playlist")
    assert pl["kind"] == "playlist" and pl["list_id"] == LIST_A
    assert env.media.update_item(item["id"], title="Renamed")["title"] == "Renamed"
    with pytest.raises(KeyError):
        env.media.update_item(999, title="x")
    assert env.media.remove_item(item["id"]) is True and env.media.remove_item(item["id"]) is False


# ------------------------------------------------------------------ gating
async def test_unknown_age_blocked_by_default(make_env):
    e = make_env(age=None)
    item = e.add()
    with pytest.raises(PermissionError, match="age is set"):
        await e.media.play(item["id"])
    assert e.ctx.state.state == RobotState.IDLE and e.ctx.arbiter.is_free(Resource.SCREEN)
    assert e.backend.opened == []
    st = await e.media.play(item["id"], parent_override=True)
    assert st["state"] == "loading"
    await e.media.close()


async def test_under_18_months_blocked_unless_parent_allows(make_env):
    e = make_env(age=10)
    item = e.add()
    with pytest.raises(PermissionError, match="under 18 months"):
        await e.media.play(item["id"])
    assert e.media.status()["age_gate"]["video_allowed"] is False
    # explicit one-off parent override
    await e.media.play(item["id"], parent_override=True)
    assert e.ctx.state.state == RobotState.PLAYING_MEDIA
    await e.media.stop()
    # or the standing parent setting
    e.ctx.settings.set("media.allow_under_min_age", True)
    assert e.media.status()["age_gate"]["video_allowed"] is True
    await e.media.play(item["id"])
    assert e.ctx.state.state == RobotState.PLAYING_MEDIA
    await e.media.close()


async def test_old_enough_child_allowed(env):
    item = env.add()
    st = await env.media.play(item["id"])
    assert st["state"] == "loading" and st["item"]["id"] == item["id"]


async def test_media_disabled_setting(env):
    item = env.add()
    env.ctx.settings.set("media.enabled", False)
    with pytest.raises(PermissionError, match="turned off"):
        await env.media.play(item["id"])


async def test_unknown_and_disabled_items(env):
    with pytest.raises(KeyError):
        await env.media.play(12345)
    item = env.add()
    env.media.update_item(item["id"], enabled=False)
    with pytest.raises(PermissionError, match="switched off"):
        await env.media.play(item["id"])


async def test_zero_limit_refuses(env):
    item = env.add()
    env.ctx.settings.set("media.session_limit_minutes", 0)
    with pytest.raises(PermissionError, match="0 minutes"):
        await env.media.play(item["id"])


async def test_busy_with_higher_priority_lease(env):
    item = env.add()
    lease = env.ctx.arbiter.acquire("parent-face", [Resource.FACE], Priority.PARENT)
    with pytest.raises(PermissionError, match="busy"):
        await env.media.play(item["id"])
    assert env.ctx.state.state == RobotState.IDLE
    lease.release()


async def test_state_transition_refused_releases_lease(env):
    item = env.add()
    env.ctx.state.transition(RobotState.ERROR, "test")
    with pytest.raises(PermissionError, match="can't start"):
        await env.media.play(item["id"])
    assert env.ctx.arbiter.is_free(Resource.SCREEN)


# ------------------------------------------------------------------ lease / state / face events
async def test_play_stop_lease_state_and_face(env):
    item = env.add(title="Moon song")
    await env.media.play(item["id"])
    ctx = env.ctx
    assert ctx.state.state == RobotState.PLAYING_MEDIA
    snap = ctx.arbiter.snapshot()
    assert snap["screen"] == snap["audio_out"] == snap["face"] == "media"
    assert ctx.arbiter.holder(Resource.SCREEN).priority == Priority.MEDIA
    started = env.last(Topics.MEDIA_STARTED)
    assert started.data["item_id"] == item["id"] and started.data["title"] == "Moon song"
    assert started.data["kind"] == "video"
    assert env.face_states() == ["hidden"]  # the face hides itself for PLAYING_MEDIA
    assert env.backend.opened == [env.media.player_url()]
    desired = env.poll()
    assert desired["action"] == "play" and desired["item"]["video_id"] == VID_A and desired["load_id"]
    assert desired["youtube_host"] == "https://www.youtube-nocookie.com"

    await env.media.stop()
    assert ctx.state.state == RobotState.IDLE
    assert all(v is None for v in ctx.arbiter.snapshot().values())
    stopped = env.last(Topics.MEDIA_STOPPED)
    assert stopped.data["reason"] == "stopped" and stopped.data["item_id"] == item["id"]
    assert env.face_states() == ["hidden", "idle"]  # animated face returns
    assert env.all(Topics.FACE_VISIBLE) == []  # single owner: the face module
    assert env.poll()["action"] == "stop"
    assert env.backend.closed == 1 and not env.backend.running  # closed before stop() returned
    # idempotent
    await env.media.stop()
    assert len(env.all(Topics.MEDIA_STOPPED)) == 1


async def test_player_ended_returns_to_face(env):
    item = env.add()
    await env.media.play(item["id"])
    await env.page_event("ready")
    await env.page_event("playing", player_state=1, current_time=0.5, duration=120, video_id=VID_A)
    assert env.media.status()["state"] == "playing"
    await env.page_event("ended")
    assert env.ctx.state.state == RobotState.IDLE
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "ended"
    assert env.face_states()[-1] == "idle"
    states = [(e.data["old"], e.data["new"]) for e in env.all(Topics.STATE_CHANGED)]
    assert states == [("IDLE", "PLAYING_MEDIA"), ("PLAYING_MEDIA", "IDLE")]
    assert env.poll()["action"] == "stop" and env.poll()["reason"] == "ended"


@pytest.mark.parametrize("code", [101, 150, "150"])
async def test_embedding_disabled_error(env, code):
    item = env.add()
    await env.media.play(item["id"])
    await env.page_event("error", code=code)
    err = env.last(Topics.MEDIA_ERROR)
    assert err.data["item_id"] == item["id"] and "Unsupported video" in err.data["error"]
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert env.ctx.state.state == RobotState.IDLE
    assert "Unsupported video" in env.media.get_item(item["id"])["last_error"]
    assert env.media.status()["last_error"]["item_id"] == item["id"]


@pytest.mark.parametrize("code, text", [(2, "error 2"), (5, "error 5"), (100, "not found"),
                                        ("api_load_failed", "internet"), ("unexpected_video", "not on the approved"),
                                        (999, "999")])
async def test_other_player_errors(env, code, text):
    item = env.add()
    await env.media.play(item["id"])
    await env.page_event("error", code=code)
    assert text in env.last(Topics.MEDIA_ERROR).data["error"]
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"


async def test_successful_play_clears_previous_error(env):
    item = env.add()
    env.media.repo.set_error(item["id"], "old problem")
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_A)
    assert env.media.get_item(item["id"])["last_error"] is None


async def test_stale_events_from_previous_item_are_ignored(env):
    a, b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    old_load = env.poll()["load_id"]
    await env.media.skip()
    assert env.poll()["item"]["id"] == b["id"]
    result = await env.media.handle_player_event({"type": "ended", "load_id": old_load})
    assert result["current"] is False
    result = await env.media.handle_player_event({"type": "error", "code": 150, "load_id": "bogus"})
    assert result["current"] is False
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA
    assert env.last(Topics.MEDIA_STOPPED) is None and env.last(Topics.MEDIA_ERROR) is None


async def test_play_other_item_during_session_switches(env):
    a, b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    sid = env.media.status()["session_id"]
    env.clock.advance(30)
    await env.media.play(b["id"])
    st = env.media.status()
    assert st["item"]["id"] == b["id"] and st["session_id"] == sid
    assert st["session_seconds"] == pytest.approx(30)
    assert len(env.backend.opened) == 1  # page already open: it loads the new item itself
    assert len(env.all(Topics.MEDIA_STARTED)) == 2 and env.face_states() == ["hidden"]


# ------------------------------------------------------------------ pause / resume
async def test_pause_resume_and_paused_time_not_counted(env):
    item = env.add()
    assert await env.media.pause() is False and await env.media.resume() is False
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_A)
    env.clock.advance(10)
    assert await env.media.pause() is True and await env.media.pause() is False
    assert env.poll()["action"] == "pause"
    assert env.last(Topics.MEDIA_PAUSED).data["item_id"] == item["id"]
    env.clock.advance(300)
    st = env.media.status()
    assert st["state"] == "paused" and st["session_seconds"] == pytest.approx(10)
    assert st["paused_seconds"] == pytest.approx(300)
    assert await env.media.resume() is True
    assert env.poll()["action"] == "play" and env.media.status()["state"] == "playing"
    assert env.last(Topics.MEDIA_RESUMED).data["item_id"] == item["id"]
    env.clock.advance(5)
    assert env.media.status()["session_seconds"] == pytest.approx(15)
    assert env.media.status()["daily_used_seconds"] == pytest.approx(15)


async def test_long_pause_gives_screen_back(env):
    item = env.add()
    await env.media.play(item["id"])
    await env.media.pause()
    env.clock.advance(env.ctx.config.media.max_pause_minutes * 60 + 1)
    await env.media.tick()
    stopped = env.last(Topics.MEDIA_STOPPED)
    assert stopped.data["reason"] == "stopped" and "paused" in stopped.data["detail"]
    assert env.ctx.state.state == RobotState.IDLE


# ------------------------------------------------------------------ limits
async def test_session_limit(env):
    env.ctx.settings.set("media.session_limit_minutes", 1)
    item = env.add()
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_A)
    env.clock.advance(59)
    await env.media.tick()
    assert env.media.status()["state"] == "playing"
    env.clock.advance(2)
    await env.media.tick()
    stopped = env.last(Topics.MEDIA_STOPPED)
    assert stopped.data["reason"] == "limit" and "session" in stopped.data["detail"]
    assert env.poll()["action"] == "stop" and env.poll()["reason"] == "limit"
    assert env.ctx.state.state == RobotState.IDLE
    assert env.face_states()[-1] == "idle"
    # a new session may start (the daily limit is the hard cap)
    await env.media.play(item["id"])
    assert env.media.status()["session_seconds"] == 0


async def test_skip_and_resume_check_limits(env):
    env.ctx.settings.set("media.session_limit_minutes", 1)
    a, _b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    env.clock.advance(61)
    await env.media.skip()
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "limit"
    await env.media.play(a["id"])
    await env.media.pause()
    env.ctx.settings.set("media.daily_limit_minutes", 1)
    assert await env.media.resume() is False
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "limit"


async def test_daily_limit_persists_across_restart_and_resets_next_day(make_env):
    e = make_env()
    e.ctx.settings.set("media.daily_limit_minutes", 2)
    item = e.add()
    await e.media.start()
    await e.media.play(item["id"])
    e.clock.advance(90)
    await e.media.tick()
    assert e.media.status()["state"] == "loading"
    await e.media.stop()
    assert e.media.usage.get(e.day[0]) == pytest.approx(90)
    await e.media.close()

    # "restart": a new controller on the same database sees today's usage
    e2 = make_env(ctx=e.ctx, clock=e.clock, day=e.day)
    await e2.media.start()
    assert e2.media.status()["daily_used_seconds"] == pytest.approx(90)
    await e2.media.play(item["id"])
    e2.clock.advance(31)
    await e2.media.tick()
    stopped = e2.last(Topics.MEDIA_STOPPED)
    assert stopped.data["reason"] == "limit" and "daily" in stopped.data["detail"]
    with pytest.raises(PermissionError, match="used up"):
        await e2.media.play(item["id"])
    e2.day[0] = e2.day[0] + dt.timedelta(days=1)
    assert e2.media.status()["daily_used_seconds"] == 0
    await e2.media.play(item["id"])
    assert e2.media.status()["state"] == "loading"
    await e2.media.close()
    assert e2.media.usage.get(dt.date(2026, 10, 20)) == pytest.approx(121)


async def test_limits_enforced_by_background_watchdog(make_env):
    e = make_env(tick=0.01)
    e.ctx.settings.set("media.session_limit_minutes", 1)
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    e.clock.advance(61)
    ev = await e.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=3)
    assert ev is not None and ev.data["reason"] == "limit"
    assert e.ctx.state.state == RobotState.IDLE
    await e.media.close()


async def test_usage_is_flushed_periodically(env):
    item = env.add()
    await env.media.play(item["id"])
    env.clock.advance(12)
    await env.media.tick()
    assert env.media.usage.get(env.day[0]) == pytest.approx(12)


def test_limit_settings_are_validated(ctx):
    create_service(ctx)
    assert ctx.settings.set("media.session_limit_minutes", 999) == 120
    assert ctx.settings.set("media.daily_limit_minutes", -5) == 0
    with pytest.raises(ValueError):
        ctx.settings.set("media.daily_limit_minutes", "lots")


# ------------------------------------------------------------------ player health
async def test_player_never_connects(make_env):
    e = make_env(media_cfg={"connect_timeout_seconds": 60})
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    e.clock.advance(61)
    await e.media.tick()
    assert e.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert "didn't open" in e.last(Topics.MEDIA_ERROR).data["error"]
    await e.media.close()


async def test_player_heartbeat_lost(make_env):
    e = make_env(media_cfg={"heartbeat_timeout_seconds": 15, "connect_timeout_seconds": 60})
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    e.poll()
    await e.page_event("heartbeat")
    e.clock.advance(10)
    await e.media.tick()
    assert e.media.status()["state"] == "loading"
    assert e.media.status()["player"]["connected"] is False  # >6 s since last poll
    e.clock.advance(6)
    await e.media.tick()
    assert "stopped responding" in e.last(Topics.MEDIA_ERROR).data["error"]
    await e.media.close()


async def test_kiosk_window_died(env):
    item = env.add()
    await env.media.play(item["id"])
    env.backend.running = False  # the Chromium process exited
    await env.media.tick()
    assert "closed unexpectedly" in env.last(Topics.MEDIA_ERROR).data["error"]
    assert env.ctx.state.state == RobotState.IDLE


async def test_backend_failure_is_reported(make_env, fake_backend_cls):
    e = make_env(backend=fake_backend_cls(fail="Chromium is not installed"))
    await e.media.start()
    item = e.add()
    with pytest.raises(PermissionError, match="Chromium is not installed"):
        await e.media.play(item["id"])
    assert e.ctx.state.state == RobotState.IDLE and e.ctx.arbiter.is_free(Resource.SCREEN)
    assert e.last(Topics.MEDIA_ERROR) is not None
    assert e.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert e.face_states() == ["hidden", "idle"]
    await e.media.close()


async def test_desktop_backend_not_reopened_while_page_connected(make_env, fake_backend_cls):
    e = make_env(backend=fake_backend_cls(name="browser", can_close=False))
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    e.poll()  # the tab is open and polling
    await e.media.stop()
    await e.media.play(item["id"])
    assert len(e.backend.opened) == 1 and e.backend.closed == 0
    await e.media.close()


# ------------------------------------------------------------------ preemption / stop all
async def test_preempted_by_higher_priority_lease(env):
    item = env.add()
    await env.media.play(item["id"])
    lease = env.ctx.arbiter.acquire("parent-command", [Resource.AUDIO_OUT], Priority.PARENT)
    assert lease is not None
    assert env.poll()["action"] == "stop"  # video halted immediately
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and ev.data["reason"] == "stopped" and "preempted" in ev.data["detail"]
    assert env.ctx.state.state == RobotState.IDLE
    assert env.ctx.arbiter.holder(Resource.AUDIO_OUT) is lease  # never released someone else's lease


async def test_lower_priority_cannot_preempt_media(env):
    item = env.add()
    await env.media.play(item["id"])
    assert env.ctx.arbiter.acquire("activity", [Resource.SCREEN, Resource.FACE], Priority.ACTIVITY) is None
    await env.settle()
    assert env.media.status()["state"] == "loading"


async def test_media_preempts_running_activity(env):
    preempted = []
    env.ctx.arbiter.acquire("activity", [Resource.FACE, Resource.AUDIO_OUT], Priority.ACTIVITY,
                            on_preempt=lambda lease, by: preempted.append(by))
    env.ctx.state.transition(RobotState.PLAYING_ACTIVITY, "test")
    item = env.add()
    await env.media.play(item["id"])
    assert preempted == ["media"]
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA


async def test_stop_all_event(env):
    item = env.add()
    await env.media.play(item["id"])
    env.ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and ev.data["reason"] == "stopped"
    assert env.ctx.state.state == RobotState.IDLE


async def test_stop_all_from_another_thread(env):
    item = env.add()
    await env.media.play(item["id"])
    await asyncio.to_thread(env.ctx.bus.publish, Topics.STOP_ALL, {"source": "button"})
    for _ in range(200):
        if env.last(Topics.MEDIA_STOPPED) is not None:
            break
        await asyncio.sleep(0.01)
    assert env.last(Topics.MEDIA_STOPPED) is not None and env.ctx.state.state == RobotState.IDLE


async def test_turning_media_off_or_removing_item_stops_playback(env):
    a, b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    env.ctx.settings.set("media.enabled", False)
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and "turned off" in ev.data["detail"]
    env.ctx.settings.set("media.enabled", True)
    await env.media.play(b["id"])
    env.media.remove_item(b["id"])
    assert env.poll()["action"] == "stop"
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and "removed" in ev.data["detail"]
    await env.media.play(a["id"])
    env.media.update_item(a["id"], enabled=False)
    ev = await env.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and "switched off" in ev.data["detail"]


async def test_age_setting_revoked_mid_session(make_env):
    e = make_env(age=12)
    e.ctx.settings.set("media.allow_under_min_age", True)
    await e.media.start()
    item = e.add()
    await e.media.play(item["id"])
    e.ctx.settings.set("media.allow_under_min_age", False)
    ev = await e.ctx.bus.wait_for(Topics.MEDIA_STOPPED, timeout=2)
    assert ev is not None and "age" in ev.data["detail"]
    await e.media.close()


# ------------------------------------------------------------------ skip
async def test_skip_only_moves_within_enabled_approved_items(env):
    a, b, c = env.add(VID_A, "A"), env.add(VID_B, "B"), env.add(VID_C, "C")
    env.media.update_item(b["id"], enabled=False)
    st = await env.media.skip()
    assert st["state"] == "idle"  # nothing playing: skip does nothing
    await env.media.play(a["id"])
    seen = []
    for _ in range(4):
        st = await env.media.skip()
        seen.append(st["item"]["id"])
        assert env.poll()["item"]["id"] == st["item"]["id"]
    assert seen == [c["id"], a["id"], c["id"], a["id"]]
    assert all(e.data["skipped"] for e in env.all(Topics.MEDIA_STARTED)[1:])
    approved = {i["youtube_id"] for i in env.media.list_items() if i["enabled"]}
    assert {e for e in (env.poll()["item"]["video_id"],)} <= approved


async def test_skip_with_single_item_restarts_it(env):
    a = env.add(VID_A, "A")
    await env.media.play(a["id"])
    first = env.poll()["load_id"]
    st = await env.media.skip()
    assert st["item"]["id"] == a["id"] and env.poll()["load_id"] != first


async def test_skip_when_nothing_left_stops(env):
    a = env.add(VID_A, "A")
    await env.media.play(a["id"])
    env.media.repo.update(a["id"], enabled=False)  # bypass the controller's own stop-on-disable
    await env.media.skip()
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "stopped"


async def test_playlist_item_sent_to_player(env):
    pl = env.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    await env.media.play(pl["id"])
    item = env.poll()["item"]
    assert item["kind"] == "playlist" and item["list_id"] == LIST_A and item["video_id"] is None


# ------------------------------------------------------------------ the reported video id (fail closed)
def _assert_stopped_unexpected(env, code="unexpected_video"):
    err = env.last(Topics.MEDIA_ERROR)
    assert err is not None and err.data["code"] == code
    stopped = env.last(Topics.MEDIA_STOPPED)
    assert stopped is not None and stopped.data["reason"] == "error"
    assert env.ctx.state.state == RobotState.IDLE and env.media.status()["state"] == "idle"
    assert env.poll()["action"] == "stop" and env.poll()["reason"] == "error"
    return err


@pytest.mark.parametrize("etype", ["playing", "heartbeat", "paused", "buffering", "ready", "track_ended"])
async def test_other_video_id_ends_a_single_video_session(env, etype):
    item = env.add(VID_A, "Approved")
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_A)
    assert env.media.status()["state"] == "playing"
    r = await env.page_event(etype, video_id="ZZZZZZZZZZZ")  # e.g. YouTube switched to another video
    assert r["current"] is True
    err = _assert_stopped_unexpected(env)
    assert "not on the approved" in err.data["error"]
    assert "not on the approved" in env.media.get_item(item["id"])["last_error"]


async def test_first_playing_report_with_wrong_id_is_refused(env):
    item = env.add(VID_A, "Approved")
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_B)  # another (real) video id: never approved
    _assert_stopped_unexpected(env)
    assert env.all(Topics.MEDIA_STOPPED)[-1].data["item_id"] == item["id"]


async def test_playing_without_a_video_id_is_never_approval(env):
    item = env.add(VID_A, "Approved")
    await env.media.play(item["id"])
    await env.page_event("playing")  # no id: time counts, but nothing is confirmed
    assert env.media.status()["state"] == "playing"
    env.clock.advance(9)
    await env.media.tick()
    assert env.media.status()["state"] == "playing"
    env.clock.advance(2)
    await env.media.tick()
    err = _assert_stopped_unexpected(env, "unverified_video")
    assert "didn't confirm" in err.data["error"]


async def test_late_confirmation_by_heartbeat_keeps_playing(env):
    item = env.add(VID_A, "Approved")
    await env.media.play(item["id"])
    await env.page_event("playing")
    env.clock.advance(5)
    await env.page_event("heartbeat", video_id=VID_A)
    env.clock.advance(30)
    await env.media.tick()
    assert env.media.status()["state"] == "playing" and env.last(Topics.MEDIA_ERROR) is None


async def test_a_later_playing_report_without_id_must_be_confirmed_again(env):
    item = env.add(VID_A, "Approved")
    await env.media.play(item["id"])
    await env.page_event("playing", video_id=VID_A)
    await env.page_event("playing")  # e.g. YouTube switched video and the page can't tell which
    env.clock.advance(11)
    await env.media.tick()
    _assert_stopped_unexpected(env, "unverified_video")


async def test_playlist_accepts_only_its_reported_video_ids(env):
    pl = env.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    await env.media.play(pl["id"])
    await env.page_event("ready", video_id=VID_A, playlist_ids=[VID_A, VID_B])
    await env.page_event("playing", video_id=VID_A)
    assert env.media.status()["state"] == "playing"
    await env.page_event("track_ended", video_id=VID_A)
    await env.page_event("playing", video_id=VID_B)  # the next track of the approved playlist
    await env.page_event("heartbeat", video_id=VID_B)
    env.clock.advance(30)
    await env.media.tick()
    assert env.media.status()["state"] == "playing" and env.last(Topics.MEDIA_ERROR) is None
    assert "playlist_ids" not in env.media.status()["player"]["last_event"]  # not echoed to the dashboard
    await env.page_event("playing", video_id=VID_C)  # not in the playlist's list
    _assert_stopped_unexpected(env)


@pytest.mark.parametrize("url", [f"https://www.youtube.com/playlist?list={LIST_A}",
                                 f"https://www.youtube.com/watch?v={VID_A}&list={LIST_A}"])
async def test_playlist_video_without_known_ids_is_refused(env, url):
    # No playlist_ids reported yet: no video id is approved - not even the link's own start video.
    pl = env.media.add_item(url, "Songs", kind="playlist")
    await env.media.play(pl["id"])
    await env.page_event("playing", video_id=VID_A)
    _assert_stopped_unexpected(env)


async def test_playlist_ids_are_per_load_and_capped(env):
    pl = env.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    other = env.add(VID_C, "C")
    await env.media.play(pl["id"])
    await env.page_event("playing", video_id=VID_A, playlist_ids=[VID_A, "bad id!", 42, VID_B])
    assert env.media.status()["state"] == "playing"
    await env.media.play(other["id"])  # a new load: the playlist's ids don't carry over
    await env.page_event("playing", video_id=VID_C, playlist_ids=[VID_A])  # ignored for a single video
    assert env.media.status()["state"] == "playing"
    await env.media.play(pl["id"])
    await env.page_event("playing", video_id=VID_B)
    _assert_stopped_unexpected(env)

    from littlebot.media.controller import MAX_PLAYLIST_IDS
    await env.media.play(pl["id"])
    many = [f"{i:011d}" for i in range(MAX_PLAYLIST_IDS + 50)]
    await env.page_event("heartbeat", playlist_ids=many)
    await env.page_event("playing", video_id=many[MAX_PLAYLIST_IDS - 1])
    assert env.media.status()["state"] == "playing"
    await env.page_event("playing", video_id=many[MAX_PLAYLIST_IDS + 10])  # beyond the cap: not remembered
    _assert_stopped_unexpected(env)


async def test_ids_of_stale_loads_are_ignored(env):
    a, b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    old = env.poll()["load_id"]
    await env.media.skip()
    r = await env.media.handle_player_event({"type": "playing", "load_id": old, "video_id": VID_A})
    assert r["current"] is False and env.media.status()["item"]["id"] == b["id"]
    assert env.last(Topics.MEDIA_ERROR) is None


async def test_stored_channel_uploads_list_is_refused_at_play_and_skipped(env):
    # Items stored before channel uploads lists were refused by the parser: never played.
    from littlebot.media.allowlist import ParsedMedia
    uu = "UU" + "x5XG1OV2P6uZZ5FSM9Ttw"
    bad = env.media.repo.add(ParsedMedia("playlist", None, uu, 0, f"https://www.youtube.com/playlist?list={uu}"),
                             "Whole channel")
    a = env.add(VID_A, "A")
    with pytest.raises(PermissionError, match="whole channels"):
        await env.media.play(bad["id"])
    assert env.ctx.state.state == RobotState.IDLE
    await env.media.play(a["id"])
    await env.media.skip()  # the only other item is the channel list: skip restarts A instead
    assert env.poll()["item"]["id"] == a["id"]


# ------------------------------------------------------------------ volume
async def test_volume_clamped_and_persisted(env):
    assert env.media.set_volume(0.3) == 0.3
    assert env.ctx.settings.get("media.volume") == 0.3
    assert env.poll()["volume"] == 0.3
    assert env.media.set_volume(5) == env.ctx.config.media.max_volume == 0.8
    assert env.media.set_volume(-1) == 0.0
    with pytest.raises(ValueError):
        env.media.set_volume("loud")
    with pytest.raises(ValueError):
        env.media.set_volume(float("nan"))
    env.ctx.settings.set("media.volume", 0.6)  # e.g. from the dashboard settings page
    assert env.poll()["volume"] == 0.6 and env.media.status()["volume"] == 0.6
    assert env.ctx.settings.set("media.volume", 3) == 0.8


async def test_volume_change_bumps_seq(env):
    s1 = env.poll()["seq"]
    env.media.set_volume(0.2)
    assert env.poll()["seq"] > s1


# ------------------------------------------------------------------ oEmbed (optional)
async def test_oembed_title_lookup_when_enabled(make_env):
    calls = []

    async def fake_lookup(url):
        calls.append(url)
        return OEmbedResult(200, "Twinkle Twinkle Little Star")

    e = make_env(media_cfg={"oembed_titles": True}, title_lookup=fake_lookup)
    await e.media.start()
    no_title = e.media.add_item(f"https://youtu.be/{VID_A}")
    with_title = e.media.add_item(f"https://youtu.be/{VID_B}", "Mum's pick")
    assert no_title["title"] == f"YouTube video {VID_A}"  # add_item never waits for the network
    await e.settle()
    assert e.media.get_item(no_title["id"])["title"] == "Twinkle Twinkle Little Star"
    assert e.media.get_item(with_title["id"])["title"] == "Mum's pick"
    assert calls == [no_title["url"], with_title["url"]]
    await e.media.close()


async def test_oembed_reports_embedding_disabled(make_env):
    async def fake_lookup(url):
        return OEmbedResult(401)

    e = make_env(media_cfg={"oembed_titles": True}, title_lookup=fake_lookup)
    await e.media.start()
    item = e.media.add_item(f"https://youtu.be/{VID_A}", "X")
    await e.settle()
    assert "Unsupported video" in e.media.get_item(item["id"])["last_error"]
    await e.media.close()


async def test_oembed_off_by_default_and_offline(make_env):
    calls = []

    async def fake_lookup(url):
        calls.append(url)
        return OEmbedResult(200, "t")

    e = make_env(title_lookup=fake_lookup)
    await e.media.start()
    e.media.add_item(f"https://youtu.be/{VID_A}")
    await e.settle()
    off = make_env(mode="offline", media_cfg={"oembed_titles": True}, title_lookup=fake_lookup)
    await off.media.start()
    off.media.add_item(f"https://youtu.be/{VID_A}")
    await off.settle()
    assert calls == []
    await e.media.close()
    await off.media.close()
