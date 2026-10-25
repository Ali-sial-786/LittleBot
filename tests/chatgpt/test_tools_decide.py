"""LittleBot's tools with a decision model (littlebot/decide): which search result plays (feature videos) and
which activity or game was meant when no name matches closely (names)."""

from __future__ import annotations

import json
from typing import Any

from test_tools import FakeActivities, FakeGames, FakeMedia, robot  # noqa: F401 - robot is a fixture

from littlebot.chatgpt.tools import RobotToolbox
from littlebot.decide import get_decider
from littlebot.testing import decision_answers, use_decision_model


async def run(box: RobotToolbox, name: str, **args: Any) -> str:
    return await box.run(name, json.dumps(args))


async def test_on_the_best_fitting_video_plays_first(robot):
    model = use_decision_model(robot, decision_answers("video_1"), modes={"videos": "on"})
    box = RobotToolbox(robot, defer=False)
    assert (await run(box, "play_youtube", query="baby shark song")) == "ok: the video 'Baby Shark' started."
    assert robot.media.played == [("video", "020g-0hhCAU", "Baby Shark | Kids Songs", ["XqZsoesa55w"])]
    state, questions = model.asked[0]
    assert state == {"they_asked_for": "baby shark song", "who_watches": "a very young child (age not set)"}
    assert '"Baby Shark | Kids Songs" by Other, 2 min' in questions["video"].options["video_1"]


async def test_shadow_or_unsure_plays_the_first_result(robot, caplog):
    caplog.set_level("INFO", logger="littlebot.decide")
    use_decision_model(robot, decision_answers("video_1"), modes={"videos": "shadow"})
    box = RobotToolbox(robot, defer=False)
    await run(box, "play_youtube", query="baby shark song")
    assert robot.media.played[0][1] == "XqZsoesa55w"
    line = caplog.records[-1].getMessage()  # the log names both videos
    assert "they_asked_for: baby shark song" in line and "jev_picks: Baby Shark | Kids Songs" in line
    assert "littlebot_plays: " in line
    use_decision_model(robot, decision_answers("video_1", 0.2), modes={"videos": "on"})
    await run(RobotToolbox(robot, defer=False), "play_youtube", query="baby shark song")
    assert robot.media.played[1][1] == "XqZsoesa55w"
    assert get_decider(robot).status()["features"]["videos"]["differed"] == 2


async def test_on_a_loose_name_finds_the_activity(robot):
    model = use_decision_model(robot, decision_answers("song_old_macdonald"), modes={"names": "on"})
    box = RobotToolbox(robot, defer=False)
    assert (await run(box, "start_activity", activity="the one with the cows that moo")).startswith("ok:")
    assert robot.activities.started == [("song_old_macdonald", "chatgpt")]
    options = model.asked[0][1]["which"].options
    assert "hidden_one" not in options and "none_of_these" in options


async def test_none_of_these_stays_an_error(robot):
    use_decision_model(robot, decision_answers("none_of_these"), modes={"names": "on"})
    box = RobotToolbox(robot, defer=False)
    assert (await run(box, "start_activity", activity="quantum physics")).startswith("error: there is no activity")


async def test_on_a_loose_name_finds_the_game(robot):
    use_decision_model(robot, decision_answers("bubbles"), modes={"names": "on"})
    box = RobotToolbox(robot, defer=False)
    assert (await run(box, "start_game", game="the popping one")).startswith("ok:")
    assert robot.extras["games"].started == [("bubbles", "chatgpt")]


async def test_a_close_name_asks_nothing(robot):
    model = use_decision_model(robot, decision_answers("song_old_macdonald"), modes={"names": "on"})
    await run(RobotToolbox(robot, defer=False), "start_activity", activity="peekaboo")
    assert model.asked == [] and robot.activities.started[0][0] == "peekaboo"
