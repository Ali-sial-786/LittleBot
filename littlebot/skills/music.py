"""Music: "play some jazz", "play Bohemian Rhapsody", "put on Taylor Swift", "next song", "turn the music down".

``play_music(query, kind)`` searches YouTube through the media controller (``ctx.media.search``, the same yt-dlp
search and the same rules as videos: ``media.enabled``, offline mode, the age gate, the time limits), adds what
makes a search find music rather than videos ("official audio" for a song, "mix" for a genre ...), prefers a
result whose length fits (a song a few minutes, a mix an hour), and plays it with the other results as what comes
next. It plays with ``presentation="music"``: the player page shows a now-playing screen (the title, the channel,
LittleBot's eyes bobbing to it and equaliser bars) around a smaller player, so it feels like music rather than a
video (littlebot/media/static/player.js).

Starting music takes the screen and the speaker like a video, so it never starts mid-answer: the start is handed
to littlebot/skills/takeover.py, which a host with its own after-the-turn queue claims (``RobotToolbox``,
the assistant's session), else it starts by itself once LittleBot has said its line.

``music_control(action)``: pause, resume, next, stop, volume_up, volume_down (the media controller's).
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from typing import Any, Callable

from littlebot.skills import takeover
from littlebot.skills.base import STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.music")

KINDS = ("song", "artist", "genre", "playlist", "radio")
ACTIONS = ("pause", "resume", "next", "stop", "volume_up", "volume_down")
SEARCH_RESULTS = 8
ALTERNATIVES = 6
VOLUME_STEP = 0.1
LABEL = "music "  # take-over labels
# What a search for each kind gets added, so YouTube finds music rather than videos about it.
SUFFIX = {"song": "official audio", "artist": "best songs mix", "genre": "music mix", "playlist": "playlist mix",
          "radio": "radio live"}
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

    # ------------------------------------------------------------ the skill
    def _media(self) -> Any:
        media = getattr(self.ctx, "media", None)
        return media if media is not None and hasattr(media, "play_video") and hasattr(media, "search") else None

    def available(self) -> bool:
        if not bool(getattr(self.ctx.config.skills, "music", True)):
            return False
        media = self._media()
        if media is None:
            return False
        allowed = getattr(media, "search_allowed", None)
        try:
            return bool(allowed()) if callable(allowed) else True
        except Exception:  # noqa: BLE001
            return False

    def tools(self) -> list[ToolSpec]:
        return [
            tool("play_music", "Play music on LittleBot (from YouTube, with a now-playing screen): a song, an "
                 "artist, a genre or mood, a playlist or a radio-style stream. It starts as soon as you have "
                 "finished speaking, so say one short line ('Here's some jazz!').",
                 {"query": {**STR, "description": "what to play, e.g. 'Bohemian Rhapsody by Queen', 'Taylor Swift', "
                                                  "'jazz', 'relaxing piano', 'lofi'"},
                  "kind": {"type": "string", "enum": list(KINDS),
                           "description": "song (one track), artist (their best songs), genre (a style or mood mix), "
                                          "playlist, radio (a live stream)"}},
                 ("query",)),
            tool("music_control", "Control the music that is playing: pause, resume, next (skip to another song), "
                 "stop, volume_up or volume_down.",
                 {"action": {"type": "string", "enum": list(ACTIONS)}}, ("action",)),
        ]

    def prompt_note(self) -> str:
        return ("Music: for 'play some jazz', 'play Bohemian Rhapsody', 'put on Taylor Swift' or 'play something "
                "relaxing' call play_music (with kind); music_control pauses, resumes, skips (next), stops it or "
                "turns it up or down. Use play_youtube only when they want to watch a video.")

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
        return {**super().status(), "query": self._query or None, "starting": self._starting}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return err("music isn't available on this robot")
        if name == "play_music":
            return await self._play_music(media, args)
        if name == "music_control":
            return await self._control(media, args)
        return await super().run(name, args)

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
        self._offer(media, first, others, title)
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

    def _offer(self, media: Any, first: dict[str, Any], others: list[dict[str, Any]], title: str) -> None:
        vid = str(first["video_id"])
        presentation = {"presentation": "music"} if _accepts(media.play_video, "presentation") else {}

        async def play() -> bool:
            self._starting = None
            await media.play_video(vid, title=first.get("title"), channel=first.get("channel"),
                                   duration_seconds=first.get("duration_seconds"), alternatives=others, **presentation)
            self._mine.add(f"search:{vid}")
            self._mine.update(f"search:{o.get('video_id')}" for o in others)
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
