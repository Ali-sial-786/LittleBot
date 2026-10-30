"""Music: "play some jazz", "play Bohemian Rhapsody", "put on Taylor Swift", "next song", "turn the music down".

Two places music comes from, chosen per request (``skills.music_provider`` / the dashboard's ``music.provider``):

* **Spotify** (littlebot/skills/spotify.py, the default when it is set up): the owner's Spotify Premium, played on
  the robot's own Spotify Connect receiver. ``auto`` uses it when it is connected and its receiver is visible
  (and isn't playing on another of the owner's devices right now: auto never takes that over), ``spotify``
  always, ``youtube`` only when asked. Their own library needs it: ``kind`` liked_songs ("play my liked songs"),
  my_playlist ("play my road trip playlist") and my_favorites ("play something I like"). Audio only, so it takes
  neither the screen nor LittleBot's speaker: it starts once LittleBot has said its line (a take-over with
  ``takeover=False``: the voice chat carries on, the music quietly under it until the chat ends), and a
  now-playing card (littlebot/screens/cards/music.py) shows it.
* **YouTube** (as before): ``ctx.media.search`` (the same yt-dlp search and the same rules as videos:
  ``media.enabled``, offline mode, the age gate, the time limits), with what makes a search find music rather than
  videos ("official audio" for a song, "mix" for a genre ...), a result whose length fits (a song a few minutes,
  a mix an hour), and the other results as what comes next. It plays with ``presentation="music"``: the player
  page shows a now-playing screen (the title, the channel, LittleBot's eyes bobbing to it and equaliser bars)
  around a smaller player (littlebot/media/static/player.js). It takes the screen and the speaker like a video,
  so it never starts mid-answer: the start is handed to littlebot/skills/takeover.py, which a host with its own
  after-the-turn queue claims (``RobotToolbox``, the assistant's session), else it starts by itself once
  LittleBot has said its line.

``play_music(query, kind, service)``: ``service`` "youtube" / "spotify" when they named one ("play it on
YouTube"). Starting one stops the other (they would play over each other).
``music_control(action, percent)``: pause, resume, next, previous, stop, volume_up, volume_down, set_volume,
shuffle_on, shuffle_off, for whichever is playing (previous and shuffle: Spotify only).
``whats_playing()``: the song, the artist and how far in, from either (Spotify's also goes on the screen).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any, Callable

from littlebot.skills import takeover
from littlebot.skills.base import INT, STR, Skill, ToolSpec, as_json, err, ok, tool

log = logging.getLogger("littlebot.skills.music")

KINDS = ("song", "artist", "album", "genre", "playlist", "radio")
LIBRARY_KINDS = ("liked_songs", "my_playlist", "my_favorites")  # the user's own Spotify library
SERVICES = ("spotify", "youtube")
ACTIONS = ("pause", "resume", "next", "stop", "volume_up", "volume_down")  # YouTube's
SPOTIFY_ACTIONS = ("previous", "shuffle_on", "shuffle_off")
ALL_ACTIONS = ("pause", "resume", "next", "previous", "stop", "volume_up", "volume_down", "set_volume",
               "shuffle_on", "shuffle_off")
READY_WAIT_S = 3.0  # auto: how long to wait for Spotify to say whether its receiver is there
SEARCH_RESULTS = 8
ALTERNATIVES = 6
VOLUME_STEP = 0.1
LABEL = "music "  # take-over labels
# What a search for each kind gets added, so YouTube finds music rather than videos about it.
SUFFIX = {"song": "official audio", "artist": "best songs mix", "album": "full album", "genre": "music mix",
          "playlist": "playlist mix", "radio": "radio live"}
_ALREADY = frozenset("official audio mix album playlist live radio lyrics lyric full songs hits".split())
GENRES = frozenset((
    "jazz lofi lo-fi classical rock pop rap country blues reggae edm house techno trance ambient chill chillout "
    "piano metal punk soul funk disco gospel bollywood qawwali ghazal latin salsa bachata kpop k-pop jpop j-pop "
    "indie folk opera christmas lullaby lullabies meditation acoustic dubstep drum bass grunge motown swing bebop "
    "bossa afrobeats afrobeat reggaeton soundtrack instrumental orchestral baroque sufi nasheed").split()) | {
    "hip hop", "r&b", "rnb", "drum and bass", "study music", "sleep music", "relaxing music", "dance music",
    "bossa nova", "smooth jazz", "film music", "video game music", "kids songs", "nursery rhymes", "white noise",
    "rain sounds", "nature sounds", "classic rock", "oldies", "80s", "90s", "70s", "60s", "2000s"}
# Words in a title that say it isn't the music itself.
NOT_MUSIC = ("reaction", "review", "tutorial", "lesson", "how to", "karaoke", "explained", "interview", "behind the",
             "making of", "podcast", "trailer", "unboxing", "slowed", "reverb", "8d audio", "nightcore", "sped up")


def _clean(text: Any, limit: int = 100) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).split())[:limit].strip(" .,!?")


def guess_kind(query: str) -> str:
    q = query.lower()
    if " by " in f" {q} ":
        return "song"
    if q in GENRES or q.removesuffix(" music") in GENRES or any(q == f"some {g}" for g in GENRES):
        return "genre"
    if re.search(r"\b(radio|station)\b", q):
        return "radio"
    if re.search(r"\bplaylist\b", q):
        return "playlist"
    return "song"


def music_query(query: str, kind: str | None) -> tuple[str, str]:
    """(what to search YouTube for, the kind): "jazz" -> ("jazz music mix", "genre")."""
    q = re.sub(r"(?i)^(?:some|a bit of|a little|the)\s+", "", query).strip()
    k = kind if kind in KINDS else guess_kind(q)
    words = set(re.findall(r"[a-z0-9]+", q.lower()))
    if words & _ALREADY:
        return q, k
    if k == "genre" and q.lower().endswith(" music"):
        return f"{q} mix", k
    return f"{q} {SUFFIX[k]}", k


def score(result: dict[str, Any], kind: str, query: str) -> float:
    """How much a search result looks like the music asked for (higher is better)."""
    title = str(result.get("title") or "").lower()
    channel = str(result.get("channel") or "").lower()
    d = result.get("duration_seconds")
    d = d if isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0 else None
    s = 0.0
    if kind == "song":
        s += 2 if d is not None and 60 <= d <= 900 else (-1 if d is not None and d > 1800 else 0)
    elif kind == "radio":
        s += 2 if d is None else (1 if d >= 3600 else 0)
    else:  # artist, genre, playlist: a long mix
        s += 2 if d is not None and d >= 1200 else (1 if d is not None and d >= 600 else 0)
    asked = query.lower()
    for w in NOT_MUSIC:
        if w in title and w not in asked:
            s -= 3
    if "cover" in title and "cover" not in asked:
        s -= 1
    if channel.endswith(" - topic") or "official audio" in title or "vevo" in channel:
        s += 1
    elif "official" in title:
        s += 0.5
    return s


def _accepts(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def speakable(title: Any) -> str:
    """A title the voice can say: no "(Official Audio)", "[HD]", hashtags or pipes."""
    s = re.sub(r"[\(\[][^)\]]*(official|audio|video|lyric|hd|4k|remaster|visualizer|mv)[^)\]]*[\)\]]", "",
               str(title or ""), flags=re.I)
    s = re.sub(r"#\w+", "", s)
    s = re.split(r"\s+[|•]\s+", s)[0]
    return " ".join(s.split()).strip(" -–—:") or "that"


class Music(Skill):
    name = "music"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time) -> None:
        super().__init__(ctx, now=now)
        self._query = ""
        self._kind = "song"
        self._played: list[str] = []  # video ids found for the last request (played or queued)
        self._mine: set[str] = set()  # media item ids this skill started ("search:<id>")
        self._starting: str | None = None  # the title waiting to start after the answer
        self._last: str | None = None  # "spotify" | "youtube": what this skill started last

    # ------------------------------------------------------------ the skill
    def _media(self) -> Any:
        media = getattr(self.ctx, "media", None)
        return media if media is not None and hasattr(media, "play_video") and hasattr(media, "search") else None

    def _spotify(self) -> Any:
        """The Spotify skill when it is connected and allowed now, else None."""
        try:
            from littlebot.skills.spotify import get_spotify  # noqa: PLC0415

            sp = get_spotify(self.ctx)
            return sp if sp is not None and sp.available() else None
        except Exception:  # noqa: BLE001
            return None

    def _youtube_ok(self) -> bool:
        media = self._media()
        if media is None:
            return False
        allowed = getattr(media, "search_allowed", None)
        try:
            return bool(allowed()) if callable(allowed) else True
        except Exception:  # noqa: BLE001
            return False

    def available(self) -> bool:
        if not bool(getattr(self.ctx.config.skills, "music", True)):
            return False
        return self._youtube_ok() or self._spotify() is not None

    def tools(self) -> list[ToolSpec]:
        spotify = self._spotify() is not None
        if spotify:
            play = ("Play music on LittleBot: a song, an artist, an album, a genre or mood, a playlist or a "
                    "radio-style mix, or the user's own Spotify music (kind liked_songs, my_playlist with its name, "
                    "my_favorites for 'something I like'). It plays from Spotify by default (YouTube when Spotify can't); set "
                    "service only when they name one ('play it on YouTube'). It starts as soon as you have finished "
                    "speaking, so say one short line ('Here's some jazz!').")
            kinds = list(KINDS + LIBRARY_KINDS)
            kind_help = ("song (one track), artist (their best songs), album, genre (a style or mood), playlist, radio "
                         "(a radio-style mix); their own Spotify: liked_songs, my_playlist (query: its name, e.g. "
                         "'Discover Weekly'), my_favorites (their most played)")
        else:
            play = ("Play music on LittleBot (from YouTube, with a now-playing screen): a song, an artist, an album, a "
                    "genre or mood, a playlist or a radio-style stream. It starts as soon as you have finished "
                    "speaking, so say one short line ('Here's some jazz!').")
            kinds = list(KINDS)
            kind_help = ("song (one track), artist (their best songs), album, genre (a style or mood mix), playlist, "
                         "radio (a live stream)")
        props: dict[str, Any] = {
            "query": {**STR, "description": "what to play, e.g. 'Bohemian Rhapsody by Queen', 'Taylor Swift', 'jazz', "
                                            "'relaxing piano', 'lofi'"},
            "kind": {"type": "string", "enum": kinds, "description": kind_help}}
        if spotify:
            props["service"] = {"type": "string", "enum": list(SERVICES),
                                "description": "only when they said where: 'on YouTube' or 'on Spotify'"}
        actions = list(ALL_ACTIONS) if spotify else [*ACTIONS, "set_volume"]
        return [
            tool("play_music", play, props, ("query",)),
            tool("music_control", "Control the music that is playing: pause, resume, next (skip to another song), "
                 "stop, volume_up, volume_down, set_volume (with percent)"
                 + (", previous (the song before), shuffle_on or shuffle_off." if spotify else "."),
                 {"action": {"type": "string", "enum": actions},
                  "percent": {**INT, "minimum": 0, "maximum": 100, "description": "set_volume only: 0 to 100"}},
                 ("action",)),
            tool("whats_playing", "What music is playing on LittleBot right now: the song, the artist and how far in"
                 + (" (it also shows it on the screen)." if spotify else "."), {}),
        ]

    def prompt_note(self) -> str:
        if self._spotify() is not None:
            return ("Music: for 'play some jazz', 'play Bohemian Rhapsody', 'put on Taylor Swift' or 'play something "
                    "relaxing' call play_music (with kind); it plays from the user's Spotify unless they ask for "
                    "YouTube. 'Play my liked songs' / 'play my Discover Weekly' / 'play something I like': kind "
                    "liked_songs / my_playlist / my_favorites. music_control pauses, resumes, skips (next), goes back "
                    "(previous), stops, shuffles or turns it up or down; whats_playing says what's on. Use "
                    "play_youtube only when they want to watch a video.")
        return ("Music: for 'play some jazz', 'play Bohemian Rhapsody', 'put on Taylor Swift' or 'play something "
                "relaxing' call play_music (with kind); music_control pauses, resumes, skips (next), stops it or "
                "turns it up or down; whats_playing says what's on. Use play_youtube only when they want to watch a "
                "video.")

    def context_note(self) -> str:
        if self._starting and any(p.startswith(LABEL) for p in takeover.pending(self.ctx)):
            return f"Music: '{self._starting}' starts once you have finished speaking."
        media = self._media()
        st = _safe_status(media)
        item = st.get("item") if isinstance(st.get("item"), dict) else None
        if item is None or st.get("state") in (None, "idle", "error"):
            return ""
        if item.get("presentation") != "music" and item.get("id") not in self._mine:
            return ""
        title = speakable(item.get("title"))
        by = f" ({item['channel']})" if item.get("channel") else ""
        if st.get("held"):
            return (f"Music: '{title}'{by} is paused while you talk; music_control resume carries on, stop ends it.")
        if st.get("state") == "paused":
            return f"Music: '{title}'{by} is paused (music_control resume, next or stop)."
        return f"Music: playing '{title}'{by} (music_control: pause, next, stop, volume_up, volume_down)."

    def status(self) -> dict[str, Any]:
        from littlebot.skills.spotify import music_provider  # noqa: PLC0415

        return {**super().status(), "query": self._query or None, "starting": self._starting,
                "provider": music_provider(self.ctx), "last_service": self._last}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in ("play_music", "music_control", "whats_playing"):
            return await super().run(name, args)
        media = self._media()
        if media is None and self._spotify() is None:
            return err("music isn't available on this robot")
        if name == "play_music":
            return await self._play(media, args)
        if name == "music_control":
            return await self._control_any(media, args)
        return await self._whats_playing(media)

    # ------------------------------------------------------------ which service
    async def _provider(self, service: str | None, kind: str | None) -> tuple[str | None, str]:
        """("spotify" | "youtube", "") or (None, why neither can play it)."""
        from littlebot.skills.spotify import SpotifyError, get_spotify, music_provider  # noqa: PLC0415

        sp = self._spotify()
        if service == "youtube":
            return "youtube", ""
        if service == "spotify" or kind in LIBRARY_KINDS:
            if sp is None:
                skill = get_spotify(self.ctx)
                why = skill.blocked_text() if skill is not None else ""
                why = why or "Spotify isn't set up on LittleBot"
                return None, why + ("; their own Spotify music needs it" if kind in LIBRARY_KINDS else "")
            return "spotify", ""
        pref = music_provider(self.ctx)
        if sp is None or pref == "youtube":
            return "youtube", ""
        if pref == "spotify" or not self._youtube_ok():
            return "spotify", ""
        try:  # auto: Spotify when its receiver is there and it isn't playing on another device right now
            dev, busy = await asyncio.wait_for(sp.ready_to_play(), READY_WAIT_S)
        except (SpotifyError, asyncio.TimeoutError) as e:
            log.info("music: Spotify not used (%s)", getattr(e, "kind", "timeout"))
            return "youtube", ""
        if dev is None:
            log.info("music: Spotify's receiver isn't visible; YouTube instead")
            return "youtube", ""
        if busy:
            log.info("music: Spotify is playing on another device; YouTube instead")
            return "youtube", ""
        return "spotify", ""

    async def _play(self, media: Any, args: dict[str, Any]) -> str:
        query = _clean(args.get("query"))
        kind = str(args.get("kind") or "").strip().lower() or None
        if kind is not None and kind not in KINDS + LIBRARY_KINDS:
            kind = None
        service = str(args.get("service") or "").strip().lower() or None
        if service not in SERVICES:
            service = None
        if not query and kind not in LIBRARY_KINDS:
            return err("say what music to play")
        which, why = await self._provider(service, kind)
        if which is None:
            return err(why)
        if which == "spotify":
            return await self._play_spotify(query, kind)
        if media is None:
            return err("music isn't available on this robot")
        if kind in LIBRARY_KINDS:  # (service youtube for "my liked songs": YouTube has no such thing)
            return err("their own liked songs, playlists and favourites are on Spotify, not YouTube")
        return await self._play_music(media, {"query": query, "kind": kind})

    # ------------------------------------------------------------ Spotify
    async def _play_spotify(self, query: str, kind: str | None) -> str:
        from littlebot.skills.spotify import SpotifyError  # noqa: PLC0415

        sp = self._spotify()
        if sp is None:
            return err("Spotify isn't set up on LittleBot")
        try:
            choice = await sp.find(query, kind)
        except SpotifyError as e:
            log.info("music: Spotify search failed (%s)", e.kind)
            return err(str(e))
        self._starting = choice.first_title or choice.title

        async def play() -> bool:
            self._starting = None
            await self._quiet_youtube()
            try:
                await sp.play_choice(choice)
            except SpotifyError as e:
                log.info("music: Spotify didn't start (%s)", e.kind)
                self._failed(str(e))
                return False
            self._last = "spotify"
            return True

        # audio only: after the answer, without ending the voice chat (takeover.py, takeover=False)
        takeover.offer(self.ctx, f"{LABEL}spotify", play, takeover=False)
        log.info("music: Spotify %s queued", choice.what)
        return ok(f"{choice.said()} starts playing on Spotify as soon as you have finished speaking; say it in one "
                  f"short line")

    async def _quiet_youtube(self) -> None:
        """Spotify is about to play: a video or YouTube music would play over it."""
        media = self._media()
        st = _safe_status(media)
        if media is not None and st.get("state") not in (None, "idle", "error"):
            try:
                await media.stop("spotify")
            except Exception:  # noqa: BLE001
                log.debug("music: stopping YouTube failed", exc_info=True)

    def _youtube_music_on(self, media: Any) -> bool:
        st = _safe_status(media)
        item = st.get("item") if isinstance(st.get("item"), dict) else None
        if item is None or st.get("state") in (None, "idle", "error"):
            return False
        return item.get("presentation") == "music" or item.get("id") in self._mine

    async def _which(self, media: Any) -> str:
        """Which one music_control / whats_playing is about: "spotify" or "youtube"."""
        sp = self._spotify()
        if sp is None:
            return "youtube"
        yt = media is not None and self._youtube_music_on(media)
        spotify_on = sp.playing()
        if spotify_on and not yt:
            return "spotify"
        if yt and not spotify_on:
            return "youtube"
        if yt and spotify_on:
            return self._last or "spotify"
        if media is None or self._last == "spotify":
            return "spotify"
        snap = sp.snapshot()
        if snap is None:  # nothing known: ask Spotify (one request)
            from littlebot.skills.spotify import SpotifyError  # noqa: PLC0415

            try:
                snap = await sp.now_playing(fresh=True)
            except SpotifyError:
                snap = None
        return "spotify" if snap and snap.get("on_littlebot") else "youtube"

    async def _control_any(self, media: Any, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action not in ALL_ACTIONS:
            return err(f"unknown action; use one of {', '.join(ALL_ACTIONS)}")
        if action in ("stop", "pause") and self._starting and takeover.drop(self.ctx, f"{LABEL}spotify"):
            self._starting = None
            return ok("the music won't start")
        if await self._which(media) == "spotify":
            sp = self._spotify()
            done, said = await sp.command(action, args.get("percent"))
            return ok(said) if done else err(said)
        if media is None:
            return ok("no music is playing")
        if action == "set_volume":
            return self._set_volume(media, args.get("percent"))
        if action in SPOTIFY_ACTIONS:
            return err("YouTube music can't do that; say next for another song, or ask for a song by name")
        return await self._control(media, args)

    def _set_volume(self, media: Any, percent: Any) -> str:
        from littlebot.skills.spotify import _percent  # noqa: PLC0415

        pct = _percent(percent)
        if pct is None:
            return err("give a volume from 0 to 100")
        try:
            new = float(media.set_volume(round(pct / 100, 3)))
        except ValueError as e:
            return err(str(e))
        return ok(f"music volume {round(new * 100)}%")

    # ------------------------------------------------------------ whats_playing
    async def _whats_playing(self, media: Any) -> str:
        from littlebot.skills.spotify import SpotifyError, mmss  # noqa: PLC0415

        if self._starting and any(p.startswith(LABEL) for p in takeover.pending(self.ctx)):
            return ok(f"'{self._starting}' starts as soon as you have finished speaking")
        if await self._which(media) == "spotify":
            sp = self._spotify()
            try:
                st = await sp.now_playing(fresh=True)
            except SpotifyError as e:
                return err(str(e))
            if st and st.get("on_littlebot") and st.get("title"):
                out: dict[str, Any] = {"service": "spotify", "song": st["title"], "artist": st.get("artist") or None,
                                       "album": st.get("album") or None,
                                       "state": "playing" if st.get("playing") else "paused"}
                if st.get("duration_ms"):
                    out["position"] = f"{mmss(st.get('progress_ms'))} of {mmss(st['duration_ms'])}"
                await self._show_music_card()
                return as_json({k: v for k, v in out.items() if v is not None})
        st = _safe_status(media)
        item = st.get("item") if isinstance(st.get("item"), dict) else None
        if media is not None and item is not None and self._youtube_music_on(media):
            state = "paused" if st.get("held") or st.get("state") == "paused" else "playing"
            return as_json({k: v for k, v in {"service": "youtube", "title": speakable(item.get("title")),
                                              "channel": item.get("channel") or None, "state": state}.items()
                            if v is not None})
        return ok("no music is playing")

    async def _show_music_card(self) -> None:
        if getattr(self.ctx, "screens", None) is None:
            return
        task = asyncio.ensure_future(self.show({"card": "music"}))
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ play_music
    async def _play_music(self, media: Any, args: dict[str, Any]) -> str:
        query = _clean(args.get("query"))
        if not query:
            return err("say what music to play")
        kind = str(args.get("kind") or "").strip().lower() or None
        blocked = _call(media, "video_blocked_reason")
        if blocked:
            return err(str(blocked))
        search, kind = music_query(query, kind)
        results, problem = await self._search(media, search)
        if problem:
            return err(problem)
        if not results:
            return err("no music found for that; try other words")
        ranked = sorted(enumerate(results), key=lambda r: (-score(r[1], kind, query), r[0]))
        picked = [r for _, r in ranked]
        first, others = picked[0], picked[1:1 + ALTERNATIVES]
        self._query, self._kind = query, kind
        self._played = [str(r.get("video_id")) for r in picked]
        title = speakable(first.get("title"))
        self._starting = title
        self._offer(media, first, others)
        log.info("music: %s for a %s (%d results)", "queued", kind, len(results))
        by = f" by {first['channel']}" if first.get("channel") and kind in ("song", "artist") else ""
        return ok(f"'{title}'{by} starts playing as soon as you have finished speaking; say it in one short line")

    async def _search(self, media: Any, text: str) -> tuple[list[dict[str, Any]], str | None]:
        from littlebot.media.search import SearchError  # noqa: PLC0415

        try:
            found = await media.search(text, limit=SEARCH_RESULTS)
        except PermissionError as e:
            return [], str(e)
        except SearchError as e:
            return [], {"unavailable": "music search isn't installed on this robot (yt-dlp)",
                        "timeout": "the music search took too long", "bad_query": "say what music to play"}.get(
                e.kind, "the music search didn't work")
        out = [r for r in found or [] if isinstance(r, dict) and r.get("video_id")]
        return out, None

    def _offer(self, media: Any, first: dict[str, Any], others: list[dict[str, Any]]) -> None:
        vid = str(first["video_id"])
        presentation = {"presentation": "music"} if _accepts(media.play_video, "presentation") else {}

        async def play() -> bool:
            self._starting = None
            await media.play_video(vid, title=first.get("title"), channel=first.get("channel"),
                                   duration_seconds=first.get("duration_seconds"), alternatives=others, **presentation)
            self._mine.add(f"search:{vid}")
            self._mine.update(f"search:{o.get('video_id')}" for o in others)
            self._last = "youtube"
            return True

        takeover.offer(self.ctx, f"{LABEL}{vid}", play, on_fail=self._failed)

    def _failed(self, why: str) -> None:
        self._starting = None
        self.announce(f"Sorry, the music couldn't start: {why.rstrip('.')}.", kind="music")

    # ------------------------------------------------------------ music_control
    async def _control(self, media: Any, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return err(f"unknown action; use one of {', '.join(ACTIONS)}")
        st = _safe_status(media)
        state = st.get("state") or "idle"
        held = bool(st.get("held"))
        if action == "stop":
            dropped = takeover.drop(self.ctx, LABEL)
            self._starting = None if dropped else self._starting
            if state in ("idle", None):
                return ok("the music won't start") if dropped else ok("no music is playing")
            await media.stop("stopped")
            return ok("stopped the music")
        if action in ("volume_up", "volume_down"):
            return self._volume(media, st, 1 if action == "volume_up" else -1)
        if state in ("idle", None, "error"):
            return ok("no music is playing")
        if action == "pause":
            if held or state == "paused":
                takeover.drop(self.ctx, LABEL)
                return ok("the music is paused")
            return ok("paused the music") if await media.pause() else ok("no music is playing")
        if action == "resume":
            if held:  # paused for this chat: it needs the screen and speaker back, like a new start
                takeover.offer(self.ctx, f"{LABEL}resume", lambda: media.resume(), on_fail=self._failed)
                return ok("the music carries on as soon as you have finished speaking")
            return ok("the music carries on") if await media.resume() else ok("the music isn't paused")
        # next
        if held:
            takeover.offer(self.ctx, f"{LABEL}next", lambda: self._next(media), on_fail=self._failed)
            return ok("the next song plays as soon as you have finished speaking")
        if not await self._next(media):
            return ok("there's nothing more to play for that; ask for something else")
        now = (_safe_status(media).get("item") or {}).get("title")
        return ok(f"next up: '{speakable(now)}'") if now else ok("skipped to the next song")

    async def _next(self, media: Any) -> bool:
        """Skip to the next result; when they are used up, search again for more of the same."""
        if int(_safe_status(media).get("queued") or 0) > 0:
            await media.skip()
            return True
        if not self._query:
            return False
        search, kind = music_query(self._query, self._kind)
        results, problem = await self._search(media, search)
        fresh = [r for r in results if str(r.get("video_id")) not in self._played]
        if problem or not fresh:
            return False
        fresh.sort(key=lambda r: -score(r, kind, self._query))
        self._played.extend(str(r.get("video_id")) for r in fresh)
        presentation = {"presentation": "music"} if _accepts(media.play_video, "presentation") else {}
        first = fresh[0]
        await media.play_video(str(first["video_id"]), title=first.get("title"), channel=first.get("channel"),
                               duration_seconds=first.get("duration_seconds"), alternatives=fresh[1:1 + ALTERNATIVES],
                               **presentation)
        self._mine.update(f"search:{r.get('video_id')}" for r in fresh[:1 + ALTERNATIVES])
        return True

    def _volume(self, media: Any, st: dict[str, Any], direction: int) -> str:
        try:
            now = float(st.get("volume"))
        except (TypeError, ValueError):
            now = 0.5
        try:
            new = float(media.set_volume(round(max(0.0, min(1.0, now + direction * VOLUME_STEP)), 3)))
        except ValueError as e:
            return err(str(e))
        pct = round(new * 100)
        if direction > 0 and new <= now + 1e-6:
            return ok(f"the music is as loud as it goes ({pct}%)")
        return ok(f"music volume {pct}%")


def _call(obj: Any, name: str) -> Any:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _safe_status(media: Any) -> dict[str, Any]:
    st = _call(media, "status") if media is not None else None
    return st if isinstance(st, dict) else {}


__all__ = ["ACTIONS", "KINDS", "Music", "guess_kind", "music_query", "score", "speakable"]
