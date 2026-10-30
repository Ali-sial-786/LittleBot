"""The music skill with Spotify (littlebot/skills/music.py + spotify.py): which service plays (auto / spotify /
youtube, "on YouTube"), the start after the answer without ending the voice chat, music_control and whats_playing
for whichever plays, and the YouTube path untouched when Spotify isn't set up."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from littlebot.core.state import RobotState
from littlebot.skills import takeover
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.skills.music import Music
from littlebot.testing import make_test_context

from spotify_fakes import DEVICE, JAZZ, PHONE, QUEEN, FakeSpotify, spotify_skill
from test_skill_music import MIXES, SONGS, FakeMedia


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(takeover, "QUIET_S", 0.02)
    monkeypatch.setattr(takeover, "POLL_S", 0.005)
    monkeypatch.setattr(takeover, "MAX_WAIT_S", 1.0)


@pytest.fixture
def fake() -> FakeSpotify:
    f = FakeSpotify()
    f.search = {"Bohemian": {"tracks": {"items": QUEEN}}, "jazz": JAZZ}
    return f


@pytest.fixture
def ctx(fake):
    c = make_test_context()
    c.media = FakeMedia()
    sp = spotify_skill(c, fake)
    c.skills = SkillHub(c, [sp, Music(c)])
    yield c
    c.db.close()


def music(ctx: Any) -> Music:
    return ctx.skills.get("music")


async def started(ctx: Any) -> None:
    for _ in range(200):
        if not takeover.pending(ctx):
            break
        await asyncio.sleep(0.005)
    for _ in range(10):
        await asyncio.sleep(0.01)


async def test_spotify_is_the_default_and_starts_after_the_answer(ctx, fake):
    m = music(ctx)
    names = [s["name"] for s in ctx.skills.specs()]
    assert names == ["play_music", "music_control", "whats_playing"]
    play = ctx.skills.specs()[0]["parameters"]["properties"]
    assert play["service"]["enum"] == ["spotify", "youtube"] and "liked_songs" in play["kind"]["enum"]
    assert "Spotify" in m.prompt_note()
    ctx.state.transition(RobotState.SPEAKING, "answering")
    out = await m.run("play_music", {"query": "Bohemian Rhapsody by Queen", "kind": "song"})
    assert out == ("ok: 'Bohemian Rhapsody' by Queen starts playing on Spotify as soon as you have finished "
                   "speaking; say it in one short line")
    assert ctx.media.searches == [] and takeover.pending(ctx) == ["music spotify"]
    assert m.context_note() == "Music: 'Bohemian Rhapsody' starts once you have finished speaking."
    await asyncio.sleep(0.05)
    assert fake.calls("PUT", "/me/player/play") == []  # still answering
    ctx.state.transition(RobotState.IDLE, "done")
    await started(ctx)
    play_req = fake.calls("PUT", "/me/player/play")[-1]
    assert play_req["query"] == {"device_id": "dev-littlebot"}
    assert play_req["json"] == {"context_uri": "spotify:album:a1", "offset": {"uri": "spotify:track:t1"}}
    sp = ctx.skills.get("spotify")
    assert sp.playing() and m.status()["last_service"] == "spotify"


async def test_a_host_claims_it_as_not_a_takeover(ctx, fake):
    m = music(ctx)
    await m.run("play_music", {"query": "jazz", "kind": "genre"})
    claimed = takeover.claim(ctx)
    assert [(c.label, c.takeover) for c in claimed] == [("music spotify", False)]
    assert await claimed[0].run() is True
    assert fake.calls("PUT", "/me/player/play")[-1]["json"] == {"context_uri": "spotify:playlist:jazz1"}


async def test_the_fallback_never_ends_a_voice_chat_for_spotify(ctx, fake, monkeypatch):
    ended: list[str] = []

    async def end_voice_chat(_ctx, reason="x"):
        ended.append(reason)
        return True

    monkeypatch.setattr(takeover, "end_voice_chat", end_voice_chat)
    await music(ctx).run("play_music", {"query": "jazz"})
    await started(ctx)
    assert fake.calls("PUT", "/me/player/play") and ended == []


async def test_youtube_when_asked_or_when_the_receiver_isnt_there(ctx, fake):
    m = music(ctx)
    out = await m.run("play_music", {"query": "Bohemian Rhapsody", "kind": "song", "service": "youtube"})
    assert out.startswith("ok: 'Bohemian Rhapsody' by Queen - Topic starts playing as soon as")  # the YouTube words
    assert ctx.media.searches[-1][0] == "Bohemian Rhapsody official audio"
    takeover.claim(ctx)
    fake.devices = [dict(PHONE)]  # the receiver isn't running
    ctx.skills.get("spotify")._device_at = None
    out = await m.run("play_music", {"query": "Bohemian Rhapsody", "kind": "song"})
    assert "on Spotify" not in out and len(ctx.media.searches) == 2
    takeover.claim(ctx)
    # explicitly on Spotify: it says why it can't
    out = await m.run("play_music", {"query": "Bohemian Rhapsody", "kind": "song", "service": "spotify"})
    assert out.startswith("ok:") and "on Spotify" in out
    seen: list[str] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: seen.append(e.get("text")))
    for c in takeover.claim(ctx):
        assert await c.run() is False
    assert seen and "receiver isn't running" in seen[0]


async def test_auto_never_takes_spotify_from_another_device(ctx, fake):
    fake.playing_on(PHONE)
    out = await music(ctx).run("play_music", {"query": "jazz", "kind": "genre"})
    assert "on Spotify" not in out and ctx.media.searches[-1][0] == "jazz music mix"
    takeover.claim(ctx)
    out = await music(ctx).run("play_music", {"query": "jazz", "kind": "genre", "service": "spotify"})
    assert "on Spotify" in out  # asked for: it may
    takeover.claim(ctx)


async def test_the_provider_setting(ctx, fake):
    m = music(ctx)
    ctx.settings.set("music.provider", "youtube")
    assert "on Spotify" not in await m.run("play_music", {"query": "jazz"})
    takeover.claim(ctx)
    assert "on Spotify" in await m.run("play_music", {"query": "jazz", "service": "spotify"})
    takeover.claim(ctx)
    ctx.settings.set("music.provider", "spotify")
    fake.devices = [dict(PHONE)]
    ctx.skills.get("spotify")._device_at = None
    assert "on Spotify" in await m.run("play_music", {"query": "jazz"})  # always Spotify (it says why it can't)
    takeover.claim(ctx)
    ctx.settings.set("music.provider", "auto")
    assert "on Spotify" not in await m.run("play_music", {"query": "jazz"})
    takeover.claim(ctx)


async def test_their_library_needs_spotify():
    c = make_test_context()
    try:
        c.media = FakeMedia()
        c.skills = SkillHub(c, [Music(c)])
        m = music(c)
        spec = c.skills.specs()[0]["parameters"]["properties"]
        assert "service" not in spec and "liked_songs" not in spec["kind"]["enum"]  # not offered without Spotify
        out = await m.run("play_music", {"query": "my liked songs", "kind": "liked_songs"})
        assert out == "error: Spotify isn't set up on LittleBot; their own Spotify music needs it"
        assert c.media.searches == []
    finally:
        c.db.close()


async def test_liked_songs_and_youtube_for_them(ctx, fake):
    m = music(ctx)
    out = await m.run("play_music", {"query": "my liked songs", "kind": "liked_songs"})
    assert out.startswith("ok: their liked songs starts playing on Spotify")
    for c in takeover.claim(ctx):
        await c.run()
    assert fake.calls("PUT", "/me/player/play")[-1]["json"]["uris"]
    out = await m.run("play_music", {"query": "my liked songs", "kind": "liked_songs", "service": "youtube"})
    assert out.startswith("error: their own liked songs")


async def test_spotify_errors_are_short_and_never_raise(ctx, fake):
    m = music(ctx)
    assert await m.run("play_music", {"query": " "}) == "error: say what music to play"
    assert (await m.run("play_music", {"query": "polka", "kind": "genre", "service": "spotify"})) == \
        "error: Spotify found nothing for that; try other words"
    out = await m.run("play_music", {"query": "Discover Weekly", "kind": "my_playlist"})
    assert out.startswith("error: Spotify no longer lets apps open its own personal playlists")
    out = await m.run("play_music", {"query": 42, "kind": ["x"], "service": {"a": 1}})  # junk from the model
    assert out.startswith("ok:") or out.startswith("error:")
    takeover.claim(ctx)
    ctx.privacy.set(cloud_enabled=False)  # Spotify gone: YouTube, as before
    assert "on Spotify" not in await m.run("play_music", {"query": "jazz"})
    takeover.claim(ctx)


async def test_music_control_and_whats_playing_follow_spotify(ctx, fake):
    m = music(ctx)
    assert await m.run("music_control", {"action": "pause"}) == "ok: no music is playing"
    fake.playing_on(DEVICE)
    await ctx.skills.get("spotify").now_playing(fresh=True)
    assert await m.run("music_control", {"action": "pause"}) == "ok: Paused."
    assert await m.run("music_control", {"action": "resume"}) == "ok: Playing again."
    assert await m.run("music_control", {"action": "previous"}) == "ok: Back one song."
    assert await m.run("music_control", {"action": "set_volume", "percent": 45}) == "ok: Music volume 45%."
    assert await m.run("music_control", {"action": "shuffle_on"}) == "ok: Shuffle on."
    assert (await m.run("music_control", {"action": "dance"})).startswith("error: unknown action")
    out = json.loads(await m.run("whats_playing", {}))
    assert out["service"] == "spotify" and out["song"] == "Bohemian Rhapsody" and out["artist"] == "Queen"
    assert out["state"] == "playing" and out["position"].endswith("of 5:54")
    assert ctx.media.calls == []  # YouTube untouched


async def test_youtube_music_playing_is_what_control_steers(ctx, fake):
    m = music(ctx)
    ctx.media.results = list(SONGS)
    await m.run("play_music", {"query": "Bohemian Rhapsody", "service": "youtube"})
    for c in takeover.claim(ctx):
        await c.run()
    assert await m.run("music_control", {"action": "pause"}) == "ok: paused the music"
    assert (await m.run("music_control", {"action": "previous"})).startswith("error: YouTube music can't do that")
    assert await m.run("music_control", {"action": "set_volume", "percent": 30}) == "ok: music volume 30%"
    out = json.loads(await m.run("whats_playing", {}))
    assert out == {"service": "youtube", "title": "Bohemian Rhapsody", "channel": "Queen - Topic", "state": "paused"}
    assert fake.calls("PUT", "/me/player/pause") == []


async def test_spotify_starting_stops_youtube(ctx, fake):
    m = music(ctx)
    ctx.media.results = list(MIXES)
    await m.run("play_music", {"query": "jazz", "service": "youtube"})
    for c in takeover.claim(ctx):
        await c.run()
    assert ctx.media.state == "playing"
    await m.run("play_music", {"query": "Bohemian Rhapsody by Queen", "service": "spotify"})
    for c in takeover.claim(ctx):
        assert await c.run() is True
    assert ctx.media.calls[-1] == "stop" and ctx.media.state == "idle"
    assert await m.run("music_control", {"action": "next"}) == "ok: Next song."


async def test_stop_before_it_started(ctx, fake):
    m = music(ctx)
    ctx.state.transition(RobotState.SPEAKING, "answering")
    await m.run("play_music", {"query": "jazz"})
    assert await m.run("music_control", {"action": "stop"}) == "ok: the music won't start"
    assert takeover.pending(ctx) == []
    ctx.state.transition(RobotState.IDLE, "done")
    await asyncio.sleep(0.05)
    assert fake.calls("PUT", "/me/player/play") == []


async def test_nothing_playing(ctx, fake):
    assert await music(ctx).run("whats_playing", {}) == "ok: no music is playing"
