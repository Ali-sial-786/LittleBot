"""LittleBot's toolbox (littlebot/chatgpt/tools.py): every tool against fakes (no network, no devices)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from littlebot.chatgpt.tools import TOOL_NAMES, RobotToolbox, speakable_title, wifi_status
from littlebot.core.state import RobotState


class FakeMedia:
    """Stands in for MediaController (search + play any video + the approved list)."""

    def __init__(self, *, allow_search: bool = True, blocked: str | None = None,
                 results: list[dict[str, Any]] | None = None, approved: list[dict[str, Any]] | None = None,
                 search_error: Exception | None = None) -> None:
        self.allow_search, self.blocked, self.search_error = allow_search, blocked, search_error
        self.results = results if results is not None else [
            {"video_id": "XqZsoesa55w", "title": "Baby Shark Dance | #babyshark Most Viewed Video",
             "channel": "Pinkfong", "duration_seconds": 137},
            {"video_id": "020g-0hhCAU", "title": "Baby Shark | Kids Songs", "channel": "Other", "duration_seconds": 95},
        ]
        self.approved = approved if approved is not None else [
            {"id": 3, "kind": "video", "youtube_id": "dQw4w9WgXcQ", "title": "Twinkle Twinkle", "enabled": True}]
        self.state, self.item, self.volume = "idle", None, 0.5
        self.searches: list[tuple[str, Any]] = []
        self.played: list[tuple[Any, ...]] = []
        self.stops = 0

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "item": self.item, "volume": self.volume}

    def search_allowed(self) -> bool:
        return self.allow_search

    def video_blocked_reason(self) -> str | None:
        return self.blocked

    async def search(self, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        self.searches.append((query, limit))
        if self.search_error is not None:
            raise self.search_error
        return [dict(r) for r in self.results][: limit or 5]

    async def play_video(self, video_id: str, *, title: str | None = None, channel: str | None = None,
                         duration_seconds: int | None = None, alternatives: list[dict[str, Any]] | None = None,
                         parent_override: bool = False) -> dict[str, Any]:
        self.played.append(("video", video_id, title, [a["video_id"] for a in alternatives or []]))
        self.state, self.item = "loading", {"id": f"search:{video_id}", "title": title}
        return self.status()

    async def play(self, item_id: int, *, parent_override: bool = False) -> dict[str, Any]:
        self.played.append(("approved", item_id))
        self.state, self.item = "loading", {"id": item_id, "title": "Twinkle Twinkle"}
        return self.status()

    async def stop(self, reason: str = "stopped") -> None:
        self.stops += 1
        self.state, self.item = "idle", None

    async def pause(self) -> bool:
        if self.state != "playing":
            return False
        self.state = "paused"
        return True

    async def resume(self) -> bool:
        if self.state != "paused":
            return False
        self.state = "playing"
        return True

    def set_volume(self, volume: float) -> float:
        self.volume = round(min(0.8, max(0.0, volume)), 3)
        return self.volume

    def list_items(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self.approved]


ACTIVITIES = [
    {"id": "peekaboo", "name": "Peekaboo", "category": "social", "age_range": "6–24 months", "duration_seconds": 120,
     "enabled": True, "tags": ["baby", "classic"], "suitable": True},
    {"id": "song_old_macdonald", "name": "Old MacDonald Had a Farm", "category": "music", "age_range": "9 months–6 years",
     "duration_seconds": 210, "enabled": True, "tags": ["song"], "suitable": True},
    {"id": "song_wheels_on_the_bus", "name": "The Wheels on the Bus", "category": "music", "age_range": "9 months–6 years",
     "duration_seconds": 180, "enabled": True, "tags": ["song"], "suitable": True},
    {"id": "hidden_one", "name": "Hidden", "category": "music", "enabled": False, "tags": []},
]


class FakeActivities:
    def __init__(self) -> None:
        self.running: str | None = None
        self.started: list[tuple[str, str]] = []
        self.stops: list[str] = []

    def list(self, age_months: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
        return [dict(a) for a in ACTIVITIES if category is None or a["category"] == category]

    async def start(self, activity_id: str, *, source: str = "dashboard") -> dict[str, Any]:
        self.started.append((activity_id, source))
        self.running = activity_id
        return self.status()

    async def stop(self, reason: str = "stopped") -> None:
        self.stops.append(reason)
        self.running = None

    def status(self) -> dict[str, Any]:
        return {"running": self.running is not None, "name": self.running}


class FakeGames:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.running = False
        self.started: list[tuple[str, str]] = []

    def list(self) -> list[dict[str, Any]]:
        return [{"id": "bubbles", "name": "Pop the Bubbles", "description": "Tap the bubbles."},
                {"id": "colours", "name": "Colour Match", "description": "Find the colour."}]

    async def start(self, game_id: str, *, source: str = "voice") -> bool:
        self.started.append((game_id, source))
        self.running = self.ok
        return self.ok

    def stop(self) -> bool:
        was, self.running = self.running, False
        return was

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "last_error": None if self.ok else "LittleBot is busy right now."}


class FakeLook:
    def __init__(self, answer: str | None = "You are holding a red ball.", detail: str | None = None) -> None:
        self.answer, self.detail = answer, detail
        self.asked: list[tuple[str, str]] = []

    async def look(self, question: str, *, asked_by: str = "parent") -> dict[str, Any]:
        self.asked.append((question, asked_by))
        if self.answer is None:
            return {"ok": False, "answer": None, "error": "ai_camera_off", "detail": self.detail}
        return {"ok": True, "answer": self.answer, "error": None, "detail": None}


@pytest.fixture
def robot(ctx):
    ctx.media = FakeMedia()
    ctx.activities = FakeActivities()
    ctx.extras["games"] = FakeGames()
    return ctx


async def run(box: RobotToolbox, name: str, **args: Any) -> str:
    return await box.run(name, json.dumps(args))


def test_specs_are_json_schema_functions_and_live_has_no_camera_tool(ctx):
    chat = RobotToolbox(ctx, mode="chat").specs()
    live = RobotToolbox(ctx, mode="live").specs()
    functions = [s for s in chat if s.get("type", "function") == "function"]
    names = {s["name"] for s in functions}
    assert names == TOOL_NAMES
    assert {"get_robot_status", "describe_yourself", "start_activity", "set_volume", "volume_up", "set_face",
            "go_to_sleep", "wake_up", "stop_everything", "search_youtube", "play_youtube", "stop_video",
            "start_game", "look_through_camera", "get_weather", "get_time"} <= names
    assert "look_through_camera" not in {s["name"] for s in live}  # the live call looks through its own path
    assert {"get_weather", "get_time"} <= {s["name"] for s in live}
    for spec in functions:
        assert spec["parameters"]["type"] == "object" and spec["description"]
        json.dumps(spec)
    # OpenAI's own web search (a hosted tool) comes last, in chat and live, while chatgpt.web_search is on
    assert chat[-1] == live[-1] == {"type": "web_search", "name": "web_search"}


async def test_unknown_tools_and_bad_arguments_are_errors(ctx):
    box = RobotToolbox(ctx)
    assert (await box.run("rm_rf", "{}")).startswith("error: there is no tool")
    assert (await box.run("set_volume", "{not json")).startswith("error:")
    assert (await RobotToolbox(ctx, mode="live").run("look_through_camera", "{}")).startswith("error:")


async def test_a_crashing_tool_returns_an_error(robot, monkeypatch):
    async def boom(*_a: Any) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(robot.media, "search", boom)
    box = RobotToolbox(robot)
    assert await run(box, "search_youtube", query="cats") == "error: the action failed"
    assert box.calls == ["search_youtube"]


async def test_robot_status(robot):
    data = json.loads(await run(RobotToolbox(robot), "get_robot_status"))
    assert data["name"] == robot.config.robot_name and data["doing"] == "idle"
    assert data["system"]["cpu_temperature_c"] == 48.0 and data["system"]["memory_used_percent"] == 35.0
    assert data["devices"]["microphone"] == "ok" and data["devices"]["speaker"] == "ok"
    assert data["devices"]["camera"] == "not connected"
    assert data["chatgpt"] == {"signed_in": False, "plan": None}
    assert data["live_voice"] == "off" and data["volume_percent"]["speaker"] == 50
    assert data["video"] == {"state": "idle"} and data["activity"] is None
    assert data["game"]["running"] is False
    assert set(data["privacy"]) == {"camera_enabled", "microphone_enabled", "cloud_enabled", "ai_camera_enabled"}
    assert "wifi" in data and "connected" in data["wifi"]


async def test_describe_yourself(robot):
    text = await run(RobotToolbox(robot), "describe_yourself")
    assert robot.config.robot_name in text and "peekaboo" in text and "3 activities" not in text
    assert "4 activities" in text and "find and play videos" in text and "Pop the Bubbles" in text
    assert "robot, not a person" in text


async def test_activities_list_start_later_and_stop(robot):
    box = RobotToolbox(robot)
    listed = json.loads(await run(box, "list_activities"))
    assert [a["id"] for a in listed] == ["peekaboo", "song_old_macdonald", "song_wheels_on_the_bus"]  # not disabled
    assert json.loads(await run(box, "list_activities", category="music"))[0]["id"] == "song_old_macdonald"
    result = await run(box, "start_activity", activity="Peekaboo")
    assert result.startswith("ok:") and "as soon as you have finished speaking" in result
    assert robot.activities.started == []  # the reply is spoken first
    assert await box.run_pending() == ["activity peekaboo"]
    assert robot.activities.started == [("peekaboo", "chatgpt")]
    assert (await run(box, "stop_activity")).startswith("ok: stopped")
    assert robot.activities.running is None
    assert await run(box, "stop_activity") == "ok: no activity is running"


async def test_activity_names_are_matched_loosely(robot):
    box = RobotToolbox(robot, defer=False)
    assert (await run(box, "start_activity", activity="old macdonald")).startswith("ok:")
    assert (await run(box, "start_activity", activity="the bus song")).startswith("ok:")
    assert [a for a, _ in robot.activities.started] == ["song_old_macdonald", "song_wheels_on_the_bus"]
    assert (await run(box, "start_activity", activity="quantum physics")).startswith("error: there is no activity")
    assert (await run(box, "start_activity", activity="hidden")).startswith("error:")  # switched off by the parent


def test_the_real_activity_library_matches_peekaboo(ctx):
    from littlebot.activities.engine import ActivityEngine
    from littlebot.chatgpt.tools import _best_match

    items = ActivityEngine(ctx).list()
    assert _best_match("peekaboo", items, extra=("tags",))["id"] == "peekaboo"
    assert _best_match("wheels on the bus", items, extra=("tags",))["id"] == "song_wheels_on_the_bus"


async def test_volume(robot):
    box = RobotToolbox(robot)
    assert (await run(box, "set_volume", percent=70)) == "ok: speaker volume is now 70%; video volume is now 70%"
    assert robot.audio.volume == 0.7 and robot.media.volume == 0.7
    assert (await run(box, "volume_up")).startswith("ok: speaker volume is now 80% (the most allowed)")
    assert robot.audio.volume == 0.8  # never above the configured maximum
    await run(box, "volume_down", step=30)
    assert robot.audio.volume == pytest.approx(0.5) and robot.media.volume == pytest.approx(0.5)
    await run(box, "set_volume", percent=20, what="video")
    assert robot.audio.volume == pytest.approx(0.5) and robot.media.volume == 0.2
    assert json.loads(await run(box, "get_volume")) == {"speaker": 50, "speaker_max": 80, "video": 20, "video_max": 80}
    assert (await run(box, "set_volume", percent="loud")).startswith("error:")


async def test_volume_without_an_audio_service_sets_the_setting(ctx):
    class LentAudio:  # the audio service lent to the live voice: no set_volume
        def status(self) -> dict[str, Any]:
            return {}

    ctx.audio = LentAudio()
    box = RobotToolbox(ctx, mode="live")
    await run(box, "set_volume", percent=30, what="speaker")
    assert ctx.settings.get("audio.volume") == 0.3  # the live call reads it
    await run(box, "volume_up")
    assert ctx.settings.get("audio.volume") == pytest.approx(0.4)


async def test_faces(robot):
    chat = RobotToolbox(robot, mode="chat")
    assert (await run(chat, "set_face", face="surprised")).startswith("ok: LittleBot will show surprised")
    assert robot.face.expression != "surprised"  # after the spoken reply
    await chat.run_pending()
    assert robot.face.expression == "surprised"
    live = RobotToolbox(robot, mode="live")
    assert (await run(live, "set_face", face="happy face")).startswith("ok: LittleBot is showing happy")
    assert robot.face.expression == "happy"
    assert (await run(live, "set_face", face="volcano")).startswith("error: unknown face")


async def test_sleep_and_wake(robot):
    box = RobotToolbox(robot)
    assert (await run(box, "go_to_sleep")).startswith("ok: sleeping will start")
    assert robot.state.state == RobotState.IDLE
    assert await box.run_pending() == ["sleep"]
    assert robot.state.state == RobotState.SLEEPING
    assert await run(box, "go_to_sleep") == "ok: LittleBot is already asleep"
    assert await run(box, "wake_up") == "ok: LittleBot woke up"
    assert robot.state.state == RobotState.IDLE
    assert await run(box, "wake_up") == "ok: LittleBot is awake"


async def test_stop_everything_stops_videos_activities_games_and_what_was_queued(robot):
    robot.media.state = "playing"
    robot.activities.running = "peekaboo"
    robot.extras["games"].running = True
    box = RobotToolbox(robot)
    await run(box, "start_activity", activity="peekaboo")
    result = await run(box, "stop_everything")
    assert result == "ok: stopped what was about to start, the video, the activity, the game"
    assert robot.media.stops == 1 and robot.activities.running is None and not robot.extras["games"].running
    assert box.pending == [] and robot.state.state == RobotState.IDLE
    assert await run(box, "stop_everything") == "ok: nothing was running"


async def test_search_youtube(robot):
    box = RobotToolbox(robot)
    found = json.loads(await run(box, "search_youtube", query="baby shark", limit=2))
    assert found[0] == {"video_id": "XqZsoesa55w", "title": "Baby Shark Dance | #babyshark Most Viewed Video",
                        "channel": "Pinkfong", "minutes": 2.3}
    assert robot.media.searches == [("baby shark", 2)]
    assert (await run(box, "search_youtube", query="")).startswith("error:")


async def test_search_problems_fall_back_to_the_approved_list(robot):
    from littlebot.media.search import SearchError

    robot.media.search_error = SearchError("unavailable")
    box = RobotToolbox(robot)
    assert (await run(box, "search_youtube", query="baby shark")) == \
        "error: video search isn't installed on this robot (yt-dlp)"
    found = json.loads(await run(box, "search_youtube", query="twinkle star"))
    assert found == [{"video_id": "approved:3", "title": "Twinkle Twinkle", "approved": True}]
    robot.media.allow_search = False
    assert (await run(box, "search_youtube", query="dinosaurs")).startswith("error: only the videos on the approved")


async def test_play_youtube_by_query_starts_after_the_reply(robot):
    box = RobotToolbox(robot)
    result = await run(box, "play_youtube", query="baby shark song")
    assert result == ("ok: the video 'Baby Shark Dance' will start as soon as you have finished speaking. "
                      "Tell them briefly.")
    assert robot.media.played == []
    assert await box.run_pending() == ["video XqZsoesa55w"]
    # the other results come along: skip, or the next one if YouTube won't play this one
    assert robot.media.played == [("video", "XqZsoesa55w", "Baby Shark Dance | #babyshark Most Viewed Video",
                                   ["020g-0hhCAU"])]


async def test_play_youtube_by_id_uses_the_search_titles(robot):
    box = RobotToolbox(robot, defer=False)
    await run(box, "search_youtube", query="baby shark")
    assert (await run(box, "play_youtube", video_id="020g-0hhCAU")) == "ok: the video 'Baby Shark' started."
    assert robot.media.played == [("video", "020g-0hhCAU", "Baby Shark | Kids Songs", ["XqZsoesa55w"])]
    await run(box, "play_youtube", video_id="https://www.youtube.com/watch?v=dQw4w9WgXcQ")  # a pasted link
    assert robot.media.played[-1][:2] == ("video", "dQw4w9WgXcQ")
    assert (await run(box, "play_youtube", video_id="not a video")).startswith("error:")
    assert (await run(box, "play_youtube")).startswith("error: give a video_id or a query")


async def test_play_an_approved_video(robot):
    box = RobotToolbox(robot, defer=False)
    listed = json.loads(await run(box, "list_approved_videos"))
    assert listed == [{"video_id": "approved:3", "title": "Twinkle Twinkle", "approved": True}]
    assert (await run(box, "play_youtube", video_id="approved:3")).startswith("ok: the video 'Twinkle Twinkle'")
    assert robot.media.played == [("approved", 3)]
    assert (await run(box, "play_youtube", video_id="approved:99")).startswith("error:")


async def test_videos_that_cannot_play_are_errors_at_once(robot):
    robot.media.blocked = "Today's video time (30 minutes) is used up. It resets tomorrow."
    box = RobotToolbox(robot)
    assert await run(box, "play_youtube", query="baby shark") == \
        "error: Today's video time (30 minutes) is used up. It resets tomorrow."
    assert box.pending == []
    robot.media.blocked = None
    robot.media.allow_search = False
    assert (await run(box, "play_youtube", video_id="XqZsoesa55w")).startswith("error: only the videos")


async def test_only_the_last_takeover_runs(robot):
    box = RobotToolbox(robot)
    await run(box, "start_activity", activity="peekaboo")
    await run(box, "play_youtube", query="baby shark")
    await run(box, "set_face", face="happy")  # not a take-over: kept as well
    assert await box.run_pending() == ["video XqZsoesa55w", "face happy"]
    assert robot.activities.started == []
    assert (await run(box, "stop_video")) == "ok: the video stopped"


async def test_video_controls(robot):
    box = RobotToolbox(robot)
    assert await run(box, "stop_video") == "ok: no video is playing"
    assert await run(box, "pause_video") == "ok: no video is playing"
    robot.media.state = "playing"
    assert await run(box, "pause_video") == "ok: the video is paused"
    assert await run(box, "resume_video") == "ok: the video carries on"
    await run(box, "play_youtube", query="baby shark")
    assert (await run(box, "stop_video")) == "ok: the video stopped" and box.pending == []


async def test_games(robot):
    box = RobotToolbox(robot)
    games = robot.extras["games"]
    assert [g["id"] for g in json.loads(await run(box, "list_games"))] == ["bubbles", "colours"]
    assert (await run(box, "start_game", game="bubbles")) == "ok: the game 'Pop the Bubbles' started on the screen"
    assert games.started == [("bubbles", "chatgpt")]  # at once: games share the speaker with a conversation
    assert (await run(box, "start_game", game="colour")).startswith("ok:")
    assert (await run(box, "start_game", game="chess")).startswith("error: there is no game")
    assert await run(box, "stop_game") == "ok: the game stopped"
    assert await run(box, "stop_game") == "ok: no game is running"
    robot.extras["games"] = FakeGames(ok=False)
    assert await run(box, "start_game", game="bubbles") == \
        "error: the game 'Pop the Bubbles' couldn't start: LittleBot is busy right now."


async def test_games_unavailable(ctx):
    box = RobotToolbox(ctx)
    for name, args in (("list_games", {}), ("start_game", {"game": "x"}), ("stop_game", {})):
        assert (await run(box, name, **args)).startswith("error: games aren't available")


async def test_games_on_the_context_attribute_are_used(ctx):
    games = FakeGames()
    ctx.games = games  # type: ignore[attr-defined] - the games agent may add ctx.games
    try:
        assert (await run(RobotToolbox(ctx), "start_game", game="bubbles")).startswith("ok:")
        assert games.started
    finally:
        del ctx.games


async def test_look_through_the_camera(ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    look = ctx.extras["camera_look"] = FakeLook()
    box = RobotToolbox(ctx)
    assert await run(box, "look_through_camera", question="what am I holding?") == \
        "ok: the camera saw: You are holding a red ball."
    assert look.asked == [("what am I holding?", "chat")]
    ctx.extras["camera_look"] = FakeLook(answer=None, detail="The AI camera is off in Privacy.")
    result = await run(box, "look_through_camera", question="what is this?")
    assert result.startswith("error: The AI camera is off in Privacy.") and "Don't guess" in result


async def test_nothing_attached_gives_friendly_errors(ctx):
    ctx.media = None
    box = RobotToolbox(ctx)
    for name, args in (("search_youtube", {"query": "x"}), ("play_youtube", {"query": "x"}), ("stop_video", {}),
                       ("list_activities", {}), ("start_activity", {"activity": "x"})):
        assert (await run(box, name, **args)).startswith("error:"), name
    assert (await run(box, "get_robot_status")).startswith("{")


def test_speakable_titles():
    assert speakable_title("Baby Shark Dance | #babyshark Most Viewed Video") == "Baby Shark Dance"
    assert speakable_title("Wheels on the Bus - Nursery Rhymes") == "Wheels on the Bus"
    assert speakable_title("Peppa Pig (Official)") == "Peppa Pig"
    assert speakable_title("Best YouTube songs") == "Best YouTube songs"  # brand words are fine now
    assert speakable_title("Our little secret song") == "the video"  # words the reply checks would stop
    assert speakable_title("") == "the video" and speakable_title(None) == "the video"


def test_wifi_status_reads_the_wireless_table(tmp_path, monkeypatch):
    import littlebot.chatgpt.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    table = tmp_path / "wireless"
    table.write_text("Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
                     " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
                     " wlan0: 0000   56.  -54.  -256        0      0      0      0      0        0\n")
    assert wifi_status(table) == {"connected": True, "interface": "wlan0", "quality_percent": 80, "signal_dbm": -54.0}
    assert wifi_status(tmp_path / "missing")["connected"] is False
