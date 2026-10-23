"""YouTube search (yt-dlp, no API key) and playing any found video in the existing player."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from media_testkit import VID_A, VID_B, VID_C  # type: ignore[import-not-found]

from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.media.search import (
    SearchError, SearchResult, YtDlpSearcher, clean_query, parse_results, ytdlp_command,
)


class FakeSearcher:
    available = True

    def __init__(self, results: list[SearchResult] | None = None, error: SearchError | None = None) -> None:
        self.results = results if results is not None else [
            SearchResult(VID_A, "Baby Shark Dance", "Pinkfong", 137),
            SearchResult(VID_B, "Baby Shark | Kids Songs", "Other", 95),
            SearchResult(VID_C, "Baby Shark Remix", "", None),
        ]
        self.error = error
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        self.queries.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.results[:limit]


@pytest.fixture
async def senv(make_env):
    e = make_env(searcher=FakeSearcher())
    await e.media.start()
    yield e
    await e.media.close()


def alternatives(*vids: str) -> list[dict[str, Any]]:
    return [{"video_id": v, "title": f"Video {v}"} for v in vids]


# ------------------------------------------------------------------ search
async def test_search_returns_plain_results(senv):
    found = await senv.media.search("  baby\nshark  ")
    assert found[0] == {"video_id": VID_A, "title": "Baby Shark Dance", "channel": "Pinkfong", "duration_seconds": 137}
    assert len(found) == 3 and senv.media.searcher.queries == [("baby shark", 5)]  # media.search_results
    assert len(await senv.media.search("baby shark", limit=1)) == 1
    st = senv.media.status()
    assert st["search"] == {"allowed": True, "available": True}


async def test_search_is_refused_when_switched_off_offline_or_videos_off(make_env):
    e = make_env(searcher=FakeSearcher(), media_cfg={"allow_search": False})
    with pytest.raises(PermissionError, match="turned off"):
        await e.media.search("cats")
    with pytest.raises(PermissionError, match="approved list"):
        await e.media.play_video(VID_A, title="x")
    off = make_env(searcher=FakeSearcher(), mode="offline")
    with pytest.raises(PermissionError, match="offline"):
        await off.media.search("cats")
    e2 = make_env(searcher=FakeSearcher())
    e2.ctx.settings.set("media.enabled", False)
    with pytest.raises(PermissionError, match="turned off"):
        await e2.media.search("cats")
    e2.ctx.settings.set("media.enabled", True)
    with pytest.raises(SearchError):
        await e2.media.search("")  # nothing to search for


# ------------------------------------------------------------------ playing a found video
async def test_a_found_video_plays_in_the_normal_player(senv):
    st = await senv.media.play_video(VID_B, title="Baby Shark | Kids Songs", channel="Other",
                                     alternatives=alternatives(VID_C))
    assert st["state"] == "loading" and st["item"]["id"] == f"search:{VID_B}"
    assert st["item"]["youtube_id"] == VID_B and st["item"]["source"] == "search" and st["queued"] == 1
    assert senv.ctx.state.state == RobotState.PLAYING_MEDIA
    desired = senv.poll()
    assert desired["action"] == "play" and desired["item"]["video_id"] == VID_B and desired["item"]["kind"] == "video"
    started = senv.last(Topics.MEDIA_STARTED)
    assert started.data["item_id"] == f"search:{VID_B}" and started.data["title"] == "Baby Shark | Kids Songs"
    await senv.page_event("playing", video_id=VID_B)  # the player confirms the very video: verified
    assert senv.media.status()["state"] == "playing"
    await senv.page_event("playing", video_id=VID_A)  # anything else in the player still ends it
    assert senv.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert senv.media.list_items() == []  # nothing was added to the approved list
    assert senv.ctx.state.state == RobotState.IDLE


async def test_time_limits_and_the_age_gate_still_apply(make_env):
    e = make_env(searcher=FakeSearcher(), age=12)  # under the 18-month video age
    with pytest.raises(PermissionError, match="under 18 months"):
        await e.media.play_video(VID_A, title="x")
    assert e.media.video_blocked_reason() and "under 18 months" in e.media.video_blocked_reason()
    e2 = make_env(searcher=FakeSearcher())
    await e2.media.start()
    e2.ctx.settings.set("media.daily_limit_minutes", 1)
    assert e2.media.video_blocked_reason() is None
    await e2.media.play_video(VID_A, title="x")
    e2.clock.advance(61)
    await e2.media.tick()
    assert e2.last(Topics.MEDIA_STOPPED).data["reason"] == "limit"
    with pytest.raises(PermissionError, match="used up"):
        await e2.media.play_video(VID_A, title="x")
    assert "used up" in e2.media.video_blocked_reason()
    await e2.media.close()


async def test_a_video_on_the_approved_list_plays_as_that_item(senv):
    item = senv.add(VID_A, "Our song")
    st = await senv.media.play_video(VID_A, title="Something else")
    assert st["item"]["id"] == item["id"] and st["item"]["title"] == "Our song"
    assert senv.media.get_item(item["id"])["play_count"] == 1


async def test_youtube_refusing_a_result_tries_the_next_one(senv):
    await senv.media.play_video(VID_A, title="A", alternatives=alternatives(VID_B, VID_C))
    first_load = senv.poll()["load_id"]
    await senv.page_event("error", code=150)  # embedding not allowed: the next result instead
    st = senv.media.status()
    assert st["state"] == "loading" and st["item"]["youtube_id"] == VID_B and st["queued"] == 1
    assert senv.poll()["load_id"] != first_load and senv.poll()["item"]["video_id"] == VID_B
    assert senv.last(Topics.MEDIA_STARTED).data["skipped"] is True
    assert senv.last(Topics.MEDIA_ERROR).data["code"] == 150
    await senv.page_event("error", code="101")
    assert senv.media.status()["item"]["youtube_id"] == VID_C
    await senv.page_event("error", code=100)  # nothing left: stop as before
    assert senv.media.status()["state"] == "idle"
    assert senv.last(Topics.MEDIA_STOPPED).data["reason"] == "error"


async def test_other_player_errors_stop_at_once(senv):
    await senv.media.play_video(VID_A, title="A", alternatives=alternatives(VID_B))
    await senv.page_event("error", code="api_load_failed")
    assert senv.media.status()["state"] == "idle"


async def test_skip_moves_through_the_search_results(senv):
    other = senv.add(VID_C, "Approved")
    await senv.media.play_video(VID_A, title="A", alternatives=alternatives(VID_B, VID_A))  # duplicates dropped
    assert senv.media.status()["queued"] == 1
    st = await senv.media.skip()
    assert st["item"]["youtube_id"] == VID_B and st["queued"] == 0
    st = await senv.media.skip()  # results used up: the approved list, as before
    assert st["item"]["id"] == other["id"]


async def test_playing_an_approved_item_clears_the_results(senv):
    item = senv.add(VID_C, "Approved")
    await senv.media.play_video(VID_A, title="A", alternatives=alternatives(VID_B))
    st = await senv.media.play(item["id"])
    assert st["item"]["id"] == item["id"] and st["queued"] == 0


async def test_bad_ids_are_refused(senv):
    with pytest.raises(ValueError):
        await senv.media.play_video("not-an-id")
    with pytest.raises(ValueError):
        await senv.media.play_video("https://evil.example/x")


async def test_stop_all_stops_a_found_video(senv):
    await senv.media.play_video(VID_A, title="A")
    senv.ctx.bus.publish(Topics.STOP_ALL, {"source": "test"})
    await senv.settle()
    assert senv.media.status()["state"] == "idle" and senv.ctx.state.state == RobotState.IDLE


# ------------------------------------------------------------------ yt-dlp output and process
YTDLP_JSON = {
    "_type": "playlist", "id": "baby shark", "entries": [
        {"_type": "url", "ie_key": "Youtube", "id": VID_A, "title": "Baby Shark Dance‮", "channel": "Pinkfong",
         "duration": 137.0, "url": f"https://www.youtube.com/watch?v={VID_A}"},
        {"_type": "url", "id": "bad", "title": "not a video id"},
        {"_type": "url", "id": VID_B, "title": "Live soon", "live_status": "is_upcoming"},
        {"_type": "url", "id": VID_C, "title": None, "uploader": "Someone", "duration": None},
        {"_type": "url", "id": VID_A, "title": "duplicate"},
    ]}


def test_parse_results():
    found = parse_results(json.dumps(YTDLP_JSON))
    assert found == [SearchResult(VID_A, "Baby Shark Dance", "Pinkfong", 137),
                     SearchResult(VID_C, "YouTube video", "Someone", None)]
    assert parse_results(json.dumps(YTDLP_JSON), limit=1) == found[:1]
    assert parse_results("{}") == []
    with pytest.raises(SearchError) as e:
        parse_results("not json")
    assert e.value.kind == "failed"


def test_clean_query():
    assert clean_query("  baby \x00 shark\n") == "baby shark"
    assert len(clean_query("a" * 500)) == 100
    with pytest.raises(SearchError):
        clean_query(" \n ")


FAKE_YTDLP = (
    "import json, sys\n"
    "args = sys.argv[1:]\n"
    "assert '--flat-playlist' in args and '--dump-single-json' in args and '--ignore-config' in args\n"
    "query = args[-1]\n"
    "if 'slow' in query:\n"
    "    import time; time.sleep(5)\n"
    "if 'broken' in query:\n"
    "    print('ERROR: nope', file=sys.stderr); sys.exit(1)\n"
    "print(json.dumps({'entries': [{'id': 'XqZsoesa55w', 'title': query, 'channel': 'c', 'duration': 60}]}))\n"
)


async def test_the_ytdlp_process_gets_one_search_argument(tmp_path):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE_YTDLP)
    s = YtDlpSearcher([sys.executable, str(script)], timeout=10)
    assert s.available
    (found,) = await s.search("--exec rm baby shark", limit=3)
    assert found.title == "ytsearch3:--exec rm baby shark"  # always a search, never an option
    with pytest.raises(SearchError) as e:
        await s.search("broken")
    assert e.value.kind == "failed"


async def test_a_hanging_search_times_out(tmp_path):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE_YTDLP)
    with pytest.raises(SearchError) as e:
        await YtDlpSearcher([sys.executable, str(script)], timeout=0.5).search("slow")
    assert e.value.kind == "timeout"


async def test_no_ytdlp_installed(monkeypatch):
    import littlebot.media.search as search

    monkeypatch.setattr(search.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    assert ytdlp_command() is None
    s = YtDlpSearcher()
    assert not s.available
    with pytest.raises(SearchError) as e:
        await s.search("cats")
    assert e.value.kind == "unavailable"
    assert ytdlp_command("/opt/yt-dlp --proxy x") == ["/opt/yt-dlp", "--proxy", "x"]


# ------------------------------------------------------------------ the AI's tools with the real controller
async def test_the_ai_tools_search_and_play_through_the_controller(senv):
    import json as _json

    from littlebot.chatgpt.tools import RobotToolbox

    senv.ctx.media = senv.media
    box = RobotToolbox(senv.ctx, mode="chat", defer=True)
    found = _json.loads(await box.run("search_youtube", '{"query": "baby shark"}'))
    assert [f["video_id"] for f in found] == [VID_A, VID_B, VID_C]
    result = await box.run("play_youtube", _json.dumps({"video_id": VID_B}))
    assert result.startswith("ok: the video 'Baby Shark' will start")
    assert senv.media.status()["state"] == "idle"  # after the reply
    assert await box.run_pending() == [f"video {VID_B}"]
    st = senv.media.status()
    assert st["item"]["id"] == f"search:{VID_B}" and st["item"]["title"] == "Baby Shark | Kids Songs"
    assert st["queued"] == 2  # the other results: skip / fallback
    assert (await box.run("stop_video", "{}")) == "ok: the video stopped"
    assert senv.media.status()["state"] == "idle"
    senv.ctx.settings.set("media.daily_limit_minutes", 0)
    assert (await box.run("play_youtube", '{"query": "baby shark"}')).startswith("error: The video time limit")
