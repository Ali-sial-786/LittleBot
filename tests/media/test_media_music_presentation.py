"""The music presentation: play_video(..., presentation="music") reaches the player page (controller -> bridge),
stays with skips, fallbacks and a resume after a chat, and plain videos are unchanged."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from media_testkit import VID_A, VID_B, VID_C  # type: ignore[import-not-found]

from littlebot.media.controller import PRESENTATIONS
from littlebot.media.search import SearchResult

STATIC = Path(__file__).resolve().parents[2] / "littlebot" / "media" / "static"


class Searcher:
    available = True

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        return [SearchResult(VID_A, "Song A", "Band", 200)][:limit]


@pytest.fixture
async def menv(make_env):
    e = make_env(searcher=Searcher())
    await e.media.start()
    yield e
    await e.media.close()


def alts(*vids: str) -> list[dict[str, Any]]:
    return [{"video_id": v, "title": f"Track {v}", "channel": "Band"} for v in vids]


async def test_music_items_carry_the_presentation_to_the_page(menv):
    assert PRESENTATIONS == ("video", "music")
    st = await menv.media.play_video(VID_A, title="Bohemian Rhapsody", channel="Queen", alternatives=alts(VID_B, VID_C),
                                     presentation="music")
    assert st["item"]["presentation"] == "music" and st["item"]["channel"] == "Queen"
    page = menv.poll()["item"]
    assert page["presentation"] == "music" and page["channel"] == "Queen" and page["title"] == "Bohemian Rhapsody"
    await menv.page_event("error", code=150)  # YouTube refused it: the next result is music too
    page = menv.poll()["item"]
    assert page["video_id"] == VID_B and page["presentation"] == "music" and page["channel"] == "Band"
    await menv.media.skip()
    assert menv.poll()["item"]["video_id"] == VID_C and menv.poll()["item"]["presentation"] == "music"


async def test_plain_videos_are_unchanged(menv):
    await menv.media.play_video(VID_A, title="A video")
    item = menv.poll()["item"]
    assert "presentation" not in item and "channel" not in item
    assert set(item) == {"id", "kind", "video_id", "list_id", "start_seconds", "title"}
    await menv.media.play_video(VID_B, title="Odd", presentation="karaoke")  # unknown: a plain video
    assert "presentation" not in menv.poll()["item"]
    await menv.media.play_video(VID_C, title="Plain", presentation="video")
    assert "presentation" not in menv.poll()["item"]


async def test_an_approved_video_played_as_music_keeps_the_stored_item(menv):
    item = menv.add(VID_A, "Our song")
    st = await menv.media.play_video(VID_A, title="x", presentation="music")
    assert st["item"]["id"] == item["id"] and st["item"]["presentation"] == "music"
    assert "presentation" not in menv.media.get_item(item["id"])  # the allowlist itself isn't touched


async def test_music_paused_for_a_chat_comes_back_as_music(menv):
    await menv.media.play_video(VID_A, title="Song A", channel="Band", presentation="music")
    await menv.page_event("playing", video_id=VID_A, player_state=1, current_time=30.0)
    assert await menv.media.hold("voice chat", wait=0.0)
    assert await menv.media.resume()
    page = menv.poll()["item"]
    assert page["presentation"] == "music" and page["start_seconds"] >= 20


def test_the_page_has_a_now_playing_view_that_never_covers_the_player():
    html = (STATIC / "player.html").read_text(encoding="utf-8")
    css = (STATIC / "player.css").read_text(encoding="utf-8")
    js = (STATIC / "player.js").read_text(encoding="utf-8")
    assert 'id="music-view"' in html and 'id="np-title"' in html and 'id="np-channel"' in html
    assert html.index('id="music-view"') < html.index('id="stage"')  # under the player in the page order too
    # the player keeps at least 200x200 px (YouTube's minimum) and sits above the now-playing view
    assert re.search(r"--np-player-w:\s*max\(356px", css) and re.search(r"--np-player-h:\s*max\(200px", css)
    assert re.search(r"#music-view\s*\{[^}]*z-index:\s*1;", css)
    assert re.search(r"body\.music #stage\s*\{[^}]*z-index:\s*2;", css)
    assert "item.presentation === 'music'" in js and "textContent" in js and "innerHTML = String" not in js
