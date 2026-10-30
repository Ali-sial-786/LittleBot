"""The music card: what Spotify plays on LittleBot now (littlebot/skills/spotify.py).

The album cover (big, on the left), the song, the artist and the album, and a progress bar that follows the clock
(the times under it), with a play / pause button; while it plays, small equaliser bars dance beside the title, and
paused, the cover dims and says so. A tap anywhere (except on LittleBot's eyes, which close the card as on every
card) pauses or carries on; the voice can say "pause", "next" ... (``command``). LittleBot's own eyes stay in the
presenter's box at the top left: music never covers the face.

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
from littlebot.screens.base import BODY_PX, SMALL_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import candy

log = logging.getLogger("littlebot.screens.music")

SYNC_S = 1.0  # read the skill's state this often (no network)
REFRESH_S = 20.0  # ask Spotify this often
ENDED_REFRESH_S = 3.0  # ... and this long after a song should have ended
BUILD_WAIT_S = 2.5
TITLE_SIZES = (44, 40, 36, 32)
ART = 296  # the cover's box (px)
BAR_H = 14
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
    def art_rect(self) -> pygame.Rect:
        return pygame.Rect(candy.PANEL.x + 24, candy.PANEL.y + (candy.PANEL.h - ART) // 2, ART, ART)

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=57)
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        if self.playing:
            self._eq(surf, (candy.HEADER_X + kit.text_w(self.title, 44) + 34, int(candy.HEADER_Y + dy * 0.5)))
        label = candy.chip("Spotify", SMALL_PX, art.WHITE, art.inkier(ACCENT, 0.35))
        surf.blit(label, label.get_rect(midright=(candy.PANEL.right - 4, int(candy.HEADER_Y + dy * 0.5))))
        box = candy.panel(surf, dy=dy)
        cover = self.art_rect().move(0, int(dy))
        self._cover(surf, cover)
        right = pygame.Rect(cover.right + 28, cover.y + 2, box.right - 26 - (cover.right + 28), cover.h)
        self._words(surf, right)
        self._bar(surf, right)

    def _cover(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        if self.slot is not None and self.slot.wanted:
            self.slot.draw(surf, r, self.t, credit=False)
        if self.slot is None or not self.slot.shown:
            if self.slot is None or not self.slot.loading:
                surf.blit(art.rounded_rect(r.w, r.h, 26, art.mix(ACCENT, art.WHITE, 0.55), shadow=4,
                                           border=art.inkier(ACCENT, 0.2), border_w=3), r.topleft)
                art.blit_center(surf, _note(110, art.inkier(ACCENT, 0.35)), r.center)
        if not self.playing:
            surf.blit(art.rounded_rect(r.w, r.h, 26, (60, 30, 55, 120)), r.topleft)
            chip = candy.chip("Paused", BODY_PX, art.WHITE, art.inkier(art.GRAPE, 0.25))
            surf.blit(chip, chip.get_rect(center=r.center))

    def _words(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        y = r.y
        size, lines = kit.fit(self.song or "Music", TITLE_SIZES, r.w, max_lines=2)
        lines = list(lines)[:2]
        if len(kit.wrap(self.song or "Music", size, r.w)) > 2:
            lines[-1] = kit.ellipsize(lines[-1] + " ...", size, r.w)
        block = kit.text_block(lines, size, kit.TEXT)
        surf.blit(block, (r.x, y))
        y += block.get_height() + 10
        if self.artist:
            surf.blit(art.text(kit.ellipsize(self.artist, BODY_PX, r.w), BODY_PX, kit.TEXT_SOFT), (r.x, y))
            y += kit.line_h(BODY_PX)
        if self.album and self.album != self.song:
            surf.blit(art.text(kit.ellipsize(self.album, SMALL_PX, r.w), SMALL_PX, kit.TEXT_SOFT), (r.x, y))

    def _bar(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        btn_r = 30
        bar = pygame.Rect(r.x, r.bottom - 70, r.w - 2 * btn_r - 18, BAR_H)
        surf.blit(art.rounded_rect(bar.w, bar.h, bar.h // 2, art.mix(art.INK_SOFT, art.WHITE, 0.62)), bar.topleft)
        k = art.clamp(self.fraction(), 0.0, 1.0)
        if self.duration_ms and k > 0:
            w = max(bar.h, int(bar.w * k))
            surf.blit(art.rounded_rect(w, bar.h, bar.h // 2, ACCENT), bar.topleft)
            art.blit_center(surf, art.circle(11, art.WHITE), (bar.x + w - bar.h // 2, bar.centery))
            art.blit_center(surf, art.circle(7, art.inkier(ACCENT, 0.2)), (bar.x + w - bar.h // 2, bar.centery))
        if self.duration_ms:
            t0 = art.text(mmss(self.progress()), SMALL_PX, kit.TEXT_SOFT)
            t1 = art.text(mmss(self.duration_ms), SMALL_PX, kit.TEXT_SOFT)
            surf.blit(t0, (bar.x, bar.bottom + 10))
            surf.blit(t1, t1.get_rect(topright=(bar.right, bar.bottom + 10)))
        center = (r.right - btn_r, bar.centery)
        grow = 1.0 + 0.12 * math.sin(math.pi * min(1.0, self._flash / 0.35)) if self._flash else 1.0
        art.blit_center(surf, art.scaled(_button(btn_r, self.playing), grow), center)

    def _eq(self, surf: pygame.Surface, mid_left: tuple[int, int]) -> None:
        x, cy = mid_left
        for i in range(3):
            h = 10 + 16 * (0.5 + 0.5 * math.sin(self.t * (6.0 + 1.7 * i) + i * 1.3))
            rect = pygame.Rect(x + i * 11, int(cy + 13 - h), 7, int(h))
            pygame.draw.rect(surf, kit.ACCENTS[i], rect, border_radius=3)


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
