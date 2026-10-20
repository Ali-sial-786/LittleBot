"""Media built by the composition root (app/robot.py): stop-all button and shutdown."""

from __future__ import annotations

from littlebot.app.robot import LittleBot
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.media.controller import MediaController
from littlebot.testing import make_test_context


def test_robot_builds_media_and_stop_all_returns_to_face():
    ctx = make_test_context(dashboard={"enabled": False})
    ctx.settings.set("profile", {"age_months": 30})
    bot = LittleBot(ctx)
    bot.build_subsystems(only={"media"})
    assert isinstance(ctx.media, MediaController)
    events = collect(ctx.bus, ["media.*", "face.visible"])
    bot.start()
    try:
        assert ctx.media.health()["status"] == "running"
        item = ctx.media.add_item("https://youtu.be/dQw4w9WgXcQ", "Song")
        bot.runtime.run(ctx.media.play(item["id"]))
        assert ctx.state.state == RobotState.PLAYING_MEDIA
        assert bot.status()["media"]["state"] == "loading"
        bot.runtime.run(bot.stop_all("parent"))
        assert ctx.state.state == RobotState.IDLE
        stopped = [e for e in events if e.topic == Topics.MEDIA_STOPPED]
        assert len(stopped) == 1 and stopped[0].data["reason"] == "stopped"
        assert [e.data["visible"] for e in events if e.topic == Topics.FACE_VISIBLE] == [False, True]
        assert all(v is None for v in ctx.arbiter.snapshot().values())
        bot.runtime.run(ctx.media.play(item["id"]))
    finally:
        bot.shutdown("test")
    assert ctx.media.health()["status"] == "stopped"
    assert ctx.state.state == RobotState.IDLE
