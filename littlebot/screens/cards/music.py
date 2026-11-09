"""The music card: what Spotify plays on LittleBot now (littlebot/skills/spotify.py).

The album cover (as tall as the panel, on the left), the song in big letters (up to two lines), the artist and the
album, and along the bottom a progress bar that follows the clock with the times under it and a big play / pause
button between them; while it plays, equaliser bars dance beside the title, and paused, the cover dims and says so.
A tap anywhere (except on LittleBot's eyes, which close the card as on every card) pauses or carries on; the voice can
say "pause", "next" ... (``command``). LittleBot's own eyes stay in the presenter's box at the top left: music never
covers the face. The progress bar is a cached track and fill: a frame only moves the fill's edge and the knob.

Shown by the Spotify skill when the music starts (it lingers like any card once the chat is over), by
``whats_playing``, or by the brain for "what's playing?". The card reads the skill's last-known state every second
(no network) and asks Spotify again now and then (when a song should have ended, and every 20 s), on the robot's
loop. Without Spotify (not set up, nothing playing on LittleBot) there is no card.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, SMALL_PX, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import candy

log = logging.getLogger("littlebot.screens.music")

SYNC_S = 1.0  # read the skill's state this often (no network)
REFRESH_S = 20.0  # ask Spotify this often
ENDED_REFRESH_S = 3.0  # ... and this long after a song should have ended
BUILD_WAIT_S = 2.5
SONG_SIZES = (64, 58, TITLE_PX)  # the song: as big as fits in two lines (never under TITLE_PX; then "...")
ARTIST_PX = 44
ALBUM_PX = BODY_PX
COLUMN_GAP = 34  # between the cover and the words
BAR_H = 18  # the progress bar
KNOB_R = 15
BUTTON_R = 48  # the play / pause button (96 px across)
TIMES_GAP = 12  # under the bar
ACCENT: art.Color = art.MINT
_ACTIONS = {"pause": "pause", "resume": "resume", "play": "resume", "toggle": "toggle", "next": "next",
            "skip": "next", "previous": "previous", "back": "previous"}


def mmss(ms: Any) -> str:
    try:
        s = max(0, int(ms) // 1000)
    except (TypeError, ValueError):
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


def _num(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return max(0, int(value))


class MusicCard(Card):
    kind = "music"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *,
                 source: Callable[[], dict[str, Any] | None] | None = None,
                 command: Callable[[str], Awaitable[Any]] | None = None,
                 refresh: Callable[[], Awaitable[Any]] | None = None,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__(spec, host)
        self.title = "Now playing"
        self.mood = "happy"
        self._source, self._command, self._refresh, self._loop = source, command, refresh, loop
        self.song = self.artist = self.album = self.image_url = ""
        self.duration_ms: int | None = None
        self.progress_ms = 0
        self.playing = True
        self._at = self.host.now()  # when progress_ms was true
        self.slot: kit.PictureSlot | None = None
        self._apply({"title": spec.get("title"), "artist": spec.get("artist"), "album": spec.get("album"),
                     "image_url": spec.get("image_url"), "duration_ms": spec.get("duration_ms"),
                     "progress_ms": spec.get("progress_ms"), "playing": spec.get("playing", True)})
        self._since_sync = 0.0
        self._since_refresh = 0.0
        self._asked: Any = None  # a command on its way (a concurrent.futures.Future)
        self._asked_was: bool | None = None
        self._refreshing: Any = None
        self._flash = 0.0

    # ------------------------------------------------------------ state
    def _apply(self, st: dict[str, Any]) -> None:
        song = kit.clean(st.get("title"), 120)
        if song:
            self.song = song
        self.artist = kit.clean(st.get("artist"), 80)
        self.album = kit.clean(st.get("album"), 100)
        url = str(st.get("image_url") or "")[:500]
        if url != self.image_url:
            self.image_url = url
            self.slot = kit.PictureSlot(self.host, url=url) if url.startswith("https://") else None
        self.duration_ms = _num(st.get("duration_ms"))
        self.progress_ms = _num(st.get("progress_ms")) or 0
        self.playing = bool(st.get("playing"))
        self._at = self.host.now()

    def progress(self) -> int:
        """Where the song is now (ms), following the clock while it plays."""
        p = self.progress_ms + (int((self.host.now() - self._at) * 1000) if self.playing else 0)
        if self.duration_ms:
            p = min(p, self.duration_ms)
        return max(0, p)

    def fraction(self) -> float:
        return self.progress() / self.duration_ms if self.duration_ms else 0.0

    # ------------------------------------------------------------ frames
    def update(self, dt: float) -> None:
        super().update(dt)
        if self.slot is not None:
            self.slot.update(self.t)
        self._flash = max(0.0, self._flash - dt)
        self._since_sync += dt
        self._since_refresh += dt
        self._check_command()
        if self._asked is None and self._source is not None and self._since_sync >= SYNC_S:
            self._since_sync = 0.0
            try:
                st = self._source()
            except Exception:  # noqa: BLE001
                st = None
            if isinstance(st, dict) and st.get("on_littlebot", True) and st.get("title"):
                self._apply(st)
            elif isinstance(st, dict) and not st.get("on_littlebot", True):
                self.playing = False  # it moved to another speaker
        ended = bool(self.duration_ms) and self.progress() >= (self.duration_ms or 0) and self.playing
        if (self._since_refresh >= REFRESH_S or (ended and self._since_refresh >= ENDED_REFRESH_S)):
            self._since_refresh = 0.0
            self._ask_refresh()

    def _submit(self, coro: Any) -> Any:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:  # noqa: BLE001
            coro.close()
            log.debug("music card: couldn't reach the robot's loop", exc_info=True)
            return None

    def _ask_refresh(self) -> None:
        if self._refresh is None or (self._refreshing is not None and not self._refreshing.done()):
            return
        try:
            self._refreshing = self._submit(self._refresh())
        except Exception:  # noqa: BLE001
            self._refreshing = None

    def _check_command(self) -> None:
        fut = self._asked
        if fut is None or not fut.done():
            return
        self._asked = None
        try:
            res = fut.result()
        except Exception:  # noqa: BLE001
            res = (False, "Spotify didn't answer.")
        done, said = res if isinstance(res, tuple) and len(res) == 2 else (True, "")
        if not done:
            if self._asked_was is not None:
                self.playing = self._asked_was  # it didn't happen: as it was
                self._at = self.host.now()
            if said:
                self.host.tell(f"The music card: {said}")
        self._since_sync = SYNC_S  # read the skill's state on the next frame

    # ------------------------------------------------------------ touch and voice
    def _do(self, action: str) -> bool:
        if self._command is None or self._asked is not None:
            return False
        if action == "toggle":
            action = "pause" if self.playing else "resume"
        try:
            fut = self._submit(self._command(action))
        except Exception:  # noqa: BLE001
            fut = None
        if fut is None:
            return False
        self._asked, self._asked_was = fut, self.playing
        if action in ("pause", "resume"):
            self.progress_ms, self._at = self.progress(), self.host.now()
            self.playing = action == "resume"
        elif action in ("next", "previous"):
            self.progress_ms, self._at = 0, self.host.now()
        self._flash = 0.35
        self.host.sound("pop")
        return True

    def tap(self, x: float, y: float) -> bool:
        return self._do("toggle")

    def key(self, name: str) -> bool:
        if name in ("space", "return", "enter"):
            return self._do("toggle")
        if name == "right":
            return self._do("next")
        if name == "left":
            return self._do("previous")
        return False

    def command(self, action: str, args: dict[str, Any]) -> bool:
        a = _ACTIONS.get(str(action or "").strip().lower())
        return self._do(a) if a else False

    def describe(self) -> str:
        what = f"'{self.song}'" + (f" by {self.artist}" if self.artist else "") if self.song else "music"
        where = f", {mmss(self.progress())} of {mmss(self.duration_ms)}" if self.duration_ms else ""
        return f"Now playing on Spotify: {what}{where}" + ("" if self.playing else " (paused)")

    def status(self) -> dict[str, Any]:
        return {**super().status(), "song": self.song, "artist": self.artist, "playing": self.playing,
                "progress_ms": self.progress(), "duration_ms": self.duration_ms}

    # ------------------------------------------------------------ drawing
    @staticmethod
    def _inner() -> pygame.Rect:
        return candy.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)

    def art_rect(self) -> pygame.Rect:
        """The cover: a square as tall as the panel's inside, on the left."""
        r = self._inner()
        return pygame.Rect(r.x, r.y, r.h, r.h)

    def column(self) -> pygame.Rect:
        """Right of the cover: the words, then the bar, the times and the button along the bottom."""
        r, cover = self._inner(), self.art_rect()
        x = cover.right + COLUMN_GAP
        return pygame.Rect(x, r.y, r.right - x, r.h)

    def bar_rect(self) -> pygame.Rect:
        col = self.column()
        return pygame.Rect(col.x, col.bottom - 2 * BUTTON_R - TIMES_GAP - BAR_H, col.w, BAR_H)

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=57)
        dy = candy.slide_in(self.t)
        hy = int(candy.HEADER_Y + dy * 0.5)
        candy.header(surf, self.title, dy=dy * 0.5)
        if self.playing:
            self._eq(surf, (candy.HEADER_X + kit.text_w(self.title, TITLE_PX) + 26, hy))
        label = candy.chip("Spotify", SMALL_PX, art.WHITE, art.inkier(ACCENT, 0.35))
        surf.blit(label, label.get_rect(midright=(candy.PANEL.right - 6, hy)))
        candy.panel(surf, dy=dy)
        off = int(dy)
        self._cover(surf, self.art_rect().move(0, off))
        bar = self.bar_rect().move(0, off)
        col = self.column().move(0, off)
        self._words(surf, pygame.Rect(col.x, col.y, col.w, bar.y - 20 - col.y))
        self._bar(surf, bar)

    def _cover(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        if self.slot is not None and self.slot.wanted:
            self.slot.draw(surf, r, self.t, credit=False)
        if self.slot is None or not self.slot.shown:
            if self.slot is None or not self.slot.loading:
                surf.blit(art.rounded_rect(r.w, r.h, 30, art.mix(ACCENT, art.WHITE, 0.55), shadow=5,
                                           border=art.inkier(ACCENT, 0.2), border_w=3), r.topleft)
                art.blit_center(surf, _note(r.w * 9 // 20, art.inkier(ACCENT, 0.35)), r.center)
        if not self.playing:
            surf.blit(art.rounded_rect(r.w, r.h, 30, (60, 30, 55, 120)), r.topleft)
            chip = candy.chip("Paused", ARTIST_PX, art.WHITE, art.inkier(art.GRAPE, 0.25))
            surf.blit(chip, chip.get_rect(center=r.center))

    def _song_lines(self, width: int) -> tuple[int, list[str]]:
        song = self.song or "Music"
        size, lines = kit.fit(song, SONG_SIZES, width, max_lines=2)
        lines = list(lines)
        if len(lines) > 2:
            lines = [lines[0], kit.ellipsize(" ".join(lines[1:]), size, width)]
        return size, lines

    def _words(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        """The song, the artist and the album, as a block in the middle of the room above the bar."""
        size, lines = self._song_lines(r.w)
        imgs = [kit.text_block(lines, size, kit.TEXT, pitch=art.font(size).get_linesize() + 2)]
        gaps = [16]
        if self.artist:
            imgs.append(art.text(kit.ellipsize(self.artist, ARTIST_PX, r.w), ARTIST_PX, kit.TEXT_SOFT))
            gaps.append(8)
        if self.album and self.album != self.song:
            imgs.append(art.text(kit.ellipsize(self.album, ALBUM_PX, r.w), ALBUM_PX, kit.TEXT_SOFT))
            gaps.append(0)
        total = sum(i.get_height() for i in imgs) + sum(gaps[:len(imgs) - 1])
        y = r.y + max(0, (r.h - total) // 2)
        for img, gap in zip(imgs, gaps):
            surf.blit(img, (r.x, y))
            y += img.get_height() + gap

    def _bar(self, surf: pygame.Surface, bar: pygame.Rect) -> None:
        """The progress bar (a cached track and fill; only the fill's width and the knob move), the times under
        its ends and the play / pause button between them."""
        surf.blit(art.rounded_rect(bar.w, bar.h, bar.h // 2, art.mix(art.INK_SOFT, art.WHITE, 0.62)), bar.topleft)
        k = art.clamp(self.fraction(), 0.0, 1.0)
        if self.duration_ms:
            w = int(bar.w * k)
            if w >= bar.h // 2:
                fill = art.rounded_rect(bar.w, bar.h, bar.h // 2, ACCENT)
                surf.blit(fill, bar.topleft, pygame.Rect(0, 0, w, bar.h))
            x = bar.x + max(KNOB_R - 4, min(bar.w - KNOB_R + 4, w))
            art.blit_center(surf, art.circle(KNOB_R, art.WHITE), (x, bar.centery))
            art.blit_center(surf, art.circle(KNOB_R - 5, art.inkier(ACCENT, 0.2)), (x, bar.centery))
            t0 = art.text(mmss(self.progress()), BODY_PX, kit.TEXT_SOFT)
            t1 = art.text(mmss(self.duration_ms), BODY_PX, kit.TEXT_SOFT)
            surf.blit(t0, (bar.x, bar.bottom + TIMES_GAP))
            surf.blit(t1, t1.get_rect(topright=(bar.right, bar.bottom + TIMES_GAP)))
        center = (bar.centerx, bar.bottom + TIMES_GAP + BUTTON_R)
        grow = 1.0 + 0.12 * math.sin(math.pi * min(1.0, self._flash / 0.35)) if self._flash else 1.0
        art.blit_center(surf, art.scaled(_button(BUTTON_R, self.playing), grow), center)

    def _eq(self, surf: pygame.Surface, mid_left: tuple[int, int]) -> None:
        x, cy = mid_left
        for i in range(4):
            h = 14 + 26 * (0.5 + 0.5 * math.sin(self.t * (6.0 + 1.7 * i) + i * 1.3))
            rect = pygame.Rect(x + i * 15, int(cy + 20 - h), 10, int(h))
            pygame.draw.rect(surf, kit.ACCENTS[i], rect, border_radius=4)


def _note(size: int, color: art.Color) -> pygame.Surface:
    """A drawn pair of quavers (no font needed)."""
    key = ("music_note", size, tuple(color))

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            u = size * k / 100
            for cx, cy in ((30, 78), (74, 68)):
                pygame.draw.ellipse(s, color, pygame.Rect(int((cx - 14) * u), int((cy - 10) * u), int(28 * u),
                                                          int(20 * u)))
                pygame.draw.rect(s, color, pygame.Rect(int((cx + 8) * u), int((cy - 58) * u), int(6 * u),
                                                       int(58 * u)))
            pygame.draw.polygon(s, color, [(36 * u, 14 * u), (88 * u, 4 * u), (88 * u, 18 * u), (36 * u, 28 * u)])

        return art.supersampled((size, size), draw)

    return art.cached(key, make)


def _button(r: int, playing: bool) -> pygame.Surface:
    """The round play / pause button (shows what a tap does)."""
    key = ("music_button", r, playing)

    def make() -> pygame.Surface:
        out = pygame.Surface((2 * r, 2 * r + 4), pygame.SRCALPHA)
        out.blit(art.circle(r, art.inkier(ACCENT, 0.4)), (0, 4))
        out.blit(art.circle(r, ACCENT), (0, 0))

        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            if playing:  # pause bars
                w, h = r * 0.24 * k, r * 0.9 * k
                for dx in (-r * 0.22 * k, r * 0.22 * k):
                    pygame.draw.rect(s, art.WHITE, pygame.Rect(int(c + dx - w / 2), int(c - h / 2), int(w), int(h)),
                                     border_radius=int(3 * k))
            else:  # a play triangle
                d = r * 0.45 * k
                pygame.draw.polygon(s, art.WHITE, [(c - d * 0.6, c - d), (c + d, c), (c - d * 0.6, c + d)])

        out.blit(art.supersampled((2 * r, 2 * r), draw), (0, 0))
        return out

    return art.cached(key, make)


# ---------------------------------------------------------------- building it
def _skill(ctx: Any) -> Any:
    if ctx is None:
        return None
    try:
        from littlebot.skills.spotify import get_spotify  # noqa: PLC0415

        sp = get_spotify(ctx)
        return sp if sp is not None and sp.available() else None
    except Exception:  # noqa: BLE001
        return None


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> MusicCard | None:
    skill = _skill(ctx)
    data: dict[str, Any] | None = dict(spec) if kit.clean(spec.get("title")) else None
    if data is None and skill is not None:
        try:
            st = await asyncio.wait_for(skill.now_playing(fresh=True), BUILD_WAIT_S)
        except Exception:  # noqa: BLE001 - SpotifyError, a timeout: no card
            st = None
        if isinstance(st, dict) and st.get("on_littlebot") and st.get("title"):
            data = {**spec, **st}
    if data is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return MusicCard(data, host, source=getattr(skill, "snapshot", None), command=getattr(skill, "command", None),
                     refresh=(lambda: skill.now_playing(fresh=True)) if skill is not None else None, loop=loop)


register(CardType(
    kind="music",
    description="What music is playing on LittleBot from Spotify: the album cover, the song, the artist and how far "
                "in; a tap pauses or carries on. For \"what's playing?\", \"what song is this?\", \"show the music\".",
    fields={},
    build=build,
    actions={"pause": "pause the music", "resume": "carry on", "next": "the next song", "previous": "the song "
             "before"},
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "music", "title": "Bohemian Rhapsody - Remastered 2011", "artist": "Queen",
     "album": "A Night At The Opera (2011 Remaster)", "duration_ms": 354_000, "progress_ms": 83_000, "playing": True},
    {"card": "music", "title": "Clair de lune", "artist": "Claude Debussy, Isao Tomita", "album": "Snowflakes Are "
     "Dancing", "duration_ms": 330_000, "progress_ms": 200_000, "playing": False},
]

__all__ = ["SAMPLES", "MusicCard", "mmss"]
