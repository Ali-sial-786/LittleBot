"""Spotify (littlebot/skills/spotify.py, spotify_api.py): the PKCE sign-in, token refresh and the token file,
searches and the play requests they make, the receiver missing, control, ducking for the voice, and what it says.
Against a fake Spotify (spotify_fakes.FakeSpotify through httpx.MockTransport): no network, no real keys."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from littlebot.skills import spotify as spotify_mod
from littlebot.skills.spotify import Choice, Spotify, norm, similar, split_by
from littlebot.skills.spotify_api import (
    REDIRECT_URI,
    SCOPES,
    SpotifyAuth,
    SpotifyError,
    clean_client_id,
    parse_redirect,
    pkce_challenge,
    pkce_verifier,
)
from littlebot.testing import make_test_context

from spotify_fakes import ACCESS, CLIENT_ID, DEVICE, JAZZ, PHONE, QUEEN, REFRESH, TAYLOR, FakeSpotify, connected, \
    spotify_skill


@pytest.fixture
def ctx():
    c = make_test_context()
    yield c
    c.db.close()


@pytest.fixture
def fake() -> FakeSpotify:
    return FakeSpotify()


def secrets_in(text: str, fake: FakeSpotify) -> list[str]:
    return [s for s in (ACCESS, REFRESH, fake.access, fake.refresh, "GOOD-CODE") if s in text]


# ------------------------------------------------------------------ PKCE and the sign-in
def test_pkce_verifier_and_challenge():
    # RFC 7636, appendix B
    rfc = ("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    assert pkce_challenge(rfc[0]) == rfc[1]
    v = pkce_verifier()
    assert 43 <= len(v) <= 128 and all(c.isalnum() or c in "-._~" for c in v)
    assert pkce_verifier() != v
    assert clean_client_id(" 0123456789ABCDEF0123456789abcdef ") == CLIENT_ID
    assert clean_client_id("not-an-id") is None and clean_client_id(None) is None


def test_the_redirected_address_is_read_loosely():
    url = f"{REDIRECT_URI}?code=abc&state=xyz"
    assert parse_redirect(url) == {"code": "abc", "state": "xyz"}
    assert parse_redirect("  ?code=abc&state=xyz\n") == {"code": "abc", "state": "xyz"}
    assert parse_redirect("code=abc&state=xyz") == {"code": "abc", "state": "xyz"}
    assert parse_redirect(f"{REDIRECT_URI}?error=access_denied&state=xyz") == {"error": "access_denied",
                                                                               "state": "xyz"}
    assert parse_redirect("") == {} and parse_redirect("hello") == {}


async def test_connect_checks_the_state_and_keeps_the_tokens_private(tmp_path: Path, fake: FakeSpotify, caplog):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "data" / "spotify_token.json"
    auth = SpotifyAuth(path, transport=fake.transport())
    with pytest.raises(SpotifyError) as e:
        auth.begin("nope")
    assert e.value.kind == "bad_client_id"
    with pytest.raises(SpotifyError) as e:
        await auth.finish(f"{REDIRECT_URI}?code=GOOD-CODE&state=x")
    assert e.value.kind == "no_pending"

    url = auth.begin(CLIENT_ID)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.spotify.com/authorize"
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code" and q["redirect_uri"] == REDIRECT_URI
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43 and "=" not in q["code_challenge"]
    assert q["scope"].split() == list(SCOPES) and len(q["state"]) >= 16
    assert auth.pending and not auth.connected
    fake.challenge = q["code_challenge"]

    for pasted, kind in ((f"{REDIRECT_URI}?code=GOOD-CODE&state=wrong", "state_mismatch"),
                         (f"{REDIRECT_URI}?state={q['state']}", "no_code"),
                         ("just some words", "no_code")):
        with pytest.raises(SpotifyError) as e:
            await auth.finish(pasted)
        assert e.value.kind == kind
    assert auth.pending  # a wrong paste doesn't end the sign-in
    await auth.finish(f"{REDIRECT_URI}?code=GOOD-CODE&state={q['state']}")
    assert auth.connected and not auth.pending and auth.client_id == CLIENT_ID
    form = fake.token_requests[-1]
    assert form["grant_type"] == "authorization_code" and form["redirect_uri"] == REDIRECT_URI
    assert pkce_challenge(form["code_verifier"]) == q["code_challenge"] and form["client_id"] == CLIENT_ID
    assert "client_secret" not in form

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert ACCESS in path.read_text() and REFRESH in path.read_text()
    status = auth.status()
    assert status["connected"] and status["missing_scopes"] == []
    for text in (repr(status), repr(auth.tokens()), caplog.text):
        assert secrets_in(text, fake) == [], text
    with pytest.raises(SpotifyError) as e:  # the code works once
        await auth.finish(f"{REDIRECT_URI}?code=GOOD-CODE&state={q['state']}")
    assert e.value.kind == "no_pending"
    assert auth.disconnect() and not path.exists() and not auth.connected


async def test_a_refused_code_or_a_cancelled_sign_in(tmp_path: Path, fake: FakeSpotify):
    auth = SpotifyAuth(tmp_path / "t.json", transport=fake.transport())
    url = auth.begin(CLIENT_ID)
    state = parse_qs(urlsplit(url).query)["state"][0]
    fake.challenge = "something else"  # the verifier won't match
    with pytest.raises(SpotifyError) as e:
        await auth.finish(f"{REDIRECT_URI}?code=GOOD-CODE&state={state}")
    assert e.value.kind == "bad_code" and not auth.connected
    auth.begin(CLIENT_ID)
    with pytest.raises(SpotifyError) as e:
        await auth.finish(f"{REDIRECT_URI}?error=access_denied&state={state}")
    assert e.value.kind == "denied" and "cancelled" in str(e.value)
    clock = [1000.0]
    slow = SpotifyAuth(tmp_path / "t2.json", transport=fake.transport(), now=lambda: clock[0])
    url = slow.begin(CLIENT_ID)
    clock[0] += 16 * 60
    with pytest.raises(SpotifyError) as e:
        await slow.finish(f"{REDIRECT_URI}?code=GOOD-CODE&state={parse_qs(urlsplit(url).query)['state'][0]}")
    assert e.value.kind == "expired"


async def test_the_access_token_is_refreshed_and_the_new_refresh_token_kept(tmp_path: Path, fake: FakeSpotify,
                                                                            caplog):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "t.json"
    connected(path, expires_in=30)  # runs out within the margin
    auth = SpotifyAuth(path, transport=fake.transport())
    token = await auth.access_token()
    assert token == fake.access != ACCESS
    assert fake.token_requests[-1] == {"grant_type": "refresh_token", "refresh_token": REFRESH,
                                       "client_id": CLIENT_ID}
    again = SpotifyAuth(path, transport=fake.transport())  # read back from the file
    assert again.tokens().refresh_token == fake.refresh and os.stat(path).st_mode & 0o777 == 0o600
    assert await again.access_token() == fake.access and len(fake.token_requests) == 1  # still fresh
    assert secrets_in(caplog.text, fake) == []
    # the refresh token is refused: signed out, said plainly
    fake.refresh_error = "invalid_grant"
    with pytest.raises(SpotifyError) as e:
        await again.access_token(force_refresh=True)
    assert e.value.kind == "revoked" and not path.exists() and not again.connected


async def test_an_expired_token_is_refreshed_once_on_401(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    fake.expire_next = True
    dev = await sp.device()
    assert dev["id"] == DEVICE["id"] and fake.refresh_n == 1
    assert [r["token_ok"] for r in fake.calls("GET", "/me/player/devices")] == [True, True]
    assert sp.status()["device"]["visible"] is True
    await sp.stop()


# ------------------------------------------------------------------ gates and status
async def test_offered_only_when_connected_online_and_cloud_on(ctx, fake: FakeSpotify):
    sp = Spotify(ctx, transport=fake.transport(), token_path=Path(ctx.extras["spotify_token_file"]))
    assert sp.blocked_reason() == "no_client_id" and not sp.available() and not sp.playing()
    ctx.settings.set("spotify.client_id", CLIENT_ID)
    assert sp.blocked_reason() == "not_connected"
    with pytest.raises(ValueError):
        ctx.settings.set("spotify.client_id", "not one")
    sp = spotify_skill(ctx, fake)
    assert sp.available() and sp.tools() == []
    ctx.privacy.set(cloud_enabled=False)
    assert sp.blocked_reason() == "cloud_off"
    assert await sp.control("pause") == "Cloud communication is off in Privacy, so LittleBot can't use Spotify."
    off = make_test_context(mode="offline")
    try:
        sp2 = spotify_skill(off, fake)
        assert sp2.blocked_reason() == "offline"
    finally:
        off.db.close()
    disabled = make_test_context(skills={"spotify": False})
    try:
        assert spotify_skill(disabled, fake).blocked_reason() == "off"
    finally:
        disabled.db.close()
    assert fake.requests == []


async def test_status_never_has_a_token(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    fake.playing_on(DEVICE)
    st = await sp.refresh()
    text = repr(st)
    assert st["connected"] and st["device"] == {"visible": True, "active": True, "volume": 60}
    assert st["now"]["title"] == "Bohemian Rhapsody" and st["client_id"] == CLIENT_ID
    assert st["redirect_uri"] == REDIRECT_URI and st["provider"] == "auto"
    assert secrets_in(text, fake) == [] and "token" not in " ".join(st)
    await sp.stop()


# ------------------------------------------------------------------ finding and playing
async def test_a_song_by_an_artist_plays_from_its_album(ctx, fake: FakeSpotify):
    fake.search = {"Bohemian": {"tracks": {"items": QUEEN}}}
    sp = spotify_skill(ctx, fake)
    choice = await sp.find("Bohemian Rhapsody by Queen", "song")
    search = fake.calls("GET", "/search")[-1]["query"]
    assert search["q"] == "track:Bohemian Rhapsody artist:Queen" and search["type"] == "track"
    assert search["market"] == "from_token" and int(search["limit"]) <= 10
    assert choice.what == "track" and choice.said() == "'Bohemian Rhapsody' by Queen"
    assert choice.body() == {"context_uri": "spotify:album:a1", "offset": {"uri": "spotify:track:t1"}}
    assert choice.image_url == "https://i.scdn.co/image/1-300"  # the smallest that is at least 300 px
    await sp.play_choice(choice, show_card=False)
    play = fake.calls("PUT", "/me/player/play")[-1]
    assert play["query"] == {"device_id": "dev-littlebot"} and play["json"] == choice.body()
    assert sp.playing() and sp.snapshot()["title"] == "Bohemian Rhapsody"
    await sp.stop()


def test_choice_picture_is_the_smallest_big_enough():
    assert spotify_mod._image({"images": [{"url": "a", "width": 640}, {"url": "b", "width": 300},
                                          {"url": "c", "width": 64}]}) == "b"
    assert spotify_mod._image({"images": [{"url": "c", "width": 64}]}) == "c"
    assert spotify_mod._image({}) == ""


async def test_an_artist_said_as_a_song_plays_the_artist(ctx, fake: FakeSpotify):
    fake.search = {"Taylor": TAYLOR}
    sp = spotify_skill(ctx, fake)
    choice = await sp.find("Taylor Swift")
    assert fake.calls("GET", "/search")[-1]["query"]["type"] == "track,artist"
    assert choice.what == "artist" and choice.body() == {"context_uri": "spotify:artist:TaylorSwift"}
    choice = await sp.find("Taylor Swift", "artist")
    assert fake.calls("GET", "/search")[-1]["query"]["type"] == "artist" and choice.title == "Taylor Swift"
    await sp.stop()


async def test_a_genre_plays_a_playlist_and_my_playlists_come_first(ctx, fake: FakeSpotify):
    fake.search = {"jazz": JAZZ}
    sp = spotify_skill(ctx, fake)
    choice = await sp.find("jazz", "genre")
    assert choice.what == "playlist" and choice.body() == {"context_uri": "spotify:playlist:jazz1"}
    assert choice.said() == "'Jazz Classics' by Jazz Fan"
    mine = await sp.find("my road trip playlist", "playlist")
    assert mine.body() == {"context_uri": "spotify:playlist:road"} and not fake.calls("GET", "/search")[1:]
    assert (await sp.find("Sunday Chill", "my_playlist")).title == "Sunday chill"
    with pytest.raises(SpotifyError) as e:
        await sp.find("Discover Weekly", "my_playlist")
    assert e.value.kind == "spotify_made" and "pick LittleBot as the speaker" in str(e.value)
    with pytest.raises(SpotifyError) as e:
        await sp.find("gym bangers", "my_playlist")
    assert e.value.kind == "no_playlist"
    with pytest.raises(SpotifyError) as e:
        await sp.find("polka", "genre")
    assert e.value.kind == "nothing_found"
    await sp.stop()


async def test_liked_songs_and_favourites_play_shuffled_without_explicit_ones(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake, rng=random.Random(4))
    liked = await sp.find("my liked songs", "liked_songs")
    assert liked.what == "tracks" and liked.said() == "their liked songs"
    assert len(liked.uris) == 4 and "spotify:track:t42" not in liked.uris  # Liked 2 is explicit
    assert set(liked.uris) == {"spotify:track:t40", "spotify:track:t41", "spotify:track:t43", "spotify:track:t44"}
    assert liked.first_title.startswith("Liked ") and liked.card_spec()["title"] == liked.first_title
    top = await sp.find("something I like", "my_favorites")
    assert top.body()["uris"] and all(u.startswith("spotify:track:t6") for u in top.body()["uris"])
    assert fake.calls("GET", "/me/top/tracks")[-1]["query"]["time_range"] == "medium_term"
    fake.top = []
    assert (await sp.find("x", "my_favorites")).said() == "their liked songs"  # a new account: liked songs
    allowed = make_test_context(skills={"spotify_explicit": True})
    try:
        sp2 = spotify_skill(allowed, fake)
        assert len((await sp2.find("", "liked_songs")).uris) == 5
        await sp2.stop()
    finally:
        allowed.db.close()
    fake.search = {"WAP": {"tracks": {"items": [dict(QUEEN[0], explicit=True, name="WAP")]}}}
    with pytest.raises(SpotifyError) as e:
        await sp.find("WAP", "song")
    assert e.value.kind == "explicit"
    await sp.stop()


async def test_the_receiver_missing_is_said_plainly(ctx, fake: FakeSpotify):
    fake.devices = [dict(PHONE)]
    sp = spotify_skill(ctx, fake)
    with pytest.raises(SpotifyError) as e:
        await sp.play_choice(Choice("artist", "Queen", context_uri="spotify:artist:Queen"), show_card=False)
    assert e.value.kind == "no_device" and "receiver isn't running" in str(e.value)
    assert fake.calls("PUT", "/me/player/play") == []
    assert sp.status()["device"]["visible"] is False
    await sp.stop()


async def test_an_idle_receiver_is_woken_with_a_transfer(ctx, fake: FakeSpotify, monkeypatch):
    monkeypatch.setattr(spotify_mod, "WAKE_RECEIVER_S", 0.0)
    sp = spotify_skill(ctx, fake)
    fake.fail["PUT /me/player/play"] = (404, {"error": {"status": 404, "message": "Device not found"}})
    await sp.play_choice(Choice("artist", "Queen", context_uri="spotify:artist:Queen"), show_card=False)
    assert fake.calls("PUT", "/me/player")[-1]["json"] == {"device_ids": ["dev-littlebot"], "play": False}
    assert len(fake.calls("PUT", "/me/player/play")) == 2 and sp.playing()
    fake.premium = False
    with pytest.raises(SpotifyError) as e:
        await sp.play_choice(Choice("artist", "Queen", context_uri="spotify:artist:Queen"), show_card=False)
    assert e.value.kind == "premium" and "Premium" in str(e.value)
    await sp.stop()


# ------------------------------------------------------------------ control
async def test_control_acts_only_on_littlebot(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    assert await sp.control("pause") == "Spotify isn't playing on me right now."
    fake.playing_on(PHONE)
    assert await sp.control("next") == "Spotify is playing on Ali's Pixel, not on me."
    assert fake.calls("POST", "/me/player/next") == [] and not sp.playing()
    fake.playing_on(DEVICE)
    assert await sp.control("pause") == "Paused." and fake.player["is_playing"] is False and not sp.playing()
    assert fake.calls("PUT", "/me/player/pause")[-1]["query"] == {"device_id": "dev-littlebot"}
    assert await sp.control("pause") == "It's paused."
    assert await sp.control("resume") == "Playing again." and sp.playing()
    assert await sp.control("play") == "It's playing."
    assert await sp.control("skip") == "Next song." and fake.calls("POST", "/me/player/next")
    assert await sp.control("previous") == "Back one song."
    assert await sp.control("volume_up") == "Music volume 70%."
    assert fake.calls("PUT", "/me/player/volume")[-1]["query"]["volume_percent"] == "70"
    assert await sp.command("set_volume", "35") == (True, "Music volume 35%.")
    assert await sp.command("set_volume", 300) == (False, "Say a volume from 0 to 100.")
    assert await sp.control("shuffle_on") == "Shuffle on." and fake.player["shuffle_state"] is True
    assert await sp.control("dance") == "Spotify can't do that."
    fake.player = None
    assert await sp.control("resume") == "Nothing is paused on Spotify here. Ask me for some music."
    await sp.stop()


async def test_control_never_raises(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    fake.playing_on(DEVICE)
    fake.fail["PUT /me/player/pause"] = (503, {"error": {"status": 503, "message": "down"}})
    assert await sp.command("pause") == (False, "Spotify had a problem; try again in a minute.")
    fake.fail["GET /me/player"] = (429, {"error": {"status": 429}})
    assert (await sp.command("next"))[0] is False
    await sp.stop()


# ------------------------------------------------------------------ ducking for the voice
async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


def volumes(fake: FakeSpotify) -> list[str]:
    return [r["query"]["volume_percent"] for r in fake.calls("PUT", "/me/player/volume")]


async def test_the_wake_word_turns_it_down_and_the_end_of_the_session_back_up(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    await sp.start()
    fake.playing_on(DEVICE, volume=60)
    ctx.bus.publish("audio.wake_word", {"phrase": "hey littlebot"})
    await settle()
    assert volumes(fake) == ["20"] and sp.status()["ducked"]
    assert "turned down while you talk" in sp.context_note()
    ctx.bus.publish("assistant.session", {"active": True})
    await settle()
    ctx.bus.publish("assistant.session", {"active": False, "reason": "quiet"})
    await settle()
    assert volumes(fake) == ["20", "60"] and not sp.status()["ducked"]
    # GPT-Live's call the same way
    ctx.bus.publish("audio.wake_word", {})
    ctx.bus.publish("live.call_started", {})
    await settle()
    ctx.bus.publish("live.call_ended", {"reason": "bye"})
    await settle()
    assert volumes(fake) == ["20", "60", "20", "60"]
    await sp.stop()


async def test_paused_meanwhile_the_volume_comes_back_with_the_music(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    await sp.start()
    fake.playing_on(DEVICE, volume=60)
    ctx.bus.publish("audio.wake_word", {})
    ctx.bus.publish("assistant.session", {"active": True})
    await settle()
    assert await sp.control("pause") == "Paused."
    ctx.bus.publish("assistant.session", {"active": False})
    await settle()
    assert volumes(fake) == ["20"] and sp.status()["ducked"]  # paused: not now
    assert await sp.control("resume") == "Playing again."
    assert volumes(fake) == ["20", "60"] and not sp.status()["ducked"]
    await sp.stop()


async def test_a_volume_chosen_meanwhile_wins_and_nothing_playing_isnt_touched(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    await sp.start()
    fake.playing_on(DEVICE, volume=60)
    ctx.bus.publish("audio.wake_word", {})
    ctx.bus.publish("assistant.session", {"active": True})
    await settle()
    assert await sp.control("volume_down") == "Music volume 50%."  # from the real level, not the ducked one
    ctx.bus.publish("assistant.session", {"active": False})
    await settle()
    assert volumes(fake) == ["20", "50"]
    fake.playing_on(PHONE)  # playing on the phone: LittleBot's wake word leaves it alone
    ctx.bus.publish("audio.wake_word", {})
    await settle()
    assert volumes(fake) == ["20", "50"]
    await sp.stop()


async def test_a_wake_word_without_a_session_doesnt_stay_quiet(ctx, fake: FakeSpotify, monkeypatch):
    monkeypatch.setattr(spotify_mod, "WAKE_GRACE_S", 0.05)
    sp = spotify_skill(ctx, fake)
    await sp.start()
    fake.playing_on(DEVICE, volume=60)
    ctx.bus.publish("audio.wake_word", {})
    await settle()
    await asyncio.sleep(0.1)
    await settle()
    assert volumes(fake) == ["20", "60"]
    await sp.stop()


async def test_music_started_while_talking_starts_quietly(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    await sp.start()
    ctx.bus.publish("assistant.session", {"active": True})
    await settle()
    await sp.play_choice(Choice("artist", "Queen", context_uri="spotify:artist:Queen"), show_card=False)
    assert volumes(fake)[:1] == ["20"] and sp.status()["ducked"]
    ctx.bus.publish("assistant.session", {"active": False})
    await settle()
    assert volumes(fake)[-1] == "60" and not sp.status()["ducked"]
    await sp.stop()


async def test_stop_all_and_a_video_pause_it(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    await sp.start()
    fake.playing_on(DEVICE)
    await sp.now_playing(fresh=True)
    ctx.bus.publish("media.started", {"item_id": "search:x", "title": "Baby Shark", "kind": "video"})
    await settle()
    assert fake.player["is_playing"] is False and len(fake.calls("PUT", "/me/player/pause")) == 1
    await sp.control("resume")
    ctx.bus.publish("system.stop_all", {"source": "dashboard"})
    await settle()
    assert len(fake.calls("PUT", "/me/player/pause")) == 2
    await sp.stop()


# ------------------------------------------------------------------ notes and helpers
async def test_context_note_and_playing(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    assert sp.context_note() == "" and not sp.playing()
    fake.playing_on(DEVICE)
    await sp.now_playing(fresh=True)
    assert sp.playing()
    assert sp.context_note().startswith("Spotify: playing 'Bohemian Rhapsody' by Queen on LittleBot")
    fake.player["is_playing"] = False
    await sp.now_playing(fresh=True)
    assert sp.context_note() == "Spotify: 'Bohemian Rhapsody' by Queen is paused on LittleBot (music_control " \
                                "resume carries on)."
    ctx.privacy.set(cloud_enabled=False)
    assert not sp.playing()
    await sp.stop()


def test_names():
    assert norm("my Road Trip playlist!") == "road trip"
    assert similar("Road Trip 2026", "road trip") >= 0.8 and similar("x", "") == 0.0
    assert split_by("Hello by Adele") == ("Hello", "Adele") and split_by("Stand by Me") == ("Stand by Me", "")
    assert split_by("Hello") == ("Hello", "")
    assert split_by("Stand by Me by Ben E. King") == ("Stand by Me", "Ben E. King")
    assert spotify_mod.mmss(83_000) == "1:23" and spotify_mod.mmss(None) == "0:00"


def test_a_real_robot_context_reads_the_test_token_path(ctx):
    sp = Spotify(ctx)
    assert sp.auth.store.path == Path(ctx.extras["spotify_token_file"]) and not sp.auth.connected


def test_the_hub_builds_it_before_music():
    from littlebot.skills.hub import SKILLS

    names = [c for _, c in SKILLS]
    assert names.index("Spotify") < names.index("Music")


async def test_unexpected_answers_are_handled(ctx, fake: FakeSpotify):
    sp = spotify_skill(ctx, fake)
    fake.search = {"odd": {"tracks": {"items": [None, {"name": "no uri"}]}}}
    with pytest.raises(SpotifyError) as e:
        await sp.find("odd song", "song")
    assert e.value.kind == "nothing_found"
    with pytest.raises(SpotifyError):
        await sp.find("", "song")
    fake.player = {"device": "not a dict", "item": None, "is_playing": "yes"}
    st: Any = await sp.now_playing(fresh=True)
    assert st["title"] is None and not st["on_littlebot"]
    await sp.stop()
