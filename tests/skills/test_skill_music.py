"""The music skill (littlebot/skills/music.py): search words, picking a result, the deferred start, the music
presentation, music_control and the context note. Against a fake media controller."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from littlebot.core.state import RobotState
from littlebot.media.search import SearchError
from littlebot.skills import takeover
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.skills.music import Music, guess_kind, music_query, score, speakable
from littlebot.testing import make_test_context

SONGS = [
    {"video_id": "aaaaaaaaaa1", "title": "Bohemian Rhapsody (Reaction!)", "channel": "Reacts", "duration_seconds": 900},
    {"video_id": "aaaaaaaaaa2", "title": "Queen - Bohemian Rhapsody (Official Video Remastered)",
     "channel": "Queen Official", "duration_seconds": 360},
    {"video_id": "aaaaaaaaaa3", "title": "Bohemian Rhapsody", "channel": "Queen - Topic", "duration_seconds": 355},
]
MIXES = [
    {"video_id": "bbbbbbbbbb1", "title": "Jazz in 5 minutes explained", "channel": "Edu", "duration_seconds": 300},
    {"video_id": "bbbbbbbbbb2", "title": "Relaxing Jazz Music Mix | 3 Hours", "channel": "Cafe",
     "duration_seconds": 10800},
    {"video_id": "bbbbbbbbbb3", "title": "Smooth Jazz Mix", "channel": "Cafe", "duration_seconds": 3600},
]


class FakeMedia:
    """Stands in for MediaController: search, play_video (with presentation), skip, pause/resume, hold."""

    def __init__(self, *, results: list[dict[str, Any]] | None = None, blocked: str | None = None,
                 search_error: Exception | None = None, allow_search: bool = True,
                 play_error: Exception | None = None) -> None:
        self.results = list(results if results is not None else SONGS)
        self.blocked, self.search_error, self.allow_search, self.play_error = blocked, search_error, allow_search, \
            play_error
        self.state, self.item, self.volume, self.held = "idle", None, 0.5, None
        self.queue: list[dict[str, Any]] = []
        self.searches: list[tuple[str, Any]] = []
        self.played: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "item": self.item, "volume": self.volume, "held": self.held,
                "queued": len(self.queue)}

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
                         parent_override: bool = False, presentation: str | None = None) -> dict[str, Any]:
        if self.play_error is not None:
            raise self.play_error
        self.played.append({"video_id": video_id, "title": title, "channel": channel, "presentation": presentation,
                            "alternatives": [a["video_id"] for a in alternatives or []]})
        self.state = "playing"
        self.item = {"id": f"search:{video_id}", "title": title, "channel": channel,
                     **({"presentation": presentation} if presentation else {})}
        self.queue = [dict(a) for a in alternatives or []]
        self.held = None
        return self.status()

    async def skip(self) -> dict[str, Any]:
        self.calls.append("skip")
        nxt = self.queue.pop(0)
        self.item = {"id": f"search:{nxt['video_id']}", "title": nxt.get("title"), "presentation": "music"}
        return self.status()

    async def stop(self, reason: str = "stopped") -> None:
        self.calls.append("stop")
        self.state, self.item, self.held = "idle", None, None

    async def pause(self) -> bool:
        if self.state != "playing":
            return False
        self.state = "paused"
        return True

    async def resume(self) -> bool:
        self.calls.append("resume")
        if self.state != "paused":
            return False
        self.state, self.held = "playing", None
        return True

    def set_volume(self, volume: float) -> float:
        self.volume = round(min(0.8, max(0.0, volume)), 3)
        return self.volume

    def list_items(self) -> list[dict[str, Any]]:
        return []


class OldMedia(FakeMedia):
    """A media controller from before the music presentation (no ``presentation`` keyword)."""

    async def play_video(  # type: ignore[override]
            self, video_id: str, *, title: str | None = None, channel: str | None = None,
            duration_seconds: int | None = None, alternatives: list[dict[str, Any]] | None = None,
            parent_override: bool = False) -> dict[str, Any]:
        return await super().play_video(video_id, title=title, channel=channel, alternatives=alternatives)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(takeover, "QUIET_S", 0.02)
    monkeypatch.setattr(takeover, "POLL_S", 0.005)
    monkeypatch.setattr(takeover, "MAX_WAIT_S", 1.0)


@pytest.fixture
def ctx():
    c = make_test_context()
    c.media = FakeMedia()
    yield c
    c.db.close()


async def started(ctx: Any) -> None:
    """Let the deferred start happen (nobody claims it)."""
    for _ in range(200):
        if not takeover.pending(ctx):
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)


# ------------------------------------------------------------------ play_music
async def test_a_song_is_found_as_audio_and_starts_after_the_answer(ctx):
    m = Music(ctx)
    ctx.state.transition(RobotState.THINKING, "answering")
    out = await m.run("play_music", {"query": "Bohemian Rhapsody by Queen", "kind": "song"})
    assert out == ("ok: 'Bohemian Rhapsody' by Queen - Topic starts playing as soon as you have finished speaking; "
                   "say it in one short line")
    assert ctx.media.searches == [("Bohemian Rhapsody by Queen official audio", 8)]
    assert takeover.pending(ctx) == ["music aaaaaaaaaa3"] and ctx.media.played == []
    assert m.context_note() == "Music: 'Bohemian Rhapsody' starts once you have finished speaking."
    await asyncio.sleep(0.05)
    assert ctx.media.played == []  # still answering
    ctx.state.transition(RobotState.IDLE, "done")
    await started(ctx)
    played = ctx.media.played[0]
    assert played["video_id"] == "aaaaaaaaaa3" and played["presentation"] == "music"
    assert played["alternatives"] == ["aaaaaaaaaa2", "aaaaaaaaaa1"]  # the reaction video last
    assert m.context_note().startswith("Music: playing 'Bohemian Rhapsody' (Queen - Topic)")


async def test_a_host_can_claim_the_start(ctx):
    ctx.media.results = list(MIXES)
    m = Music(ctx)
    await m.run("play_music", {"query": "jazz"})
    assert ctx.media.searches[-1][0] == "jazz music mix"
    claimed = takeover.claim(ctx)
    assert [c.label for c in claimed] == ["music bbbbbbbbbb2"]
    await asyncio.sleep(0.05)
    assert ctx.media.played == []
    assert await claimed[0].run() is True and ctx.media.played


async def test_a_genre_prefers_a_long_mix(ctx):
    ctx.media.results = list(MIXES)
    m = Music(ctx)
    await m.run("play_music", {"query": "some jazz", "kind": "genre"})
    assert ctx.media.searches[-1][0] == "jazz music mix"
    assert takeover.pending(ctx) == ["music bbbbbbbbbb2"]
    takeover.claim(ctx)


async def test_refusals_never_raise(ctx):
    m = Music(ctx)
    assert await m.run("play_music", {"query": "  "}) == "error: say what music to play"
    ctx.media.blocked = "Videos are turned off in LittleBot's settings."
    assert await m.run("play_music", {"query": "jazz"}) == "error: Videos are turned off in LittleBot's settings."
    ctx.media.blocked = None
    ctx.media.search_error = PermissionError("Videos need the internet, and LittleBot is in offline mode.")
    assert "offline mode" in await m.run("play_music", {"query": "jazz"})
    ctx.media.search_error = SearchError("unavailable")
    assert await m.run("play_music", {"query": "jazz"}) == "error: music search isn't installed on this robot (yt-dlp)"
    ctx.media.search_error = SearchError("timeout")
    assert await m.run("play_music", {"query": "jazz"}) == "error: the music search took too long"
    ctx.media.search_error = None
    ctx.media.results = []
    assert await m.run("play_music", {"query": "jazz"}) == "error: no music found for that; try other words"
    assert takeover.pending(ctx) == []
    assert (await m.run("music_control", {"action": "dance"})).startswith("error: unknown action")
    assert (await m.run("nope", {})).startswith("error:")


async def test_a_start_that_fails_is_said(ctx):
    seen: list[dict[str, Any]] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: seen.append(dict(e.data)))
    ctx.media.play_error = PermissionError("Today's video time (60 minutes) is used up. It resets tomorrow.")
    m = Music(ctx)
    await m.run("play_music", {"query": "jazz"})
    await started(ctx)
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen and seen[0]["kind"] == "music"
    assert seen[0]["text"] == "Sorry, the music couldn't start: Today's video time (60 minutes) is used up. It " \
                              "resets tomorrow."
    assert m.status()["starting"] is None


async def test_an_older_media_controller_plays_it_as_a_video():
    ctx = make_test_context()
    try:
        ctx.media = OldMedia()
        m = Music(ctx)
        await m.run("play_music", {"query": "Queen", "kind": "artist"})
        assert ctx.media.searches[-1][0] == "Queen best songs mix"
        await started(ctx)
        assert ctx.media.played[0]["presentation"] is None
    finally:
        ctx.db.close()


async def test_offered_only_with_media_and_search():
    ctx = make_test_context()
    try:
        m = Music(ctx)
        assert not m.available() and await m.run("play_music", {"query": "x"}) == \
            "error: music isn't available on this robot"
        ctx.media = FakeMedia(allow_search=False)
        assert not m.available()
        ctx.media.allow_search = True
        assert m.available()
        assert [s["name"] for s in SkillHub(ctx, [m]).specs()] == ["play_music", "music_control", "whats_playing"]
        assert "play_youtube only when they want to watch" in m.prompt_note()
    finally:
        ctx.db.close()
    off = make_test_context(skills={"music": False})
    try:
        off.media = FakeMedia()
        assert not Music(off).available()
    finally:
        off.db.close()


# ------------------------------------------------------------------ music_control
async def playing(ctx: Any, m: Music) -> None:
    await m.run("play_music", {"query": "Bohemian Rhapsody", "kind": "song"})
    for t in takeover.claim(ctx):
        await t.run()


async def test_pause_resume_volume(ctx):
    m = Music(ctx)
    assert await m.run("music_control", {"action": "pause"}) == "ok: no music is playing"
    await playing(ctx, m)
    assert await m.run("music_control", {"action": "pause"}) == "ok: paused the music"
    assert m.context_note().startswith("Music: 'Bohemian Rhapsody' (Queen - Topic) is paused")
    assert await m.run("music_control", {"action": "pause"}) == "ok: the music is paused"
    assert await m.run("music_control", {"action": "resume"}) == "ok: the music carries on"
    assert await m.run("music_control", {"action": "resume"}) == "ok: the music isn't paused"
    assert await m.run("music_control", {"action": "volume_up"}) == "ok: music volume 60%"
    assert await m.run("music_control", {"action": "volume_up"}) == "ok: music volume 70%"
    assert await m.run("music_control", {"action": "volume_up"}) == "ok: music volume 80%"
    assert await m.run("music_control", {"action": "volume_up"}) == "ok: the music is as loud as it goes (80%)"
    assert await m.run("music_control", {"action": "volume_down"}) == "ok: music volume 70%"


async def test_next_skips_then_searches_for_more(ctx):
    m = Music(ctx)
    await playing(ctx, m)
    assert await m.run("music_control", {"action": "next"}) == "ok: next up: 'Queen - Bohemian Rhapsody'"
    assert await m.run("music_control", {"action": "next"}) == "ok: next up: 'Bohemian Rhapsody (Reaction!)'"
    assert ctx.media.calls == ["skip", "skip"]
    # the results are used up: a fresh search, without what was found before
    ctx.media.results = SONGS + [{"video_id": "cccccccccc1", "title": "Bohemian Rhapsody (Live Aid 1985)",
                                  "channel": "Queen Official", "duration_seconds": 400}]
    assert await m.run("music_control", {"action": "next"}) == "ok: next up: 'Bohemian Rhapsody (Live Aid 1985)'"
    assert ctx.media.played[-1]["video_id"] == "cccccccccc1" and ctx.media.played[-1]["presentation"] == "music"
    assert (await m.run("music_control", {"action": "next"})).startswith("ok: there's nothing more to play")


async def test_paused_for_a_chat_resume_and_next_wait_for_the_answer(ctx):
    m = Music(ctx)
    await playing(ctx, m)
    ctx.media.state, ctx.media.held = "paused", {"position_seconds": 31.0}
    assert m.context_note().startswith("Music: 'Bohemian Rhapsody' (Queen - Topic) is paused while you talk")
    assert await m.run("music_control", {"action": "resume"}) == \
        "ok: the music carries on as soon as you have finished speaking"
    assert takeover.pending(ctx) == ["music resume"]
    await started(ctx)
    assert ctx.media.calls == ["resume"] and ctx.media.state == "playing"
    ctx.media.state, ctx.media.held = "paused", {"position_seconds": 50.0}
    assert await m.run("music_control", {"action": "next"}) == \
        "ok: the next song plays as soon as you have finished speaking"
    await started(ctx)
    assert ctx.media.calls[-1] == "skip"


async def test_stop(ctx):
    m = Music(ctx)
    ctx.state.transition(RobotState.SPEAKING, "answering")
    await m.run("play_music", {"query": "jazz"})
    assert await m.run("music_control", {"action": "stop"}) == "ok: the music won't start"
    assert takeover.pending(ctx) == [] and m.context_note() == ""
    ctx.state.transition(RobotState.IDLE, "done")
    assert await m.run("music_control", {"action": "stop"}) == "ok: no music is playing"
    await playing(ctx, m)
    assert await m.run("music_control", {"action": "stop"}) == "ok: stopped the music"
    assert ctx.media.calls[-1] == "stop" and m.context_note() == ""


async def test_a_video_isnt_called_music(ctx):
    m = Music(ctx)
    ctx.media.state, ctx.media.item = "playing", {"id": "search:zzzzzzzzzzz", "title": "Baby Shark Dance"}
    assert m.context_note() == ""


async def test_with_the_real_media_controller():
    from littlebot.media.controller import MediaController
    from littlebot.media.display import NoneBackend
    from littlebot.media.search import SearchResult

    class Searcher:
        available = True

        async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
            return [SearchResult("dQw4w9WgXcQ", "Never Gonna Give You Up (Official Audio)", "Rick Astley", 213),
                    SearchResult("M7lc1UVf-VE", "Never Gonna Give You Up (Live)", "Rick Astley", 230)][:limit]

    ctx = make_test_context(media={"player": "none"})
    try:
        ctx.settings.set("profile", {"age_months": 24})
        media = MediaController(ctx, backend=NoneBackend(), searcher=Searcher(), tick_seconds=3600)
        await media.start()
        ctx.media = media
        m = Music(ctx)
        assert m.available()
        assert (await m.run("play_music", {"query": "never gonna give you up", "kind": "song"})).startswith(
            "ok: 'Never Gonna Give You Up' by Rick Astley starts playing")
        for t in takeover.claim(ctx):
            assert await t.run() is True
        st = media.status()
        assert st["state"] == "loading" and st["item"]["presentation"] == "music" and st["queued"] == 1
        page = media.player_state()["item"]
        assert page["presentation"] == "music" and page["channel"] == "Rick Astley"
        assert m.context_note().startswith("Music: playing 'Never Gonna Give You Up' (Rick Astley)")
        assert await m.run("music_control", {"action": "next"}) == "ok: next up: 'Never Gonna Give You Up (Live)'"
        assert media.player_state()["item"]["video_id"] == "M7lc1UVf-VE"
        assert await m.run("music_control", {"action": "stop"}) == "ok: stopped the music"
        assert media.status()["state"] == "idle"
        await media.close()
    finally:
        ctx.db.close()


# ------------------------------------------------------------------ helpers
def test_search_words():
    assert music_query("jazz", None) == ("jazz music mix", "genre")
    assert music_query("relaxing music", None) == ("relaxing music mix", "genre")
    assert music_query("Hello by Adele", None) == ("Hello by Adele official audio", "song")
    assert music_query("lofi hip hop radio", None) == ("lofi hip hop radio", "radio")
    assert music_query("BBC Radio 2", "radio") == ("BBC Radio 2", "radio")
    assert music_query("Taylor Swift", "artist") == ("Taylor Swift best songs mix", "artist")
    assert music_query("workout", "playlist") == ("workout playlist mix", "playlist")
    assert music_query("Adele full album", "artist") == ("Adele full album", "artist")
    assert music_query("x", "nonsense")[1] == "song"
    assert guess_kind("some jazz") == "genre" and guess_kind("my road trip playlist") == "playlist"


def test_scores_and_titles():
    song = {"title": "Song (Official Audio)", "channel": "Artist", "duration_seconds": 200}
    cover = {"title": "Song cover", "channel": "Someone", "duration_seconds": 200}
    reaction = {"title": "Song reaction", "channel": "Someone", "duration_seconds": 200}
    hour = {"title": "Song 10 hours", "channel": "x", "duration_seconds": 36000}
    assert score(song, "song", "song") > score(cover, "song", "song") > score(reaction, "song", "song")
    assert score(song, "song", "song") > score(hour, "song", "song")
    assert score(cover, "song", "song cover") > score(cover, "song", "song")
    live = {"title": "lofi radio - beats to relax to", "channel": "Lofi Girl", "duration_seconds": None}
    assert score(live, "radio", "lofi") > score(song, "radio", "lofi")
    assert speakable("Queen - Bohemian Rhapsody (Official Video Remastered)") == "Queen - Bohemian Rhapsody"
    assert speakable("Song [HD] #music #fyp | Best Of") == "Song"
    assert speakable(None) == "that"
