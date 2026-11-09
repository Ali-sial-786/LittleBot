"""The music card (littlebot/screens/cards/music.py): registered and offered, built from the Spotify skill or a
spec, drawn (playing, paused, with and without a cover), a progress bar that follows the clock, taps and voice
commands that pause / carry on through the skill (and flip back when that fails), and the face never covered."""

from __future__ import annotations

import asyncio
from typing import Any

import pygame
import pytest

from littlebot.screens import base, preview
from littlebot.screens.base import PRESENTER_BOX, RecordingHost
from littlebot.screens.cards import music as music_card
from littlebot.screens.cards.music import MusicCard, mmss
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context

from spotify_fakes import DEVICE, QUEEN, FakeSpotify, spotify_skill

SPEC = music_card.SAMPLES[0]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class Picture:
    ready = True
    credit = ""

    def __init__(self) -> None:
        self.surface = pygame.Surface((640, 640))
        self.surface.fill((200, 40, 90))


class Skill:
    """Stands in for the Spotify skill on the card's side."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.asked: list[str] = []
        self.state: dict[str, Any] | None = None
        self.refreshed = 0

    async def command(self, action: str) -> tuple[bool, str]:
        self.asked.append(action)
        return (True, "Paused.") if self.ok else (False, "Spotify had a problem; try again in a minute.")

    def snapshot(self) -> dict[str, Any] | None:
        return self.state

    async def refresh(self) -> None:
        self.refreshed += 1


def test_registered_and_offered():
    ct = base.card_types(offered_only=True)["music"]
    assert ct.fields == {} and ct.required == ()
    assert set(ct.actions) == {"pause", "resume", "next", "previous"}
    assert "what's playing" in ct.description


async def test_samples_build_and_draw():
    for spec in music_card.SAMPLES:
        card = await base.build(spec, None, RecordingHost())
        assert isinstance(card, MusicCard)
        for seconds in (0.1, 0.6, 2.0):
            assert preview.surface(card, seconds=seconds).get_size() == (base.W, base.H)
    paused = await base.build(music_card.SAMPLES[1], None, RecordingHost())
    assert not paused.playing and paused.describe().endswith("(paused)")
    assert await base.build({"card": "music"}, None, RecordingHost()) is None  # nothing to show


def test_the_cover_is_fetched_and_drawn_and_the_face_stays_free():
    url = "https://i.scdn.co/image/1-300"
    host = RecordingHost(pictures={url: Picture()})
    card = MusicCard({**SPEC, "image_url": url}, host)
    assert host.asked_pictures == [("", url)]
    surf = preview.surface(card, seconds=1.5)
    cover = card.art_rect()
    assert all(abs(a - b) <= 6 for a, b in zip(surf.get_at(cover.center)[:3], (200, 40, 90)))  # the cover
    assert card.presenter_box == PRESENTER_BOX
    assert not cover.colliderect(pygame.Rect(*PRESENTER_BOX))
    no_https = MusicCard({**SPEC, "image_url": "http://example.com/x.jpg"}, RecordingHost())
    assert no_https.slot is None


def test_the_progress_follows_the_clock():
    clock = Clock()
    card = MusicCard(SPEC, RecordingHost(clock=clock))
    assert card.progress() == 83_000 and mmss(card.progress()) == "1:23"
    clock.t += 10
    assert card.progress() == 93_000
    assert card.describe() == "Now playing on Spotify: 'Bohemian Rhapsody - Remastered 2011' by Queen, 1:33 of 5:54"
    clock.t += 1000
    assert card.progress() == 354_000 and card.fraction() == 1.0  # never past the end
    paused = MusicCard({**SPEC, "playing": False}, RecordingHost(clock=clock))
    clock.t += 50
    assert paused.progress() == 83_000
    assert card.status()["song"].startswith("Bohemian") and card.status()["duration_ms"] == 354_000


async def test_a_tap_pauses_through_the_skill():
    skill = Skill()
    host = RecordingHost()
    card = MusicCard(SPEC, host, command=skill.command, source=skill.snapshot, loop=asyncio.get_running_loop())
    assert card.tap(500, 300) and not card.playing and host.sounds == ["pop"]
    assert not card.tap(500, 300)  # one at a time
    await asyncio.sleep(0.01)
    card.update(0.05)
    assert skill.asked == ["pause"] and not card.playing
    assert card.command("next", {}) and card.progress_ms == 0
    await asyncio.sleep(0.01)
    card.update(0.05)
    assert card.command("toggle", {}) and card.playing  # paused: a toggle carries on
    await asyncio.sleep(0.01)
    card.update(0.05)
    assert skill.asked == ["pause", "next", "resume"]
    assert not card.command("dance", {})
    assert card.key("left") and skill.asked[-1] != "previous"  # sent; runs on the loop
    await asyncio.sleep(0.01)
    card.update(0.05)
    assert skill.asked[-1] == "previous"
    preview.surface(card, seconds=0.2)


async def test_a_failed_pause_flips_back_and_tells_the_voice():
    skill = Skill(ok=False)
    host = RecordingHost()
    card = MusicCard(SPEC, host, command=skill.command, loop=asyncio.get_running_loop())
    assert card.tap(500, 300) and not card.playing
    await asyncio.sleep(0.01)
    card.update(0.05)
    assert card.playing and host.told == ["The music card: Spotify had a problem; try again in a minute."]


def test_without_the_skill_a_tap_does_nothing():
    card = MusicCard(SPEC, RecordingHost())
    assert not card.tap(10, 10) and card.playing


async def test_it_follows_the_skill_and_asks_spotify_now_and_then():
    skill = Skill()
    card = MusicCard(SPEC, RecordingHost(), source=skill.snapshot, refresh=skill.refresh,
                     loop=asyncio.get_running_loop())
    skill.state = {"title": "Another One Bites the Dust", "artist": "Queen", "on_littlebot": True, "playing": True,
                   "duration_ms": 215_000, "progress_ms": 1000, "image_url": ""}
    card.update(1.1)
    assert card.song == "Another One Bites the Dust" and card.duration_ms == 215_000
    skill.state = {"title": "x", "on_littlebot": False, "playing": True}
    card.update(1.1)
    assert not card.playing and card.song == "Another One Bites the Dust"  # moved to another speaker
    card.update(music_card.REFRESH_S)
    await asyncio.sleep(0.01)
    assert skill.refreshed == 1


async def test_built_from_the_spotify_skill():
    ctx = make_test_context()
    try:
        fake = FakeSpotify()
        sp = spotify_skill(ctx, fake)
        ctx.skills = SkillHub(ctx, [sp])
        assert await base.build({"card": "music"}, ctx, RecordingHost()) is None  # nothing playing
        fake.playing_on(DEVICE, QUEEN[0], progress=60_000)
        card = await base.build({"card": "music"}, ctx, RecordingHost())
        assert isinstance(card, MusicCard) and card.song == "Bohemian Rhapsody" and card.artist == "Queen"
        assert card.album == "A Night at the Opera" and card.image_url == "https://i.scdn.co/image/1-300"
        assert card.tap(400, 300)
        for _ in range(20):
            await asyncio.sleep(0.01)
            card.update(0.02)
            if fake.calls("PUT", "/me/player/pause"):
                break
        assert fake.player["is_playing"] is False
        card.update(1.1)
        assert not card.playing
        await sp.stop()
    finally:
        ctx.db.close()


@pytest.mark.parametrize("spec", [{"card": "music", "title": "**A** song\n" * 30, "artist": ["x"], "album": None,
                                   "duration_ms": "long", "progress_ms": -5, "playing": "yes"},
                                  {"card": "music", "title": "Short", "duration_ms": True}])
def test_odd_specs_are_cleaned(spec):
    card = MusicCard(spec, RecordingHost())
    assert card.song and card.artist == "" and card.duration_ms is None and card.progress_ms == 0
    preview.surface(card, seconds=0.5)
