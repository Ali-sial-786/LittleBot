"""Spotify: LittleBot plays the owner's Spotify (Premium) on its own speaker, and is the default for music.

The pieces (docs/SPOTIFY.md):

* the receiver: librespot (the open-source Spotify Connect speaker, from raspotify) runs on the robot as the user
  service ``littlebot-spotify.service`` (scripts/setup_spotify.sh). It shows up in every Spotify app as a speaker
  called "LittleBot" (``skills.spotify_device_name``) and plays through the robot's PipeWire, into the
  echo-cancelled speaker, so its music never sets off the wake word;
* control: Spotify's Web API as the owner, signed in with PKCE from the dashboard (littlebot/skills/spotify_api.py,
  the tokens in data/spotify_token.json, chmod 600);
* this skill: the Spotify side of the music skill's tools (littlebot/skills/music.py ``play_music``,
  ``music_control``, ``whats_playing``). It has no tools of its own, so the model never sees two ways to play music.

Public API (the music skill, the music card, the dashboard, the assistant's instant intents)::

    spotify.available() / blocked_reason()      connected, not offline, cloud communication on
    spotify.playing() -> bool                   Spotify is playing on LittleBot (what it last heard; no network)
    spotify.paused() -> bool                    ... is paused on LittleBot ("carry on" is about it)
    await spotify.control(action) -> str        "pause" | "resume" | "next" | "previous" | "stop" | "volume_up" |
                                                "volume_down" | "shuffle_on" | "shuffle_off": a short sentence to say
    await spotify.command(action, percent=None) -> (done, sentence)
    await spotify.find(query, kind) -> Choice   search / their library (SpotifyError)
    await spotify.play_choice(choice)           plays it on LittleBot's receiver (SpotifyError)
    await spotify.ready_to_play() -> (device, busy_elsewhere)
    await spotify.now_playing(fresh=False) -> dict | None;  spotify.snapshot() -> dict | None (no network)
    spotify.begin_connect(client_id) -> url;  await spotify.finish_connect(pasted);  spotify.disconnect()
    spotify.status() -> dict                    never a token

Playing: a song plays from its album (so the next songs follow; librespot's autoplay is off), an artist, album or
playlist as itself, liked songs / favourites as a shuffled list. ``play_choice`` sends ``PUT /me/player/play`` with
LittleBot's device id (transferring playback to the robot); when the device isn't in ``/me/player/devices`` it says
plainly that the receiver isn't running on the robot. Songs marked explicit are skipped unless
``skills.spotify_explicit`` (albums and playlists play as they are: Spotify's own filter covers those).

Ducking: when the wake word is heard while Spotify plays on LittleBot, its volume goes down to
``skills.spotify_duck_percent`` (Web API volume) and comes back when the voice session ends (``assistant.session``
{active: false} or ``live.call_ended``). Paused or stopped meanwhile: it comes back when the music carries on. A
volume the user chose meanwhile ("turn it up") wins. Music started while a session is on starts ducked.

Spotify also pauses on the parent's STOP ALL and when a video or an activity starts (they would play over each
other). Nothing about the child goes to Spotify: only search words and playback commands. Gated like the cloud
services: never in offline mode, only while cloud communication is on.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from littlebot.skills.base import Skill, ToolSpec
from littlebot.skills.spotify_api import (
    ERRORS,
    REDIRECT_URI,
    SpotifyAPI,
    SpotifyAuth,
    SpotifyError,
    clean_client_id,
)

log = logging.getLogger("littlebot.skills.spotify")

WAKE_TOPIC = "audio.wake_word"  # littlebot/audio/wakeword.py
SESSION_TOPIC = "assistant.session"  # littlebot/assistant/events.py SESSION {active, reason}
CALL_STARTED = "live.call_started"  # littlebot/chatgpt/live.py
CALL_ENDED = "live.call_ended"
STOP_ALL = "system.stop_all"
MEDIA_STARTED = "media.started"
ACTIVITY_STARTED = "activity.started"

CLIENT_ID_SETTING = "spotify.client_id"
TOKEN_FILE = "spotify_token.json"
DEVICE_TTL_S = 60.0  # a device seen is trusted this long
NO_DEVICE_TTL_S = 8.0  # ... a device not seen, this long
STATE_TTL_S = 4.0  # now_playing() reuses what it last heard this long
PLAYING_TRUST_S = 20 * 60.0  # playing() trusts what it last heard this long
POLL_S = 30.0  # while Spotify plays on LittleBot: check now and then (it ended, paused from a phone)
WAKE_GRACE_S = 20.0  # a wake word with no voice session after it this long: the volume comes back
VOLUME_STEP = 10
SEARCH_LIMIT = 8
LIBRARY_LIMIT = 50
PLAYLIST_PAGES = 3
MATCH_MIN = 0.72  # a playlist name scoring less than this isn't the one they meant
CARD_WAIT_S = 0.5
WAKE_RECEIVER_S = 0.6  # after waking an idle receiver with a transfer, before playing again
LIBRARY_KINDS = ("liked_songs", "my_playlist", "my_favorites")
CONTROLS = ("pause", "resume", "next", "previous", "stop", "volume_up", "volume_down", "set_volume",
            "shuffle_on", "shuffle_off")
ALIASES = {"play": "resume", "unpause": "resume", "continue": "resume", "carry_on": "resume", "skip": "next",
           "next_song": "next", "back": "previous", "prev": "previous", "previous_song": "previous",
           "louder": "volume_up", "quieter": "volume_down", "shuffle": "shuffle_on", "volume": "set_volume"}
# Spotify's own personalised playlists, which apps can no longer open (Web API changes of November 2024).
SPOTIFY_MADE = re.compile(r"\b(discover weekly|release radar|daily mix|on repeat|repeat rewind|time capsule|"
                          r"daylist|your top songs|blend|dj)\b", re.I)
_FILLER = frozenset("my the a an some please playlist playlists songs song music list on spotify of".split())
_NOT_ARTIST = frozenset({"the", "a", "my", "me", "you", "us", "him", "her", "them", "your side", "my side"})


# ---------------------------------------------------------------- what to play
@dataclass
class Choice:
    """What a request found: a context (album, artist, playlist) or a list of tracks, and how to say it."""

    what: str  # "track" | "album" | "artist" | "playlist" | "tracks"
    title: str
    by: str = ""
    context_uri: str | None = None
    uris: list[str] = field(default_factory=list)
    offset_uri: str | None = None
    image_url: str = ""
    album: str = ""
    duration_ms: int | None = None
    explicit: bool = False
    first_title: str = ""  # a list: the song it starts with (the card shows it)
    first_by: str = ""

    def said(self) -> str:
        if self.what == "tracks":
            return self.title
        return f"'{self.title}'" + (f" by {self.by}" if self.by else "")

    def body(self) -> dict[str, Any]:
        if self.context_uri:
            out: dict[str, Any] = {"context_uri": self.context_uri}
            if self.offset_uri:
                out["offset"] = {"uri": self.offset_uri}
            return out
        return {"uris": list(self.uris)}

    def card_spec(self) -> dict[str, Any]:
        """The music card before Spotify says what plays (a song: it; a list: its first song; an album, artist or
        playlist: its name, until the first song is known)."""
        title = self.first_title or self.title
        by = self.first_by if self.first_title else self.by
        return {"card": "music", "title": title, "artist": by, "album": self.album, "image_url": self.image_url,
                "duration_ms": self.duration_ms if self.what in ("track", "tracks") else None, "progress_ms": 0,
                "playing": True}


def clean(text: Any, limit: int = 100) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).split())[:limit].strip(" .,!?")


def norm(text: Any) -> str:
    """Lower-case words without punctuation or filler ("my Road Trip playlist!" -> "road trip")."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", ""))
    kept = [w for w in words if w not in _FILLER]
    return " ".join(kept or words)


def similar(a: Any, b: Any) -> float:
    """How alike two names are, 0..1 (filler words ignored; one inside the other counts a lot)."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    xs, ys = set(x.split()), set(y.split())
    if xs <= ys or ys <= xs:
        return 0.9 - 0.04 * abs(len(xs) - len(ys))
    return round(difflib.SequenceMatcher(None, x, y).ratio() * 0.9, 3)


def split_by(query: str) -> tuple[str, str]:
    """("Hello", "Adele") from "Hello by Adele" (the last "by": "Stand by Me by Ben E. King"); (query, "") without
    one, or when it is part of the title ("Stand by Me")."""
    m = re.match(r"^(.+)\s+by\s+(.+?)$", query, re.I)
    if not m or norm(m.group(2)) in _NOT_ARTIST:
        return query, ""
    return m.group(1).strip(), m.group(2).strip()


def _image(obj: Any) -> str:
    images = obj.get("images") if isinstance(obj, dict) else None
    if not isinstance(images, list):
        return ""
    sized = [i for i in images if isinstance(i, dict) and isinstance(i.get("url"), str)]
    if not sized:
        return ""
    # the smallest picture that is still at least 300 px (the card shows it at ~280)
    sized.sort(key=lambda i: int(i.get("width") or 0) or 640)
    for img in sized:
        if int(img.get("width") or 640) >= 300:
            return str(img["url"])
    return str(sized[-1]["url"])


def _names(objs: Any) -> str:
    return ", ".join(str(a.get("name")) for a in objs or [] if isinstance(a, dict) and a.get("name"))[:80]


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _items(data: Any, key: str) -> list[dict[str, Any]]:
    """The non-null objects of a search answer (Spotify sometimes sends nulls in ``items``)."""
    block = data.get(key) if isinstance(data, dict) else None
    items = block.get("items") if isinstance(block, dict) else None
    return [i for i in items or [] if isinstance(i, dict) and i.get("uri")]


def mmss(ms: Any) -> str:
    try:
        s = max(0, int(ms) // 1000)
    except (TypeError, ValueError):
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- the skill
class Spotify(Skill):
    name = "spotify"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic,
                 transport: httpx.AsyncBaseTransport | None = None, token_path: Path | None = None,
                 rng: random.Random | None = None) -> None:
        super().__init__(ctx, now=now)
        self._clock = clock
        self._rng = rng or random.Random()
        path = token_path or _token_path(ctx)
        self.auth = SpotifyAuth(path, transport=transport, now=now)
        self.api = SpotifyAPI(self.auth, transport=transport, timeout=lambda: getattr(self.cfg,
                                                                                     "spotify_timeout_seconds", 6.0))
        self._device: dict[str, Any] | None = None
        self._device_at: float | None = None
        self._state: dict[str, Any] | None = None  # what /me/player last said (normalised), see _parse_player
        self._state_at: float | None = None
        self._voice = False  # someone is talking to LittleBot (wake word .. session end)
        self._session_seen = False
        self._duck_from: int | None = None  # the volume to bring back after the voice session
        self._duck_to: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Any] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._poller: asyncio.Task[Any] | None = None
        self._wake_timer: asyncio.TimerHandle | None = None
        self.last_error: str | None = None
        try:
            ctx.settings.register_validator(CLIENT_ID_SETTING, _validate_client_id)
        except Exception:  # noqa: BLE001 - a context without settings (some tests)
            log.debug("spotify: no settings to validate", exc_info=True)

    # ------------------------------------------------------------ set-up and gates
    @property
    def cfg(self) -> Any:
        return self.ctx.config.skills

    def device_name(self) -> str:
        return str(getattr(self.cfg, "spotify_device_name", "") or "LittleBot").strip() or "LittleBot"

    def client_id(self) -> str:
        """The Client ID: the dashboard's (settings), else the config's, else the one the sign-in used."""
        value = ""
        try:
            value = str(self.ctx.settings.get(CLIENT_ID_SETTING) or "")
        except Exception:  # noqa: BLE001
            value = ""
        return clean_client_id(value) or clean_client_id(getattr(self.cfg, "spotify_client_id", "")) or \
            (self.auth.client_id or "")

    def set_client_id(self, value: Any) -> str:
        """Save the Client ID (the dashboard). Raises SpotifyError("bad_client_id")."""
        cid = clean_client_id(value)
        if cid is None:
            raise SpotifyError("bad_client_id")
        self.ctx.settings.set(CLIENT_ID_SETTING, cid, source="dashboard")
        return cid

    def blocked_reason(self) -> str | None:
        if not bool(getattr(self.cfg, "spotify", True)):
            return "off"
        if not self.auth.connected:
            return "no_client_id" if not self.client_id() else "not_connected"
        if self.ctx.config.is_offline:
            return "offline"
        try:
            if not self.ctx.privacy.cloud_enabled:
                return "cloud_off"
        except Exception:  # noqa: BLE001
            return "cloud_off"
        return None

    def blocked_text(self) -> str:
        reason = self.blocked_reason()
        return ERRORS[reason] if reason else ""

    def available(self) -> bool:
        return self.blocked_reason() is None

    def tools(self) -> list[ToolSpec]:
        return []  # the music skill's play_music / music_control / whats_playing play and steer Spotify

    def context_note(self) -> str:
        st = self.snapshot()
        if st is None or not st.get("on_littlebot") or not st.get("title"):
            return ""
        what = f"'{st['title']}'" + (f" by {st['artist']}" if st.get("artist") else "")
        if not st.get("playing"):
            return f"Spotify: {what} is paused on LittleBot (music_control resume carries on)."
        quiet = " (turned down while you talk)" if self._duck_from is not None else ""
        return (f"Spotify: playing {what} on LittleBot{quiet} (music_control: pause, next, previous, volume, "
                f"shuffle; whats_playing).")

    # ------------------------------------------------------------ connecting (the dashboard)
    def begin_connect(self, client_id: Any = None) -> str:
        """The authorize URL to open (saves a new Client ID first). Raises SpotifyError."""
        if client_id not in (None, ""):
            self.set_client_id(client_id)
        cid = self.client_id()
        if not cid:
            raise SpotifyError("no_client_id")
        return self.auth.begin(cid)

    async def finish_connect(self, pasted: str) -> dict[str, Any]:
        await self.auth.finish(pasted)
        self._device = self._device_at = None
        self._state = self._state_at = None
        self.last_error = None
        return self.status()

    def disconnect(self) -> bool:
        self._device = self._device_at = None
        self._state = self._state_at = None
        self._duck_from = self._duck_to = None
        return self.auth.disconnect()

    def status(self) -> dict[str, Any]:
        """For the dashboard and logs: never a token."""
        reason = self.blocked_reason()
        dev = self._device
        seen = self._device_at is not None
        st = self.snapshot()
        return {**super().status(), "enabled": bool(getattr(self.cfg, "spotify", True)), "blocked": reason,
                "blocked_text": ERRORS[reason] if reason else "", "client_id": self.client_id() or None,
                **self.auth.status(), "device_name": self.device_name(),
                "device": None if not seen else {"visible": dev is not None,
                                                 "active": bool(dev and dev.get("is_active")),
                                                 "volume": _int(dev.get("volume_percent")) if dev else None},
                "now": st, "ducked": self._duck_from is not None, "provider": music_provider(self.ctx),
                "last_error": self.last_error}

    async def refresh(self) -> dict[str, Any]:
        """Ask Spotify for the device and what's playing (the dashboard), then ``status()``. Never raises."""
        if self.available():
            try:
                await self.device(fresh=self._device_at is None or self._clock() - self._device_at > 10)
                await self.now_playing(fresh=True)
                self.last_error = None
            except SpotifyError as e:
                self.last_error = str(e)
        return self.status()

    # ------------------------------------------------------------ the receiver
    async def device(self, *, fresh: bool = False) -> dict[str, Any] | None:
        """LittleBot's own Spotify Connect device ({id, name, is_active, volume_percent, ...}), None when Spotify
        doesn't see it. Raises SpotifyError."""
        now = self._clock()
        if not fresh and self._device_at is not None:
            ttl = DEVICE_TTL_S if self._device is not None else NO_DEVICE_TTL_S
            if now - self._device_at < ttl:
                return self._device
        data = await self.api.call("GET", "/me/player/devices")
        devices = data.get("devices") if isinstance(data, dict) else None
        want = self.device_name().casefold()
        found = None
        for d in devices or []:
            if isinstance(d, dict) and str(d.get("name") or "").strip().casefold() == want and d.get("id"):
                found = d
                break
        if found is None and self._device is not None:
            log.info("spotify: the %s receiver isn't visible to Spotify", self.device_name())
        self._device, self._device_at = found, self._clock()
        return found

    async def ready_to_play(self) -> tuple[dict[str, Any] | None, bool]:
        """(LittleBot's device or None, Spotify is playing on another device right now). Raises SpotifyError."""
        dev, st = await asyncio.gather(self.device(), self.now_playing(fresh=True))
        busy = bool(st and st.get("playing") and not st.get("on_littlebot"))
        return dev, busy

    async def _device_or_fail(self) -> dict[str, Any]:
        dev = await self.device()
        if dev is None:
            dev = await self.device(fresh=True)
        if dev is None:
            raise SpotifyError("no_device")
        if dev.get("is_restricted"):
            raise SpotifyError("restricted")
        return dev

    # ------------------------------------------------------------ what's playing
    def _on_us(self, device: Any) -> bool:
        if not isinstance(device, dict):
            return False
        if self._device is not None and device.get("id") and device.get("id") == self._device.get("id"):
            return True
        return str(device.get("name") or "").strip().casefold() == self.device_name().casefold()

    def _parse_player(self, data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        device = data.get("device") if isinstance(data.get("device"), dict) else {}
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        show = item.get("show") if isinstance(item.get("show"), dict) else {}
        on_us = self._on_us(device)
        if on_us and device.get("id"):
            self._device = {**(self._device or {}), **device}
            self._device_at = self._clock()
        return {"title": clean(item.get("name"), 120) or None,
                "artist": _names(item.get("artists")) or clean(show.get("name"), 80),
                "album": clean(album.get("name"), 120), "image_url": _image(album) or _image(item) or _image(show),
                "uri": str(item.get("uri") or ""), "duration_ms": _int(item.get("duration_ms")),
                "progress_ms": _int(data.get("progress_ms")) or 0, "playing": bool(data.get("is_playing")),
                "on_littlebot": on_us, "device_name": clean(device.get("name"), 60),
                "volume": _int(device.get("volume_percent")), "shuffle": bool(data.get("shuffle_state")),
                "measured": self._clock()}

    async def now_playing(self, *, fresh: bool = False) -> dict[str, Any] | None:
        """What Spotify plays now (on any of the owner's devices: ``on_littlebot`` says whether it is the robot),
        None for nothing. Raises SpotifyError."""
        if not fresh and self._state_at is not None and self._clock() - self._state_at < STATE_TTL_S:
            return self.snapshot(trust=STATE_TTL_S)
        data = await self.api.call("GET", "/me/player", params={"additional_types": "episode"})
        self._state, self._state_at = self._parse_player(data), self._clock()
        return self.snapshot(trust=STATE_TTL_S)

    def snapshot(self, *, trust: float = PLAYING_TRUST_S) -> dict[str, Any] | None:
        """What it last heard, with the position moved on by the clock (no network); None for nothing known."""
        st, at = self._state, self._state_at
        if st is None or at is None or self._clock() - at > trust:
            return None
        out = dict(st)
        if out.get("playing"):
            moved = int((self._clock() - float(out.get("measured") or at)) * 1000)
            out["progress_ms"] = int(out.get("progress_ms") or 0) + max(0, moved)
            if out.get("duration_ms"):
                out["progress_ms"] = min(out["progress_ms"], int(out["duration_ms"]))
            out["measured"] = self._clock()
        return out

    def playing(self) -> bool:
        """Spotify is playing on LittleBot (what it last heard; no network: cheap enough for every utterance)."""
        if not self.available():
            return False
        st = self.snapshot()
        return bool(st and st.get("playing") and st.get("on_littlebot"))

    def paused(self) -> bool:
        """Spotify's music on LittleBot is paused ("carry on" is about it; what it last heard, no network)."""
        if not self.available():
            return False
        st = self.snapshot()
        return bool(st and not st.get("playing") and st.get("on_littlebot") and st.get("title"))

    def _set_state(self, **changes: Any) -> None:
        if self._state is None:
            return
        snap = self.snapshot(trust=PLAYING_TRUST_S) or dict(self._state)
        snap.update(changes)
        self._state, self._state_at = snap, self._clock()

    # ------------------------------------------------------------ finding music
    async def find(self, query: str, kind: str | None = None) -> Choice:
        """What to play for ``query`` (a ``play_music`` kind, or a guess). Raises SpotifyError."""
        q = clean(query)
        k = (kind or "").strip().lower() or None
        if k == "liked_songs":
            return await self._liked()
        if k == "my_favorites":
            return await self._favorites()
        if k == "my_playlist":
            return await self._my_playlist(q)
        if not q:
            raise SpotifyError("nothing_found")
        if k is None:
            from littlebot.skills.music import guess_kind  # noqa: PLC0415

            k = guess_kind(q)
        if k == "playlist":
            try:
                return await self._my_playlist(q)
            except SpotifyError as e:
                if e.kind not in ("no_playlist", "empty_library"):
                    raise
            return await self._search_playlist(q)
        if k == "artist":
            return await self._search_artist(q)
        if k == "album":
            return await self._search_album(q)
        if k in ("genre", "radio"):
            return await self._search_playlist(q)
        return await self._search_song(q)

    def _explicit_ok(self) -> bool:
        return bool(getattr(self.cfg, "spotify_explicit", False))

    def _fits(self, track: dict[str, Any]) -> bool:
        if track.get("is_playable") is False or track.get("is_local"):
            return False
        return self._explicit_ok() or not track.get("explicit")

    async def _search(self, q: str, types: str) -> Any:
        return await self.api.call("GET", "/search", params={"q": q[:200], "type": types, "limit": SEARCH_LIMIT,
                                                             "market": "from_token"})

    def _track_choice(self, t: dict[str, Any]) -> Choice:
        album = t.get("album") if isinstance(t.get("album"), dict) else {}
        ctx_uri = str(album.get("uri") or "") or None
        return Choice("track", clean(t.get("name"), 120) or "that song", by=_names(t.get("artists")),
                      context_uri=ctx_uri, uris=[] if ctx_uri else [str(t["uri"])],
                      offset_uri=str(t["uri"]) if ctx_uri else None, image_url=_image(album),
                      album=clean(album.get("name"), 120), duration_ms=_int(t.get("duration_ms")),
                      explicit=bool(t.get("explicit")))

    async def _search_song(self, q: str) -> Choice:
        title, artist = split_by(q)
        data = await self._search(f"track:{title} artist:{artist}" if artist else q,
                                  "track" if artist else "track,artist")
        tracks = _items(data, "tracks")
        fit = [t for t in tracks if self._fits(t)]
        if not artist:  # "play Taylor Swift" said as a song: the artist
            artists = _items(data, "artists")
            if artists and similar(artists[0].get("name"), q) >= 0.99 and \
                    not any(similar(t.get("name"), q) >= 0.99 for t in fit[:3]):
                return self._artist_choice(artists[0])
        if not fit:
            if tracks:
                raise SpotifyError("explicit")
            if artist:  # the field search found nothing: try the words as they were said
                return await self._search_song_loose(q)
            raise SpotifyError("nothing_found")
        n = len(fit)

        def rank(i_t: tuple[int, dict[str, Any]]) -> float:
            i, t = i_t
            s = similar(t.get("name"), title) * 2 + 0.4 * (1 - i / n)
            if artist:
                s += similar(_names(t.get("artists")), artist)
            return s

        best = max(enumerate(fit), key=rank)[1]
        return self._track_choice(best)

    async def _search_song_loose(self, q: str) -> Choice:
        tracks = _items(await self._search(q, "track"), "tracks")
        fit = [t for t in tracks if self._fits(t)]
        if not fit:
            raise SpotifyError("explicit" if tracks else "nothing_found")
        return self._track_choice(fit[0])

    def _artist_choice(self, a: dict[str, Any]) -> Choice:
        return Choice("artist", clean(a.get("name"), 80) or "them", context_uri=str(a["uri"]), image_url=_image(a))

    async def _search_artist(self, q: str) -> Choice:
        artists = _items(await self._search(q, "artist"), "artists")
        if not artists:
            raise SpotifyError("nothing_found")
        best = max(enumerate(artists), key=lambda i_a: similar(i_a[1].get("name"), q) + 0.3 * (1 - i_a[0] / 8))[1]
        return self._artist_choice(best)

    async def _search_album(self, q: str) -> Choice:
        title, artist = split_by(q)
        albums = _items(await self._search(f"album:{title} artist:{artist}" if artist else q, "album"), "albums")
        if not albums and artist:
            albums = _items(await self._search(q, "album"), "albums")
        if not albums:
            raise SpotifyError("nothing_found")
        best = max(enumerate(albums), key=lambda i_a: similar(i_a[1].get("name"), title) + 0.3 * (1 - i_a[0] / 8))[1]
        return Choice("album", clean(best.get("name"), 120) or "that album", by=_names(best.get("artists")),
                      context_uri=str(best["uri"]), image_url=_image(best), album=clean(best.get("name"), 120))

    def _playlist_choice(self, p: dict[str, Any]) -> Choice:
        owner = p.get("owner") if isinstance(p.get("owner"), dict) else {}
        return Choice("playlist", clean(p.get("name"), 120) or "that playlist",
                      by="" if str(owner.get("id") or "") == "spotify" else clean(owner.get("display_name"), 60),
                      context_uri=str(p["uri"]), image_url=_image(p))

    async def _search_playlist(self, q: str) -> Choice:
        lists = _items(await self._search(q, "playlist"), "playlists")
        lists = [p for p in lists if int((p.get("tracks") or {}).get("total") or 1) > 0]
        if not lists:
            raise SpotifyError("nothing_found")
        best = max(enumerate(lists), key=lambda i_p: similar(i_p[1].get("name"), q) * 0.5 + (1 - i_p[0] / 8))[1]
        return self._playlist_choice(best)

    async def _my_playlist(self, q: str) -> Choice:
        if not norm(q):
            raise SpotifyError("no_playlist")
        mine: list[dict[str, Any]] = []
        for page in range(PLAYLIST_PAGES):
            data = await self.api.call("GET", "/me/playlists", params={"limit": LIBRARY_LIMIT,
                                                                       "offset": page * LIBRARY_LIMIT})
            items = [p for p in (data.get("items") if isinstance(data, dict) else None) or []
                     if isinstance(p, dict) and p.get("uri")]
            mine += items
            if not isinstance(data, dict) or not data.get("next") or not items:
                break
        scored = sorted(((similar(p.get("name"), q), i, p) for i, p in enumerate(mine)), key=lambda x: (-x[0], x[1]))
        if scored and scored[0][0] >= MATCH_MIN:
            return self._playlist_choice(scored[0][2])
        if SPOTIFY_MADE.search(q):
            raise SpotifyError("spotify_made")
        raise SpotifyError("no_playlist" if mine else "empty_library")

    async def _tracks_choice(self, tracks: list[dict[str, Any]], title: str) -> Choice:
        tracks = [t for t in tracks if isinstance(t, dict) and t.get("uri")]
        fit = [t for t in tracks if self._fits(t)]
        if not fit:
            raise SpotifyError("explicit" if tracks else "empty_library")
        self._rng.shuffle(fit)
        first = fit[0]
        album = first.get("album") if isinstance(first.get("album"), dict) else {}
        return Choice("tracks", title, uris=[str(t["uri"]) for t in fit[:LIBRARY_LIMIT]], image_url=_image(album),
                      album=clean(album.get("name"), 120), duration_ms=_int(first.get("duration_ms")),
                      first_title=clean(first.get("name"), 120), first_by=_names(first.get("artists")))

    async def _liked(self) -> Choice:
        data = await self.api.call("GET", "/me/tracks", params={"limit": LIBRARY_LIMIT, "market": "from_token"})
        items = (data.get("items") if isinstance(data, dict) else None) or []
        tracks = [i.get("track") for i in items if isinstance(i, dict) and isinstance(i.get("track"), dict)]
        return await self._tracks_choice(tracks, "their liked songs")

    async def _favorites(self) -> Choice:
        data = await self.api.call("GET", "/me/top/tracks", params={"limit": LIBRARY_LIMIT,
                                                                    "time_range": "medium_term"})
        tracks = [t for t in (data.get("items") if isinstance(data, dict) else None) or [] if isinstance(t, dict)]
        if not tracks:  # a new account: Spotify doesn't know their favourites yet; their liked songs instead
            return await self._liked()
        return await self._tracks_choice(tracks, "some of their favourite songs")

    # ------------------------------------------------------------ playing
    async def play_choice(self, choice: Choice, *, show_card: bool = True) -> None:
        """Play ``choice`` on LittleBot's receiver (taking playback over from another device). Raises SpotifyError."""
        dev = await self._device_or_fail()
        dev_id = str(dev["id"])
        duck = self._duck_level()
        vol = _int(dev.get("volume_percent"))
        quiet = self._voice and vol is not None and vol > duck
        if quiet:  # a voice session is on: start quietly, and come back up when it ends
            if self._duck_from is None:
                self._duck_from = vol
            self._duck_to = duck
            await self._try_volume(duck, dev_id)
        try:
            await self.api.call("PUT", "/me/player/play", params={"device_id": dev_id}, body=choice.body())
        except SpotifyError as e:
            if e.kind not in ("no_device", "no_active"):
                raise
            # librespot that has been idle a long time: wake it with a transfer, then play again
            self._device_at = None
            await self.api.call("PUT", "/me/player", body={"device_ids": [dev_id], "play": False})
            await asyncio.sleep(WAKE_RECEIVER_S)
            await self.api.call("PUT", "/me/player/play", params={"device_id": dev_id}, body=choice.body())
        if quiet:
            await self._try_volume(duck, dev_id)
        elif self._duck_from is not None and not self._voice:  # still down from a chat (it was paused then)
            self._later(self._restore_soon())
        log.info("spotify: playing a %s on %s", choice.what, self.device_name())
        self.last_error = None
        self._state = {"title": choice.first_title or choice.title, "artist": choice.first_by or choice.by,
                       "album": choice.album, "image_url": choice.image_url, "uri": choice.offset_uri or "",
                       "duration_ms": choice.duration_ms, "progress_ms": 0, "playing": True, "on_littlebot": True,
                       "device_name": self.device_name(), "volume": self._duck_to if quiet else vol,
                       "shuffle": False, "measured": self._clock()}
        self._state_at = self._clock()
        self._later(self._refresh_soon(1.5))
        if show_card:
            await self._show_card(choice)

    async def _show_card(self, choice: Choice) -> None:
        if getattr(self.ctx, "screens", None) is None:
            return
        task = asyncio.ensure_future(self.show(choice.card_spec()))
        try:
            await asyncio.wait_for(asyncio.shield(task), CARD_WAIT_S)
        except asyncio.TimeoutError:
            pass

    async def _restore_soon(self) -> None:
        await asyncio.sleep(0.5)  # Spotify says it plays a moment after it was asked
        await self._restore()

    async def _refresh_soon(self, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self.now_playing(fresh=True)
        except SpotifyError:
            pass

    # ------------------------------------------------------------ control
    async def control(self, action: str) -> str:
        """Do ``action`` to the music Spotify plays on LittleBot; a short sentence to say. Never raises."""
        _, said = await self.command(action)
        return said

    async def command(self, action: Any, percent: Any = None) -> tuple[bool, str]:
        """(done, a short sentence). Acts only on music playing (or paused) on LittleBot. Never raises."""
        a = str(action or "").strip().lower().replace(" ", "_")
        a = ALIASES.get(a, a)
        if a not in CONTROLS:
            return False, "Spotify can't do that."
        reason = self.blocked_reason()
        if reason:
            return False, _sentence(ERRORS[reason])
        try:
            return await self._command(a, percent)
        except SpotifyError as e:
            self.last_error = str(e)
            return False, _sentence(str(e))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never into the caller
            log.warning("spotify: %s failed", a, exc_info=True)
            return False, "Spotify didn't do that."

    async def _command(self, a: str, percent: Any) -> tuple[bool, str]:
        st = await self.now_playing(fresh=True)
        here = bool(st and st.get("on_littlebot"))
        dev_id = str((self._device or {}).get("id") or "")
        if a == "resume":
            if here and st and st.get("playing"):
                return True, "It's playing."
            if not here or not dev_id:
                return False, "Nothing is paused on Spotify here. Ask me for some music."
            await self.api.call("PUT", "/me/player/play", params={"device_id": dev_id})
            self._set_state(playing=True)
            if not self._voice:
                await self._restore()
            return True, "Playing again."
        if not here:
            if st and st.get("playing"):
                return False, f"Spotify is playing on {st.get('device_name') or 'another device'}, not on me."
            return False, "Spotify isn't playing on me right now."
        params = {"device_id": dev_id} if dev_id else None
        if a in ("pause", "stop"):
            if not (st and st.get("playing")):
                return True, "It's paused."
            await self.api.call("PUT", "/me/player/pause", params=params)
            self._set_state(playing=False)
            return True, "Paused." if a == "pause" else "Stopped."
        if a == "next":
            await self.api.call("POST", "/me/player/next", params=params)
            self._set_state(playing=True, progress_ms=0)
            self._later(self._refresh_soon(0.8))
            return True, "Next song."
        if a == "previous":
            await self.api.call("POST", "/me/player/previous", params=params)
            self._set_state(playing=True, progress_ms=0)
            self._later(self._refresh_soon(0.8))
            return True, "Back one song."
        if a in ("shuffle_on", "shuffle_off"):
            on = a == "shuffle_on"
            await self.api.call("PUT", "/me/player/shuffle",
                                params={"state": "true" if on else "false", **(params or {})})
            self._set_state(shuffle=on)
            return True, "Shuffle on." if on else "Shuffle off."
        # volume: from the real level (the one it comes back to after a chat), and the user's choice wins
        base = self._duck_from if self._duck_from is not None else (st or {}).get("volume")
        base = int(base) if isinstance(base, int) else 50
        if a == "set_volume":
            new = _percent(percent)
            if new is None:
                return False, "Say a volume from 0 to 100."
        else:
            new = max(0, min(100, base + (VOLUME_STEP if a == "volume_up" else -VOLUME_STEP)))
        self._duck_from = self._duck_to = None
        await self.api.call("PUT", "/me/player/volume", params={"volume_percent": new, **(params or {})})
        self._set_state(volume=new)
        if a == "volume_up" and new == base == 100:
            return True, "That's as loud as it goes."
        return True, f"Music volume {new}%."

    # ------------------------------------------------------------ ducking
    def _duck_level(self) -> int:
        try:
            return max(0, min(100, int(getattr(self.cfg, "spotify_duck_percent", 20))))
        except (TypeError, ValueError):
            return 20

    async def _try_volume(self, percent: int, dev_id: str | None = None) -> bool:
        try:
            await self.api.call("PUT", "/me/player/volume",
                                params={"volume_percent": int(percent), **({"device_id": dev_id} if dev_id else {})})
            return True
        except SpotifyError as e:
            log.info("spotify: volume change failed (%s)", e.kind)
            return False

    async def duck(self) -> bool:
        """Turn the music on LittleBot down for the voice (the wake word was heard). True when it did."""
        if self._duck_from is not None or not self.available():
            return False
        try:
            st = await self.now_playing(fresh=True)
        except SpotifyError:
            return False
        if not (st and st.get("playing") and st.get("on_littlebot")):
            return False
        vol, duck = st.get("volume"), self._duck_level()
        if not isinstance(vol, int) or vol <= duck:
            return False
        self._duck_from, self._duck_to = vol, duck
        if not await self._try_volume(duck, str((self._device or {}).get("id") or "") or None):
            self._duck_from = self._duck_to = None
            return False
        self._set_state(volume=duck)
        log.info("spotify: music turned down for the voice")
        if not self._voice:  # the session ended while this was asked: straight back up
            await self._restore()
        return True

    async def _restore(self) -> bool:
        """Bring the volume back after the voice session (only while it plays: paused, it waits)."""
        if self._duck_from is None or not self.available():
            return False
        try:
            st = await self.now_playing(fresh=True)
        except SpotifyError:
            return False
        if not (st and st.get("on_littlebot")):  # stopped, or playing elsewhere now: nothing to bring back
            self._duck_from = self._duck_to = None
            return False
        if not st.get("playing"):
            return False  # paused meanwhile: it comes back when the music carries on
        if isinstance(st.get("volume"), int) and self._duck_to is not None and abs(st["volume"] - self._duck_to) > 2:
            self._duck_from = self._duck_to = None  # someone changed it meanwhile (the phone): their choice wins
            return False
        back = self._duck_from
        if not await self._try_volume(back, str((self._device or {}).get("id") or "") or None):
            return False
        self._duck_from = self._duck_to = None
        self._set_state(volume=back)
        log.info("spotify: music volume back after the voice")
        return True

    def _voice_on(self, *, session: bool) -> None:
        self._voice = True
        if session:
            self._session_seen = True
        if self._wake_timer is not None:
            self._wake_timer.cancel()
            self._wake_timer = None

    def _voice_off(self) -> None:
        self._voice = False
        self._session_seen = False
        if self._wake_timer is not None:
            self._wake_timer.cancel()
            self._wake_timer = None
        if self._duck_from is not None:
            self._later(self._restore())

    def _on_wake(self, _event: Any) -> None:
        self._voice_on(session=False)
        if self._loop is not None:  # no voice session after the wake word (busy, mic off): don't stay quiet
            self._wake_timer = self._loop.call_later(WAKE_GRACE_S, self._wake_expired)
        if self.available() and (self.playing() or self._state is None or self._state_at is None
                                 or self._clock() - self._state_at > STATE_TTL_S):
            self._later(self.duck())

    def _wake_expired(self) -> None:
        self._wake_timer = None
        if self._voice and not self._session_seen:
            self._voice_off()

    def _on_session(self, event: Any) -> None:
        if event.get("active"):
            self._voice_on(session=True)
        else:
            self._voice_off()

    def _on_takeover(self, _event: Any) -> None:
        """A video, an activity or STOP ALL: Spotify on LittleBot pauses (they would play over each other)."""
        if self.playing():
            self._later(self.command("pause"))

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        for topic, fn in ((WAKE_TOPIC, self._on_wake), (SESSION_TOPIC, self._on_session),
                          (CALL_STARTED, lambda _e: self._voice_on(session=True)),
                          (CALL_ENDED, lambda _e: self._voice_off()), (STOP_ALL, self._on_takeover),
                          (MEDIA_STARTED, self._on_takeover), (ACTIVITY_STARTED, self._on_takeover)):
            self._subs.append(bus.subscribe_async(topic, fn, self._loop))
        self._poller = self._loop.create_task(self._poll())

    async def stop(self) -> None:
        for sub in self._subs:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._subs = []
        if self._wake_timer is not None:
            self._wake_timer.cancel()
            self._wake_timer = None
        tasks = [t for t in [self._poller, *self._tasks] if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poller = None
        await self.api.close()

    async def _poll(self) -> None:
        """While Spotify plays on LittleBot: notice it ended or was paused from a phone; a volume to bring back."""
        while True:
            await asyncio.sleep(POLL_S)
            if not self.available() or not (self.playing() or self._duck_from is not None):
                continue
            try:
                await self.now_playing(fresh=True)
            except SpotifyError:
                continue
            except Exception:  # noqa: BLE001
                log.debug("spotify: poll failed", exc_info=True)
                continue
            if self._duck_from is not None and not self._voice:
                await self._restore()

    def _later(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# ---------------------------------------------------------------- helpers
def _token_path(ctx: Any) -> Path:
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict) and extras.get("spotify_token_file"):
        return Path(extras["spotify_token_file"])
    return Path(ctx.config.data_path) / TOKEN_FILE


def _validate_client_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    cid = clean_client_id(value)
    if cid is None:
        raise ValueError(ERRORS["bad_client_id"])
    return cid


def _percent(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        m = re.search(r"\d{1,3}", value)
        value = int(m.group(0)) if m else None
    if not isinstance(value, (int, float)) or value != value:
        return None
    v = int(round(float(value)))
    return v if 0 <= v <= 100 else None


def _sentence(text: str) -> str:
    t = str(text or "").strip()
    return (t[:1].upper() + t[1:]).rstrip(".") + "." if t else ""


def music_provider(ctx: Any) -> str:
    """auto | spotify | youtube: the dashboard's choice (settings), else ``skills.music_provider``."""
    value = None
    try:
        value = ctx.settings.get("music.provider")
    except Exception:  # noqa: BLE001
        value = None
    value = str(value or getattr(ctx.config.skills, "music_provider", "auto") or "auto").strip().lower()
    return value if value in ("auto", "spotify", "youtube") else "auto"


def get_spotify(ctx: Any) -> Spotify | None:
    """The Spotify skill from ``ctx.skills`` (whether or not it is connected), or None."""
    hub = getattr(ctx, "skills", None)
    get = getattr(hub, "get", None)
    if get is None:
        return None
    try:
        skill = get("spotify")
    except Exception:  # noqa: BLE001
        return None
    return skill if isinstance(skill, Spotify) else None


__all__ = ["CONTROLS", "LIBRARY_KINDS", "REDIRECT_URI", "Choice", "Spotify", "SpotifyError", "get_spotify", "mmss",
           "music_provider", "norm", "similar", "split_by"]
