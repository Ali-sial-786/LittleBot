"""GamesService: catalogue, start / stop, leases, sharing with voice, STOP ALL, time limits, hooks."""

from __future__ import annotations

import asyncio

import pytest

from games_helpers import distinct_colors
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.games import create_service, service as games_service
from littlebot.games.service import GAME_FINISHED, GAME_STARTED, OWNER, GamesService, StopResult

pygame = pytest.importorskip("pygame")


def holders(ctx):
    return ctx.arbiter.snapshot()


def face_calls(ctx, name):
    return [a for a, _k in ctx.face.called(name)]


# ------------------------------------------------------------------ catalogue
def test_list_has_the_exact_public_shape(svc):
    items = svc.list()
    assert len(items) == 8
    ids = [g["id"] for g in items]
    assert ids == ["bubbles", "peekaboo", "colours", "star", "memory", "tictactoe", "reaction", "rps"]
    for g in items:
        assert {"id", "name", "description", "ages"} <= set(g)
        assert g["name"] and g["description"] and g["ages"]
        assert g["audience"] in ("little", "big")
    assert svc.get("memory")["name"] == "Memory Lights"
    assert svc.get("nope") is None


def test_create_service_factory(ctx):
    s = create_service(ctx)
    try:
        assert isinstance(s, GamesService)
        assert s.status() == {"available": True, "running": False, "games": 8, "last": None, "last_error": None}
    finally:
        s.close()


async def test_lifecycle_start_without_id(svc):
    assert await svc.start() is True
    assert svc.health()["status"] == "running"
    assert not svc.active


# ------------------------------------------------------------------ start / stop
async def test_start_unknown_game_is_refused(svc, ctx):
    assert await svc.start("chess") is False
    assert "chess" in svc.status()["last_error"]
    assert holders(ctx)["screen"] is None and ctx.state.state == RobotState.IDLE


async def test_start_takes_screen_face_and_speaker(svc, ctx):
    events = collect(ctx.bus, [GAME_STARTED])
    assert await svc.start("bubbles", source="test") is True
    snap = holders(ctx)
    assert snap["screen"] == snap["face"] == snap["audio_out"] == OWNER
    assert snap["microphone"] is None  # games never take the microphone
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY
    assert svc.active
    st = svc.status()
    assert st["running"] and st["id"] == "bubbles" and st["name"] == "Bubble Pop" and st["source"] == "test"
    assert st["limit_seconds"] == 180 and st["seconds_left"] == 180 and st["shared"] is False
    assert events[0].data["id"] == "bubbles"


async def test_minutes_override_is_clamped(svc):
    assert await svc.start("memory", minutes=99)
    assert svc.status()["limit_seconds"] == 15 * 60
    assert await svc.start("memory", minutes=0.01)
    assert svc.status()["limit_seconds"] == 30


async def test_stop_releases_everything_and_returns_the_face(svc, ctx):
    await svc.start("peekaboo")
    events = collect(ctx.bus, [GAME_FINISHED])
    result = svc.stop()
    assert isinstance(result, StopResult) and result and result == True  # noqa: E712
    assert not svc.active
    assert all(v is None for v in holders(ctx).values())
    assert ctx.state.state == RobotState.IDLE
    assert ("neutral",) in face_calls(ctx, "set_expression") and face_calls(ctx, "open_eyes")
    assert events[0].data["id"] == "peekaboo" and events[0].data["reason"] == "stopped"
    assert events[0].data["completed"] is False
    assert svc.status()["last"]["name"] == "Peekaboo Friends"
    assert not svc.stop()
    assert await svc.stop() is False  # awaitable too (the robot's shutdown awaits every stop())


async def test_stop_all_event_stops_the_game(svc, ctx):
    await svc.start("memory")
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    assert not svc.active
    assert svc.status()["last"]["reason"] == "stop all"
    assert ctx.state.state == RobotState.IDLE


async def test_preempt_all_stops_the_game(svc, ctx):
    await svc.start("memory")
    ctx.arbiter.preempt_all(by="parent")
    assert not svc.active
    assert svc.status()["last"]["reason"] == "preempted by parent"
    assert ctx.state.state == RobotState.IDLE


async def test_a_video_preempts_the_game(svc, ctx):
    await svc.start("star")
    lease = ctx.arbiter.acquire("media", [Resource.SCREEN, Resource.AUDIO_OUT, Resource.FACE], Priority.MEDIA)
    assert lease is not None
    assert not svc.active
    assert svc.status()["last"]["reason"] == "preempted by media"
    assert ctx.state.state == RobotState.IDLE  # released synchronously, before media moves to PLAYING_MEDIA
    assert ("neutral",) not in face_calls(ctx, "set_expression")  # the new owner drives the face
    assert ctx.state.transition(RobotState.PLAYING_MEDIA, "video")


async def test_an_activity_preempts_the_game(svc, ctx):
    await svc.start("colours")
    assert ctx.arbiter.acquire("activities", [Resource.FACE, Resource.AUDIO_OUT], Priority.ACTIVITY) is not None
    assert not svc.active
    assert holders(ctx)["screen"] is None


async def test_refused_while_a_video_owns_the_screen(svc, ctx):
    ctx.arbiter.acquire("media", [Resource.SCREEN, Resource.AUDIO_OUT, Resource.FACE], Priority.MEDIA)
    assert await svc.start("bubbles") is False
    assert "video" in svc.status()["last_error"]
    assert holders(ctx)["screen"] == "media"
    assert not svc.active


async def test_robot_going_to_sleep_ends_the_game(svc, ctx):
    await svc.start("bubbles")
    assert ctx.state.transition(RobotState.SLEEPING, "parent")
    assert not svc.active
    assert svc.status()["last"]["reason"] == "robot sleeping"


async def test_starting_another_game_replaces_the_first(svc, ctx):
    await svc.start("bubbles")
    events = collect(ctx.bus, [GAME_FINISHED])
    assert await svc.start("tictactoe")
    assert events[0].data["id"] == "bubbles" and events[0].data["reason"] == "replaced"
    assert svc.status()["id"] == "tictactoe"
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY
    snap = holders(ctx)
    assert snap["screen"] == snap["face"] == OWNER
    svc.stop()
    assert all(v is None for v in holders(ctx).values())


async def test_starting_a_game_stops_a_running_activity(svc, ctx):
    class Acts:
        def __init__(self):
            self.stopped = []

        def status(self):
            return {"running": not self.stopped}

        async def stop(self, reason="stopped"):
            self.stopped.append(reason)

    ctx.activities = Acts()
    assert await svc.start("bubbles")
    assert ctx.activities.stopped == ["game started"]


# ------------------------------------------------------------------ voice sharing
async def test_game_started_mid_conversation_shares_face_and_speaker(svc, ctx, clock):
    voice = ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT)
    ctx.state.transition(RobotState.SPEAKING, "live voice")
    assert await svc.start("bubbles", source="voice")
    snap = holders(ctx)
    assert snap["screen"] == OWNER and snap["face"] == "live_voice" and snap["audio_out"] == "live_voice"
    assert svc.status()["shared"] is True
    assert ctx.state.state == RobotState.SPEAKING  # the conversation keeps its state
    voice.release()
    ctx.state.transition(RobotState.IDLE, "live voice ended")
    svc.tick()
    snap = holders(ctx)
    assert snap["face"] == snap["audio_out"] == OWNER
    assert svc.status()["shared"] is False
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY
    svc.stop()
    assert ctx.state.state == RobotState.IDLE


async def test_live_voice_during_a_game_keeps_the_game_on_screen(svc, ctx):
    await svc.start("memory")
    voice = ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT)
    assert voice is not None
    assert svc.active and svc.status()["shared"] is True
    assert holders(ctx)["screen"] == OWNER


async def test_conversation_turn_is_refused_while_a_game_holds_the_face(svc, ctx):
    await svc.start("memory")
    assert ctx.arbiter.acquire("conversation", [Resource.FACE], Priority.CONVERSATION) is None
    assert svc.active


# ------------------------------------------------------------------ time limit / headless driving
async def test_time_limit_plays_finale_then_returns_a_happy_face(svc, ctx, clock):
    await svc.start("bubbles", minutes=0.5)
    clock.advance(29)
    svc.tick()
    assert svc.status()["game"]["finale"] is False
    clock.advance(2)
    svc.tick()
    assert svc.status()["game"]["finale"] is True and svc.active
    events = collect(ctx.bus, [GAME_FINISHED])
    for _ in range(40):
        clock.advance(0.25)
        svc.tick()
        if not svc.active:
            break
    assert not svc.active
    assert events[0].data["reason"] == "time up" and events[0].data["completed"] is True
    assert ("happy",) in face_calls(ctx, "set_expression")
    assert ("sparkle",) in face_calls(ctx, "play_animation")
    assert ctx.state.state == RobotState.IDLE


async def test_headless_tick_keeps_the_game_running(svc, clock):
    await svc.start("star")
    game = svc._session.game
    for _ in range(8):
        clock.advance(0.25)
        svc.tick()
    assert game.t == pytest.approx(2.0, abs=0.01)


async def test_watchdog_task_drives_the_session(svc, clock, monkeypatch):
    monkeypatch.setattr(games_service, "WATCH_PERIOD", 0.005)
    await svc.start("memory", minutes=0.5)
    clock.advance(31)  # past the limit: the watchdog starts the finale by itself
    for _ in range(100):
        await asyncio.sleep(0.005)
        if svc.status().get("game", {}).get("finale"):
            break
    assert svc.status()["game"]["finale"] is True
    clock.advance(30)
    for _ in range(100):
        await asyncio.sleep(0.005)
        if not svc.active:
            break
    assert not svc.active and svc.status()["last"]["reason"] == "time up"


# ------------------------------------------------------------------ display hooks
async def test_render_draws_the_game_native_and_scaled(svc, surface):
    assert svc.render(surface, surface.get_rect(), 1 / 30) is False  # nothing running
    await svc.start("memory")
    assert svc.render(surface, surface.get_rect(), 1 / 30) is True
    assert distinct_colors(surface) > 8
    big = pygame.Surface((1260, 660))
    rect = pygame.Rect(150, 10, 960, 640)
    assert svc.render(big, rect, 1 / 30) is True
    assert big.get_at((10, 10))[:3] == (0, 0, 0)  # outside the face rect untouched
    assert big.get_at(rect.center)[:3] != (0, 0, 0)
    assert svc.status()["drawn"] is True


async def test_taps_go_to_the_game_and_never_fall_through(svc):
    assert svc.handle_tap(0.5, 0.5) is False
    await svc.start("bubbles")
    game = svc._session.game
    b = game.bubbles[0]
    assert svc.handle_tap(b.x / game.w, b.y / game.h) is True
    assert game.popped == 1
    assert svc.status()["game"]["popped"] == 1


async def test_keys_escape_stops_other_keys_go_to_the_game(svc):
    assert svc.handle_key("escape") is False
    await svc.start("tictactoe")
    game = svc._session.game
    game.starter = game.turn = "X"
    game.set_phase("play")
    assert svc.handle_key("5") is True and game.board[4] == "X"
    assert svc.handle_key("x") is False  # not a game key: the display handles it (STOP ALL)
    assert svc.handle_key("escape") is True
    assert not svc.active and svc.status()["last"]["reason"] == "keyboard"


async def test_broken_game_falls_back_to_the_face(svc, surface, monkeypatch):
    await svc.start("bubbles")
    game = svc._session.game

    def boom(_surf):
        raise RuntimeError("bad frame")

    monkeypatch.setattr(game, "draw", boom)
    for _ in range(games_service.MAX_FRAME_ERRORS - 1):
        assert svc.render(surface, None, 1 / 30) is False
    assert svc.active
    svc.render(surface, None, 1 / 30)
    assert not svc.active and svc.status()["last"]["reason"] == "error"


# ------------------------------------------------------------------ host: sound, speech, camera, voice
async def test_game_sounds_reach_audio_and_are_throttled(svc, ctx, clock):
    await svc.start("bubbles")
    await asyncio.sleep(0)
    ctx.audio.sounds.clear()
    for _ in range(10):  # a toddler hammering the screen: no growing audio backlog
        svc.host.sound("pop")
    await asyncio.sleep(0.01)
    assert 1 <= ctx.audio.sounds.count("pop") <= 3
    for call in (lambda: svc.host.sound("giggle"), lambda: svc.host.melody([("C5", 0.5)]),
                 lambda: svc.host.say("Moo!")):
        clock.advance(2.0)  # the previous sound has finished
        call()
    await asyncio.sleep(0.01)
    assert "giggle" in ctx.audio.sounds
    assert ctx.audio.called("play_melody") and ctx.audio.spoken == ["Moo!"]


async def test_feedback_sounds_may_wait_a_little_longer(svc, ctx, clock):
    await svc.start("memory")
    await asyncio.sleep(0)
    ctx.audio.sounds.clear()
    svc.host.sound("peekaboo")
    svc.host.sound("giggle")  # dropped: would play late
    svc.host.sound("giggle", max_wait=2.0)  # queued: late is better than never for this one
    await asyncio.sleep(0.01)
    assert ctx.audio.sounds == ["peekaboo", "giggle"]


async def test_no_audio_is_fine(svc, ctx):
    ctx.audio = None
    assert await svc.start("colours")
    svc.host.sound("pop")
    svc.host.say("hello")
    assert svc.active


async def test_child_gaze_comes_from_vision(svc, ctx, clock):
    assert svc.child_gaze() is None
    ctx.bus.publish(Topics.VISION_FACES, {"faces": [
        {"x": 0, "y": 0, "w": 10, "h": 10, "gaze_x": 0.2, "gaze_y": 0.4, "primary": False},
        {"x": 0, "y": 0, "w": 5, "h": 5, "gaze_x": 0.7, "gaze_y": 0.5, "primary": True}]})
    assert svc.child_gaze() == (0.7, 0.5)
    clock.advance(5)
    assert svc.child_gaze() is None  # stale
    ctx.bus.publish(Topics.VISION_FACES, {"faces": [{"w": 4, "h": 4, "gaze_x": 0.3, "gaze_y": 0.3}]})
    assert svc.child_gaze() == (0.3, 0.3)
    ctx.bus.publish(Topics.VISION_PERSON_LEFT, {})
    assert svc.child_gaze() is None


async def test_heard_words_reach_the_game(svc, ctx):
    await svc.start("rps")
    ctx.bus.publish(Topics.AUDIO_UTTERANCE, {"text": "I pick Scissors!", "source": "stt"})
    game = svc._session.game
    assert game.you == "scissors" and game.phase == "count"


# ------------------------------------------------------------------ the real robot facade
def test_littlebot_builds_games_and_stop_all_stops_them(ctx):
    from littlebot.app.robot import SUBSYSTEMS, LittleBot

    assert ("games", "littlebot.games") in SUBSYSTEMS
    bot = LittleBot(ctx)
    bot.build_subsystems(only={"games"})
    assert isinstance(ctx.games, GamesService)
    bot.runtime.start()
    try:
        assert bot.runtime.run(ctx.games.start("reaction", source="test"))
        assert bot.status()["games"]["running"] is True
        bot.runtime.run(bot.stop_all("test"))
        assert not ctx.games.active
        assert bot.status()["games"]["running"] is False
        bot.runtime.run(bot._stop_async())  # shutdown awaits games.stop() without errors
    finally:
        ctx.games.close()
        bot.runtime.stop()
