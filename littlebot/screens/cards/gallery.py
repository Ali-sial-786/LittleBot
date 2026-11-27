"""The gallery card (photos and videos LittleBot took, littlebot/capture) and the share card (a QR code for a phone).

GalleryCard
    One photo or video at a time, as big as the screen allows, with round back / next arrows at the sides and a bar
    along the bottom ("2 of 9", when it was taken, a video's length); a tap in the middle hides them (and shows
    them again). Swipe, tap the left / right side, or say "next" / "previous" to move; tap a video (or say "play
    it") to play it full screen with its sound; "slideshow" moves on by itself every few seconds. It also does "say
    cheese": a viewfinder of what the camera sees (mirrored, like a selfie camera) with a big 3, 2, 1 over it, a
    white flash, then the new photo.
ShareCard
    The share link as a big QR code on the right (littlebot/capture/qr.py): any phone's camera opens it, on the home
    Wi-Fi. On the left, under LittleBot's eyes, the photo or video being shared and what to do.

Pictures are decoded and scaled off the display thread (one shared worker); a frame is a few blits. Both cards are
made by LittleBot's own code (``ctx.capture``), never offered to the screen director (``offered=False``); the voice
steers them with change_card (next, prev, first, last, play, slideshow, share).
"""

from __future__ import annotations

import collections
import concurrent.futures
import io
import logging
import threading
from pathlib import Path
from typing import Any, Callable

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    BODY_PX,
    PRESENTER_BOX,
    SMALL_PX,
    TITLE_PX,
    Card,
    CardHost,
    CardType,
    H,
    W,
    register,
)
from littlebot.screens.cards import _kit as kit

log = logging.getLogger("littlebot.screens")

BG = (20, 18, 26)
BAR_H = 72  # the info bar along the bottom ("2 of 9", when, a video's length) at BODY_PX
BAR_PAD = 24
ARROW_R = 46  # the back / next arrows at the sides (92 px across)
ARROW_ALPHA = 0.88
PLAY_R = 70  # the play button on a video
COUNT_PX = 300  # "say cheese": the 3, 2, 1
CHEESE_PX = 150
SMILE_PX = 64
SLIDESHOW_S = 5.0
FLASH_S = 0.45
SWIPE_PX = 70
EDGE = 0.26  # a tap in the outer quarter goes back / on
# Once the voice chat is over (or since the last touch): the face comes back after this long, and it dozes and
# sleeps as usual. A slideshow first goes once through everything (at most SLIDESHOW_MAX_S).
BROWSE_LINGER_S = 30.0
SHARE_LINGER_S = 30.0
SLIDESHOW_MAX_S = 300.0
CACHE = 4  # fitted pictures kept
# LittleBot's eyes a little smaller over a photo (top-left, like every card): 4/5 of the usual box.
PRESENTER = (PRESENTER_BOX[0] - 4, PRESENTER_BOX[1] - 2, round(PRESENTER_BOX[2] * 0.8), round(PRESENTER_BOX[3] * 0.8))
# The share card: the QR code as big as the screen's height allows, on the right; the photo and the words on the left.
QR_MARGIN = 20
SHARE_THUMB_H = 236
SHARE_WORDS_PX = 44

_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="littlebot-gallery")
        return _POOL


def _fit(img: pygame.Surface, box: tuple[int, int]) -> pygame.Surface:
    w, h = img.get_size()
    k = min(box[0] / max(1, w), box[1] / max(1, h))
    size = (max(1, int(w * k)), max(1, int(h * k)))
    if size == (w, h):
        return img
    try:
        return pygame.transform.smoothscale(img, size)
    except (ValueError, pygame.error):
        return pygame.transform.scale(img, size)


def load_fitted(path: str | Path, box: tuple[int, int] = (W, H), *, mirror: bool = False) -> pygame.Surface:
    """A picture file decoded and scaled to fit ``box`` (any thread)."""
    img = pygame.image.load(str(path))
    if mirror:
        img = pygame.transform.flip(img, True, False)
    return _fit(img, box)


def viewfinder_frame(jpeg: bytes, box: tuple[int, int] = (W, H)) -> pygame.Surface | None:
    """A camera JPEG as the "say cheese" viewfinder: mirrored like a selfie camera, scaled to fit (any thread)."""
    try:
        img = pygame.image.load(io.BytesIO(jpeg), "frame.jpg")
    except (pygame.error, ValueError):
        return None
    return _fit(pygame.transform.flip(img, True, False), box)


class _Pictures:
    """Fitted pictures by key, loaded one at a time off the display thread."""

    def __init__(self) -> None:
        self._done: collections.OrderedDict[str, pygame.Surface | None] = collections.OrderedDict()
        self._pending: dict[str, concurrent.futures.Future[Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, path: Path | None, box: tuple[int, int] = (W, H)) -> tuple[bool, pygame.Surface | None]:
        """(ready, surface): ready with None = it couldn't be loaded."""
        with self._lock:
            if key in self._done:
                self._done.move_to_end(key)
                return True, self._done[key]
            fut = self._pending.get(key)
            if fut is None:
                if path is None:
                    return True, None
                fut = self._pending[key] = _pool().submit(self._load, path, box)
            if not fut.done():
                return False, None
            del self._pending[key]
            try:
                surf = fut.result()
            except Exception:  # noqa: BLE001 - a broken file shows as "can't show this one"
                log.info("gallery: %s couldn't be shown", Path(path or "").name)
                surf = None
            self._done[key] = surf
            while len(self._done) > CACHE:
                self._done.popitem(last=False)
            return True, surf

    @staticmethod
    def _load(path: Path, box: tuple[int, int]) -> pygame.Surface:
        return load_fitted(path, box)


def _when(ts: float) -> str:
    from littlebot.capture.service import _when as when  # noqa: PLC0415

    s = when(ts)
    return s[:1].upper() + s[1:]


def _duration(seconds: float | None) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


class GalleryCard(Card):
    kind = "gallery"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, gallery: Any = None,
                 items: list[Any] | None = None, index: int = 0, countdown: int | None = None,
                 linger: float | None = None, slideshow: bool = False,
                 on_play: Callable[[str], None] | None = None, on_share: Callable[[str], None] | None = None) -> None:
        super().__init__(spec, host)
        self.gallery = gallery
        self.items = list(items or [])
        self.index = max(0, min(int(index), len(self.items) - 1)) if self.items else 0
        self.presenter_box = PRESENTER
        self.mood = "happy"
        self.count: int | None = countdown if countdown is not None else None  # "say cheese" mode while set
        self.cheese = countdown is not None
        self.preview: pygame.Surface | None = None  # the viewfinder (say cheese)
        self.flash_at: float | None = None
        self.error: str | None = None
        self.slideshow = bool(slideshow)
        self._linger = float(linger) if linger is not None else None
        self._on_play = on_play
        self._on_share = on_share
        self._pics = _Pictures()
        self._changed_at = 0.0
        self._count_at = 0.0
        self._dx = 0.0
        self._bar = True

    # ------------------------------------------------------------ say cheese (the capture service drives it)
    def set_count(self, n: int) -> None:
        self.count = int(n)
        self._count_at = self.t

    def set_preview(self, surf: pygame.Surface | None) -> None:
        self.preview = surf

    def taken(self, items: list[Any], item_id: str) -> None:
        """The photo was taken: the flash, then that photo."""
        self.items = list(items)
        self.index = next((i for i, it in enumerate(self.items) if it.id == item_id), 0)
        self.count = None
        self.preview = None
        self.flash_at = self.t
        self._changed_at = self.t

    def failed(self, words: str) -> None:
        self.count = None
        self.preview = None
        self.error = str(words)[:120]
        self.mood = "surprised"

    @property
    def linger_seconds(self) -> float:
        """How long it stays once the chat is over (littlebot/screens/service.py): the new photo ``linger``
        seconds, browsing BROWSE_LINGER_S, a slideshow until it has shown each one once."""
        if self.slideshow and self.count is None and len(self.items) > 1:
            return max(BROWSE_LINGER_S, min(SLIDESHOW_MAX_S, SLIDESHOW_S * len(self.items)))
        return BROWSE_LINGER_S if self._linger is None else self._linger

    @property
    def animating(self) -> bool:
        return self.count is not None or (self.flash_at is not None and self.t - self.flash_at < FLASH_S) or \
            abs(self._dx) > 0.5

    # ------------------------------------------------------------ the item on show
    @property
    def item(self) -> Any:
        return self.items[self.index] if self.items else None

    def _path(self, item: Any) -> Path | None:
        if self.gallery is None or item is None:
            return None
        if item.kind == "video":
            thumb = self.gallery.thumb(item.id)
            return thumb if thumb.is_file() else None
        return self.gallery.file(item)

    def move(self, step: int) -> bool:
        if len(self.items) < 2:
            return False
        self.index = (self.index + step) % len(self.items)
        self._changed_at = self.t
        self.flash_at = None
        return True

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.slideshow and self.count is None and self.t - self._changed_at >= SLIDESHOW_S:
            self.move(1)
        if self.count is None and not self.error:
            # warm the next one while this one shows
            nxt = self.items[(self.index + 1) % len(self.items)] if len(self.items) > 1 else None
            if nxt is not None:
                self._pics.get(nxt.id, self._path(nxt))

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(BG)
        if self.count is not None:
            self._draw_cheese(surf)
        elif self.error:
            self._centre_text(surf, [(self.error, SHARE_WORDS_PX, art.WHITE)])
        elif not self.items:
            self._centre_text(surf, [("No photos yet.", TITLE_PX, art.WHITE),
                                     ("Say: \u201cHey LittleBot, take a picture!\u201d", BODY_PX, (225, 222, 235))])
        else:
            self._draw_item(surf)
        if self.flash_at is not None:
            k = 1.0 - (self.t - self.flash_at) / FLASH_S
            if k > 0:
                surf.blit(art.veil((W, H), (255, 255, 255), int(255 * k)), (0, 0))

    def _draw_cheese(self, surf: pygame.Surface) -> None:
        if self.preview is not None:
            surf.blit(self.preview, self.preview.get_rect(center=(W // 2, H // 2)))
        else:
            surf.blit(art.background((W, H), seed=31), (0, 0))
        if self.count and self.count > 0:
            k = art.ease_out_back(kit.appear(self.t, self._count_at, 0.35))
            img = art.text_outlined(str(self.count), COUNT_PX, art.WHITE, art.INK, 8)
            art.blit_center(surf, art.scaled(img, max(0.05, k)), (W // 2, H // 2 - 10))
            hint = art.text_outlined("Smile!", SMILE_PX, art.WHITE, art.INK, 4)
            art.blit_center(surf, hint, (W // 2, H - 56))
        else:
            art.blit_center(surf, art.text_outlined("Cheese!", CHEESE_PX, art.WHITE, art.INK, 6), (W // 2, H // 2))

    def arrow_centers(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Where the back and next arrows sit (their taps are the outer quarters, EDGE)."""
        y = (H - BAR_H) // 2 + 8
        return (BAR_PAD + ARROW_R, y), (W - BAR_PAD - ARROW_R, y)

    def _draw_item(self, surf: pygame.Surface) -> None:
        item = self.item
        ready, pic = self._pics.get(item.id, self._path(item))
        dx = int(self._dx)
        if pic is not None:
            surf.blit(pic, pic.get_rect(center=(W // 2 + dx, H // 2)))
        elif ready:
            self._centre_text(surf, [("A video" if item.kind == "video" else "This photo can't be shown", BODY_PX,
                                      art.WHITE)])
        else:
            kit.placeholder(surf, pygame.Rect(0, 0, W, H).inflate(-200, -160), self.t)
        if item.kind == "video":
            cx, cy = W // 2 + dx, H // 2
            art.blit_center(surf, art.circle(PLAY_R + 4, art.INK, 90), (cx, cy + 3))
            art.blit_center(surf, art.circle(PLAY_R, (255, 255, 255), 225), (cx, cy))
            d = PLAY_R * 0.46
            pygame.draw.polygon(surf, art.INK, [(cx - d * 0.62 + 6, cy - d), (cx - d * 0.62 + 6, cy + d),
                                                (cx + d + 6, cy)])
        if self.slideshow:
            tag = art.pill("Slideshow", SMALL_PX, art.WHITE, (52, 46, 66), pad=(16, 6))
            surf.blit(tag, tag.get_rect(topright=(W - 16, 14)))
        if not self._bar:
            return
        if len(self.items) > 1:
            for direction, center in zip((-1, 1), self.arrow_centers()):
                img = kit.arrow(direction, art.WHITE, ARROW_R)
                kit.blit_alpha(surf, img, img.get_rect(center=center).topleft, ARROW_ALPHA)
        surf.blit(art.veil((W, BAR_H), (0, 0, 0), 160), (0, H - BAR_H))
        mid_y = H - BAR_H // 2
        left = art.text(f"{self.index + 1} of {len(self.items)}", BODY_PX, art.WHITE)
        surf.blit(left, left.get_rect(midleft=(BAR_PAD, mid_y)))
        right = art.text(f"Video {_duration(item.seconds)}", BODY_PX, art.WHITE) if item.kind == "video" else None
        if right is not None:
            surf.blit(right, right.get_rect(midright=(W - BAR_PAD, mid_y)))
        side = max(left.get_width(), right.get_width() if right is not None else 0) + BAR_PAD + 24
        when = kit.ellipsize(_when(item.taken_at), BODY_PX, W - 2 * side)
        mid = art.text(when, BODY_PX, art.WHITE)
        surf.blit(mid, mid.get_rect(center=(W // 2, mid_y)))

    def _centre_text(self, surf: pygame.Surface, parts: list[tuple[str, int, tuple[int, int, int]]]) -> None:
        """Words in the middle of the screen: each part wrapped (at most three lines), one under the other."""
        imgs = []
        for text, size, color in parts:
            lines = list(kit.wrap(text, size, W - 200))
            if len(lines) > 3:
                lines = lines[:2] + [kit.ellipsize(" ".join(lines[2:]), size, W - 200)]
            imgs.append(kit.text_block(lines, size, color, align="center"))
        gap = 22
        y = H // 2 - (sum(i.get_height() for i in imgs) + gap * (len(imgs) - 1)) // 2
        for img in imgs:
            surf.blit(img, img.get_rect(midtop=(W // 2, y)))
            y += img.get_height() + gap

    # ------------------------------------------------------------ touch, keys, voice
    def tap(self, x: float, y: float) -> bool:
        if self.count is not None:
            return True
        if self.error or not self.items:
            self.done = True
            return True
        if x < W * EDGE:
            self.move(-1)
        elif x > W * (1 - EDGE):
            self.move(1)
        elif self.item.kind == "video":
            self.play()
        else:
            self._bar = not self._bar
        self.slideshow = False
        return True

    def drag(self, dx: float, dy: float) -> None:
        if self.count is None:
            self._dx += dx

    def drag_end(self) -> None:
        if self._dx <= -SWIPE_PX:
            self.move(1)
        elif self._dx >= SWIPE_PX:
            self.move(-1)
        self._dx = 0.0
        self.slideshow = False

    def key(self, name: str) -> bool:
        if name in ("right", "page down", "n"):
            return self.move(1) or True
        if name in ("left", "page up", "p"):
            return self.move(-1) or True
        if name in ("return", "space") and self.item is not None and self.item.kind == "video":
            return self.play()
        return False

    def play(self) -> bool:
        item = self.item
        if item is None or item.kind != "video" or self._on_play is None:
            return False
        self._on_play(item.id)
        return True

    def command(self, action: str, args: dict[str, Any]) -> bool:
        a = action.strip().lower()
        if a in ("next", "forward", "on"):
            return self.move(1)
        if a in ("prev", "previous", "back", "before"):
            return self.move(-1)
        if a in ("first", "oldest") and self.items:
            self.index, self._changed_at = len(self.items) - 1, self.t
            return True
        if a in ("last", "latest", "newest") and self.items:
            self.index, self._changed_at = 0, self.t
            return True
        if a == "number" and self.items:
            try:
                n = int(args.get("n") or args.get("number") or 0)
            except (TypeError, ValueError):
                return False
            if 1 <= n <= len(self.items):
                self.index, self._changed_at = n - 1, self.t
                return True
            return False
        if a == "play":
            return self.play()
        if a == "slideshow":
            self.slideshow = bool(args.get("on", True))
            self._changed_at = self.t
            return True
        if a == "share" and self.item is not None and self._on_share is not None:
            self._on_share(self.item.id)
            return True
        return False

    def describe(self) -> str:
        if self.count is not None:
            return "the camera, counting down to take a photo"
        item = self.item
        if item is None:
            return "the photo gallery (empty)"
        what = "photo" if item.kind == "photo" else f"video ({_duration(item.seconds)})"
        return (f"the photo gallery: {what} {self.index + 1} of {len(self.items)}, taken "
                f"{_when(item.taken_at).lower()}" + (" (slideshow)" if self.slideshow else ""))

    def status(self) -> dict[str, Any]:
        out = super().status()
        item = self.item
        out.update({"id": item.id if item is not None else None, "index": self.index, "count": len(self.items),
                    "countdown": self.count, "slideshow": self.slideshow})
        return out


class ShareCard(Card):
    kind = "share"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, url: str = "", gallery: Any = None,
                 item: Any = None, hours: float = 24.0, reachable: bool = True) -> None:
        super().__init__(spec, host)
        self.url = str(url)
        self.gallery = gallery
        self.item = item
        self.hours = float(hours)
        self.reachable = bool(reachable)
        self.mood = "happy"
        self.linger_seconds = SHARE_LINGER_S
        self._qr: pygame.Surface | None = None
        self._qr_side = 0
        self._pics = _Pictures()

    def _code(self, side: int) -> pygame.Surface:
        """The QR code at most ``side`` px square, made once (whole modules, a 4-module quiet zone all round)."""
        if self._qr is None or self._qr_side != side:
            from littlebot.capture.qr import matrix  # noqa: PLC0415

            m = matrix(self.url)
            n = len(m) + 8  # a 4-module quiet zone all round
            px = max(1, side // n)
            img = pygame.Surface((n * px, n * px))
            img.fill((255, 255, 255))
            for y, row in enumerate(m):
                for x, dark in enumerate(row):
                    if dark:
                        img.fill((0, 0, 0), ((x + 4) * px, (y + 4) * px, px, px))
            self._qr, self._qr_side = img, side
        return self._qr

    def code_rect(self) -> pygame.Rect:
        """Where the QR code goes: on the right, as tall as the screen allows (clear of LittleBot's eyes)."""
        code = self._code(H - 2 * QR_MARGIN)
        return code.get_rect(midright=(W - QR_MARGIN - 6, H // 2))

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=29)
        code = self._code(H - 2 * QR_MARGIN)
        box = self.code_rect()
        surf.blit(art.rounded_rect(box.w + 12, box.h + 12, 18, (255, 255, 255), shadow=5), box.move(-6, -6))
        surf.blit(code, box)
        px, py, pw, ph = self.presenter_box
        x0 = px + 4
        width = box.left - 6 - 28 - x0
        if self.title:
            title_x = px + pw + 18
            title = kit.text_block([kit.ellipsize(self.title, TITLE_PX, box.left - 30 - title_x)], TITLE_PX, kit.TEXT)
            surf.blit(title, title.get_rect(midleft=(title_x, py + ph // 2)))
        top = py + ph + 20
        thumb_h = 0
        if self.item is not None and self.gallery is not None:
            path = self.gallery.thumb(self.item.id)
            if not path.is_file() and self.item.kind == "photo":
                path = self.gallery.file(self.item)
            ready, pic = self._pics.get(f"share-{self.item.id}", path if path.is_file() else None,
                                        (width - 12, SHARE_THUMB_H - 12))
            if pic is not None:
                frame = art.rounded_rect(pic.get_width() + 12, pic.get_height() + 12, 16, art.WHITE, shadow=4)
                r = frame.get_rect(midtop=(x0 + width // 2, top))
                surf.blit(frame, r)
                surf.blit(pic, (r.x + 6, r.y + 6))
                thumb_h = pic.get_height() + 12 + 22
        lines = (["Scan to save it", "on your phone"] if self.reachable
                 else ["Phones can't reach", "LittleBot yet"])
        img = kit.text_block([kit.ellipsize(s, SHARE_WORDS_PX, width) for s in lines], SHARE_WORDS_PX, kit.TEXT,
                             align="center")
        small = (["Same Wi-Fi as LittleBot", f"Works for {self.hours:g} hours"] if self.reachable
                 else ["Turn on LAN access", "(see the dashboard)"])
        img2 = kit.text_block([kit.ellipsize(s, BODY_PX, width) for s in small], BODY_PX, kit.TEXT_SOFT,
                              align="center")
        words_h = img.get_height() + 16 + img2.get_height()
        # with the picture: right under it; without: in the middle of the room under the eyes
        y = top + thumb_h if thumb_h else top + max(0, (H - 24 - top - words_h) // 2)
        surf.blit(img, img.get_rect(midtop=(x0 + width // 2, y)))
        surf.blit(img2, img2.get_rect(midtop=(x0 + width // 2, y + img.get_height() + 16)))

    def tap(self, x: float, y: float) -> bool:
        self.done = True
        return True

    def describe(self) -> str:
        return "a QR code to scan with a phone, for the photo or video" if self.reachable else \
            "a QR code, but phones can't reach LittleBot until LAN access is on"


async def _build_gallery(spec: dict[str, Any], ctx: Any, host: CardHost) -> Card | None:
    capture = getattr(ctx, "capture", None)
    if capture is None:
        return None
    items = capture.gallery.items(limit=500)
    return GalleryCard(spec, host, gallery=capture.gallery, items=items,
                       on_play=getattr(capture, "_play_cb", None), on_share=getattr(capture, "_share_cb", None))


async def _no_build(spec: dict[str, Any], ctx: Any, host: CardHost) -> Card | None:
    return None


register(CardType(
    kind="gallery",
    description="The photos and videos LittleBot took (shown by LittleBot's own camera tools).",
    fields={},
    build=_build_gallery,
    actions={"next": "the next (older) one", "prev": "the one before (newer)", "first": "the oldest",
             "last": "the newest", "number": "{\"n\": 3}: the 3rd", "play": "play the video on the screen",
             "slideshow": "{\"on\": true|false}: move on by itself", "share": "a QR code for a phone"},
    offered=False,
))
register(CardType(
    kind="share",
    description="A QR code for a phone to open a photo or video (shown by LittleBot's own camera tools).",
    fields={},
    build=_no_build,
    offered=False,
))


__all__ = ["GalleryCard", "ShareCard", "load_fitted"]
