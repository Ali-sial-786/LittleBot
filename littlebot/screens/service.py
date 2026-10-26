"""ScreenService: one info card at a time over the face, presented by LittleBot's own eyes.

Public API (``ctx.screens``; used by the voice's director and tools, and by the dashboard)::

    await screens.show(spec, source="voice") -> {"ok", "showing", "error"}
        builds ``spec`` (littlebot.screens.base.build: spec["card"] is the kind) and shows it,
        replacing any card on the screen. ``error``: "disabled" (the parent's switch or config),
        "busy" (a game, a video or an activity has the screen, or LittleBot is asleep) or
        "no_card" (nothing could be built); None when it worked. ``showing``: describe().
    screens.show_card(card, source="voice") -> the same, for a card that is already built
    screens.make_host()        -> a CardHost bound to this service (to build a card yourself)
    screens.command(action, args) -> bool   (steers the card: "next", "day", ...; False: no card / not understood)
    screens.close(reason)      -> bool      (True if a card was taken off the screen)
    screens.current            -> Card | None
    screens.active             -> bool
    screens.describe()         -> str | None (one line: what the screen shows)
    screens.status()           -> {available, enabled, web_pictures, active, card, source, seconds, sticky,
                                   closes_in, linger_seconds, last, last_error}

Display hooks (``littlebot.face.display`` calls these on the main thread; a game wins over a card)::

    screens.render(surface, rect, dt) -> bool   (True: the card was drawn instead of the face)
    screens.handle_tap(nx, ny)  -> bool   (normalised 0..1 inside the face rect; the presenter's eyes close it)
    screens.handle_press(nx, ny) / handle_drag(nx, ny) / handle_release(nx, ny) -> bool
                                      (the finger's path: a card with ``drag`` scrolls, a still press-and-
                                      release is its tap; other cards get the tap on press, as before)
    screens.handle_key(name)    -> bool   ("escape" closes the card; other keys go to the card)
    screens.animating           -> bool   (the display runs at full frame rate meanwhile)

Bus topics: ``screen.card_shown`` {kind, title, source}, ``screen.card_closed`` {kind, reason},
``screen.news`` {kind, title, text} and ``screen.keep_awake`` {kind}.

How a card reaches the voice
----------------------------
``CardHost.tell(text)`` publishes ``screen.news`` {kind, title, text}: the live voice (littlebot/chatgpt)
picks it up and passes it into the call as a quiet note (it is not read out as it stands; a game's
``game.news`` by contrast is said straight away). ``CardHost.say(text)`` speaks through ``ctx.audio`` when no
voice chat is on, and becomes a ``tell`` while one is, so a card never talks over the voice.

How long a card stays
---------------------
While a voice chat is on (the live voice or a conversation holds the face / speaker / microphone) the
card stays. After the chat it lingers ``screens.linger_seconds`` (config, 30 s; a card may set its own
``linger_seconds``, the games menu 60 s) and goes, unless it is
``sticky`` (a running timer): that one stays until it sets ``done`` (and meanwhile publishes
``screen.keep_awake`` every minute so LittleBot doesn't fall asleep on it). A card that sets ``done``,
or calls ``host.close()``, is closed. A card also goes on STOP ALL, when LittleBot sleeps, when a game,
a video or an activity starts, when the parent switches cards off, on a tap on the presenter's eyes
and on Esc. A card shown with no chat on (the dashboard, a long press for the games menu) lingers from
the moment it appears, and every touch on it starts the wait again.

Leases: a card takes only ``Resource.SCREEN``, at ``Priority.CONVERSATION``. The face and speaker stay
with whoever has them (the live voice keeps talking under the card), and a game (``ACTIVITY``) or a
video (``MEDIA``) simply preempts the card. A card never stops the voice or a game.

Threading: one ``RLock`` guards the session. ``render`` / ``handle_*`` run on the display thread, ``show``
/ ``command`` on the asyncio runtime, ``close`` / preemption / bus callbacks on any thread. Cards never
block: sounds and speech are submitted to the runtime fire-and-forget, pictures are fetched in the
background (littlebot/screens/images.py). Without a window, a watchdog keeps the card's clock running.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.screens.base import PICTURE_ACTION, PICTURE_ZOOMS, Card, H, W

PICTURE_REPEAT_S = 2.5  # "zoom in" heard by the hand-off and the screen director: zoomed once

log = logging.getLogger("littlebot.screens")

OWNER = "screens"
CARD_SHOWN = "screen.card_shown"  # {kind, title, source}
CARD_CLOSED = "screen.card_closed"  # {kind, reason}
SCREEN_NEWS = "screen.news"  # {kind, title, text}: a quiet note for the live voice (CardHost.tell)
KEEP_AWAKE = "screen.keep_awake"  # {kind}: a sticky card (a running timer) is still up

VOICE_OWNERS = frozenset({"conversation", "live_voice"})
VOICE_RESOURCES = (Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE)
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
_STOP_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value}
_BUSY_STATES = {RobotState.SLEEPING, RobotState.ERROR, RobotState.PLAYING_MEDIA}

WATCH_PERIOD = 0.25  # s, lifetime watchdog (linger, done, headless driving)
HEADLESS_AFTER = 0.6  # s without a drawn frame -> the watchdog advances the card itself
KEEP_AWAKE_EVERY = 60.0
MAX_DT = 0.1
MAX_FRAME_ERRORS = 30
TAP_MARGIN = 10  # px around the presenter's box that still count as a tap on the eyes
DRAG_SLOP = 14  # canvas px a finger moves before a press becomes a drag (not a tap)
VOICE_FRESH = 0.35  # s a lip-sync level stays valid (the voice publishes ~30 a second while talking)
MOUTH_RATE = 28.0
INTRO_SECONDS = 1.0  # full frame rate while the card and presenter pop in
SOUND_GAP = 0.12  # s between card sounds (a tap storm never builds an audio backlog)
DEFAULT_EYES = (90, 210, 255)


class _NoPicture:
    """A picture that already failed: the card shows its no-picture layout."""

    ready = True
    surface = None
    credit = ""


@dataclass(eq=False)
class _Session:
    card: Card
    host: _Host
    source: str
    shown: float
    lease: Lease | None = None
    ended: bool = False
    t: float = 0.0  # seconds drawn / driven (the presenter's clock)
    quiet_since: float | None = None  # when the voice chat was last seen ending
    last_render: float = -1e9
    last_tick: float = 0.0
    last_check: float = -1e9
    awake_t: float = 0.0
    close_req: str | None = None
    errors: int = 0
    taps: int = 0
    mouth: float = 0.0
    shape: str = ""
    press: tuple[float, float] | None = None  # where the finger went down (cards that drag)
    last_xy: tuple[float, float] = (0.0, 0.0)
    dragging: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    task: Any = None
    zoom: int = -1  # the card's picture full screen: an index into PICTURE_ZOOMS (-1: the card itself)
    zoom_cmd: tuple[str, float] | None = None  # the last picture command and when (the same one twice: once)
    zoomed: tuple[tuple[int, float], Any] | None = None  # (zoom, picture id) -> the scaled picture


class _Host:
    """The real CardHost: sounds / speech through ``ctx.audio``, notes for the voice, pictures."""

    def __init__(self, svc: ScreenService) -> None:
        self.svc = svc
        self.session: _Session | None = None
        self._last_sound = -1e9
        self._lock = threading.Lock()

    @property
    def eye_color(self) -> tuple[int, int, int]:
        try:
            c = self.svc.ctx.config.face.eye_color
            return (int(c[0]), int(c[1]), int(c[2]))
        except Exception:  # noqa: BLE001
            return DEFAULT_EYES

    def _live(self) -> bool:
        s = self.session
        return s is None or (not s.ended and self.svc._session is s)

    def now(self) -> float:
        return self.svc._clock()

    def sound(self, name: str) -> None:
        now = self.svc._clock()
        with self._lock:
            if now - self._last_sound < SOUND_GAP or not self._live():
                return
            self._last_sound = now
        self.svc._submit(lambda a: a.play_sound(str(name), wait=False), f"sound {name}")

    def say(self, text: str) -> None:
        text = str(text or "").strip()[:200]
        if not text or not self._live():
            return
        if self.svc.voice_on():  # never talk over the voice: let it say it
            self.tell(text)
            return
        self.svc._submit(lambda a: a.say(text), "speech")

    def tell(self, text: str) -> None:
        s = self.session
        text = str(text or "").strip()[:300]
        if not text or s is None or not self._live():
            return
        self.svc._publish(SCREEN_NEWS, {"kind": s.card.kind, "title": s.card.title, "text": text})

    def picture(self, query: str = "", url: str = "") -> Any:
        ctx = self.svc.ctx
        if not ctx.settings.get("screens.web_pictures", True):
            return _NoPicture()
        try:
            from littlebot.screens import images  # noqa: PLC0415
        except ImportError:
            return _NoPicture()
        try:
            return images.fetch(ctx, query=str(query or "")[:120], url=str(url or "")[:500])
        except Exception:  # noqa: BLE001 - no picture is never an error worth showing
            log.debug("picture fetch failed", exc_info=True)
            return _NoPicture()

    def close(self) -> None:
        s = self.session
        if s is not None and not s.ended:
            s.close_req = s.close_req or "closed by the card"  # picked up by the next check (never mid-draw)


class ScreenService(Service):
    name = "screens"

    def __init__(self, ctx: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.ctx = ctx
        self._clock = clock
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._last: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._voice: tuple[float, str, float] = (0.0, "", -1e9)  # (level, shape, when)
        self._canvas: Any = None
        self._freeing: threading.Thread | None = None  # giving a closed card's memory back (_free_memory)
        self._scaled: Any = None
        bus = ctx.bus
        self._subs: list[Subscription] = [
            bus.subscribe(Topics.STOP_ALL, lambda _e: self.close("stop all")),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state),
            bus.subscribe(Topics.AUDIO_VISEME, self._on_viseme),
            bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close("activity started")),
            bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.close("video started")),
            bus.subscribe(GAME_STARTED, lambda _e: self.close("game started")),
            bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
        ]

    # ================================================================ lifecycle
    async def start(self) -> None:  # type: ignore[override]
        self.set_status(ServiceStatus.RUNNING if self._config_enabled() else ServiceStatus.DISABLED)

    async def stop(self) -> None:  # type: ignore[override]
        self.detach()
        self.set_status(ServiceStatus.STOPPED)

    def detach(self) -> None:
        """Drop bus subscriptions and close any card (shutdown / tests)."""
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.close("shutdown")

    # ================================================================ settings
    @property
    def linger_seconds(self) -> float:
        cfg = getattr(self.ctx.config, "screens", None)
        return float(getattr(cfg, "linger_seconds", 30.0))

    def _linger(self, s: _Session) -> float:
        """How long ``s`` stays once no chat is on: the card's own ``linger_seconds``, else the config's."""
        own = getattr(s.card, "linger_seconds", None)
        try:
            return float(own) if own is not None else self.linger_seconds
        except (TypeError, ValueError):
            return self.linger_seconds

    def _touched(self, s: _Session) -> None:
        """Someone is using the card: with no chat on, its wait starts again (caller holds the lock)."""
        if s.quiet_since is not None:
            s.quiet_since = self._clock()

    def _config_enabled(self) -> bool:
        return bool(getattr(getattr(self.ctx.config, "screens", None), "enabled", True))

    @property
    def enabled(self) -> bool:
        """The config switch and the parent's switch (``screens.enabled``) are both on."""
        return self._config_enabled() and bool(self.ctx.settings.get("screens.enabled", True))

    def voice_on(self) -> bool:
        """A voice chat is on: the live voice or a conversation holds the face, speaker or microphone."""
        arbiter = self.ctx.arbiter
        for r in VOICE_RESOURCES:
            h = arbiter.holder(r)
            if h is not None and h.owner in VOICE_OWNERS:
                return True
        return False

    # ================================================================ control
    def make_host(self) -> _Host:
        """A CardHost bound to this service (``show`` makes one per card)."""
        return _Host(self)

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        """Build ``spec`` and show it, replacing any current card (see the module docstring)."""
        err = self._blocked()
        if err is not None:
            return self._result(False, err)
        from littlebot.screens import base  # noqa: PLC0415

        host = self.make_host()
        card = await base.build(dict(spec or {}), self.ctx, host)
        if card is None:
            return self._result(False, "no_card")
        return self.show_card(card, source=source)

    def show_card(self, card: Card, *, source: str = "voice") -> dict[str, Any]:
        """Show an already-built card (it gets this service's host), replacing any current one."""
        err = self._blocked()
        if err is not None:
            return self._result(False, err)
        with self._lock:
            same = self._session if self._session is not None and self._session.card is card \
                and not self._session.ended else None
        if same is not None:  # the card on screen took the new spec in (another timer): keep showing it
            self._touched(same)
            self._check(same, force=True)
            return self._result(True, None)
        host = card.host if isinstance(card.host, _Host) and card.host.svc is self else self.make_host()
        card.host = host
        now = self._clock()
        s = _Session(card=card, host=host, source=str(source or "voice")[:40], shown=now, last_tick=now,
                     awake_t=now)
        host.session = s
        if not self.voice_on():
            s.quiet_since = now
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.CONVERSATION,
                                         on_preempt=lambda _l, by, s=s: self._end(s, f"preempted by {by}"))
        if lease is None:
            return self._result(False, "busy")
        s.lease = lease
        with self._lock:
            old, self._session = self._session, s
        if old is not None:
            self._end(old, "replaced")  # its lease was already replaced by ours (same owner)
        self._start_watch(s)
        self.last_error = None
        log.info("card %s shown (%s): %s", card.kind, s.source, card.title[:60])
        self._publish(CARD_SHOWN, {"kind": card.kind, "title": card.title, "source": s.source})
        return self._result(True, None)

    def command(self, action: str, args: dict[str, Any] | None = None) -> bool:
        """Steer the current card from the voice ("next", "day", ...). False: no card, or not understood."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            try:
                if str(action or "") == PICTURE_ACTION:
                    ok = self._picture_command(s, str((args or {}).get("view") or "fill"))
                else:
                    ok = bool(s.card.command(str(action or ""), dict(args or {})))
            except Exception:  # noqa: BLE001
                log.exception("card %s command %r failed", s.card.kind, action)
                return False
        if ok:
            self._check(s, force=True)
        return ok

    def _picture_command(self, s: _Session, view: str) -> bool:
        """"fill" / "in" / "out" / "back" for the card's picture (caller holds the lock). The same command again
        within PICTURE_REPEAT_S counts as done once: the voice's hand-off and the screen director may both hear
        "zoom in"."""
        slot = getattr(s.card, "slot", None)
        if slot is None or not getattr(slot, "shown", False):
            return False
        view = view.strip().lower()
        now = self._clock()
        if s.zoom_cmd is not None and s.zoom_cmd[0] == view and now - s.zoom_cmd[1] < PICTURE_REPEAT_S:
            return True
        s.zoom_cmd = (view, now)
        top = len(PICTURE_ZOOMS) - 1
        if view in ("fill", "full", "full screen", "fullscreen", "big", "bigger"):
            s.zoom = max(0, s.zoom)
        elif view in ("in", "zoom in", "closer", "more"):
            s.zoom = min(top, s.zoom + 1)
        elif view in ("out", "zoom out", "smaller", "less"):
            s.zoom -= 1
        elif view in ("back", "reset", "card", "normal"):
            s.zoom = -1
        else:
            return False
        if s.zoom < 0:
            s.zoomed = None
        log.info("card %s: picture %s", s.card.kind, "back on the card" if s.zoom < 0 else
                 f"full screen x{PICTURE_ZOOMS[s.zoom]:g}")
        return True

    def _draw_picture(self, s: _Session, canvas: Any) -> bool:
        """The card's picture filling the screen at s.zoom (centred, the middle when zoomed in), or False."""
        import pygame  # noqa: PLC0415

        slot = getattr(s.card, "slot", None)
        pic = getattr(slot, "picture", None) if slot is not None and getattr(slot, "shown", False) else None
        src = getattr(pic, "surface", None)
        if s.zoom < 0 or src is None:
            return False
        key = (s.zoom, id(src))
        if s.zoomed is None or s.zoomed[0] != key:
            w, h = src.get_size()
            k = min(W / max(1, w), H / max(1, h)) * PICTURE_ZOOMS[s.zoom]
            size = (max(1, int(w * k)), max(1, int(h * k)))
            try:
                img = pygame.transform.smoothscale(src, size)
            except (ValueError, pygame.error):
                img = pygame.transform.scale(src, size)
            if size[0] > W or size[1] > H:  # zoomed in: keep only the middle (less memory than the whole)
                img = img.subsurface(pygame.Rect((size[0] - min(W, size[0])) // 2, (size[1] - min(H, size[1])) // 2,
                                                 min(W, size[0]), min(H, size[1]))).copy()
            s.zoomed = (key, img)
        img = s.zoomed[1]
        canvas.fill((24, 20, 28))
        canvas.blit(img, img.get_rect(center=(W // 2, H // 2)))
        return True

    def close(self, reason: str = "closed") -> bool:
        """Take the card off the screen (the face comes back). Safe from any thread."""
        with self._lock:
            s = self._session
        if s is None:
            return False
        return self._end(s, str(reason or "closed")[:60])

    @property
    def current(self) -> Card | None:
        s = self._session
        return s.card if s is not None and not s.ended else None

    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    @property
    def animating(self) -> bool:
        """The card wants the full frame rate: popping in, the presenter talking, or ``card.animating``."""
        s = self._session
        if s is None or s.ended:
            return False
        if s.t < INTRO_SECONDS or s.mouth > 0.02 or self._clock() - self._voice[2] < VOICE_FRESH:
            return True
        return bool(getattr(s.card, "animating", False))

    def describe(self) -> str | None:
        s = self._session
        if s is None or s.ended:
            return None
        try:
            return str(s.card.describe())[:200]
        except Exception:  # noqa: BLE001
            return s.card.title or s.card.kind

    def status(self) -> dict[str, Any]:
        settings = self.ctx.settings
        out: dict[str, Any] = {
            "available": True, "enabled": self.enabled,
            "web_pictures": bool(settings.get("screens.web_pictures", True)), "active": False, "card": None,
            "linger_seconds": self.linger_seconds, "last": dict(self._last) if self._last else None,
            "last_error": self.last_error,
        }
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return out
            try:
                card = dict(s.card.status())
            except Exception:  # noqa: BLE001
                card = {"kind": s.card.kind, "title": s.card.title}
            now = self._clock()
            closes_in = None
            if not s.card.sticky and s.quiet_since is not None:
                closes_in = round(max(0.0, self._linger(s) - (now - s.quiet_since)), 1)
            if s.zoom >= 0:
                card["picture_zoom"] = PICTURE_ZOOMS[s.zoom]
            out.update({"active": True, "card": card, "showing": self.describe(), "source": s.source,
                        "seconds": round(max(0.0, now - s.shown), 1), "sticky": bool(s.card.sticky),
                        "closes_in": closes_in, "voice_on": self.voice_on(), "drawn": now - s.last_render < 2.0})
            return out

    def tick(self) -> None:
        """Run the lifetime checks now (the watchdog calls this)."""
        s = self._session
        if s is not None:
            self._check(s, force=True)

    # ================================================================ display hooks
    def render(self, surface: Any, rect: Any = None, dt: float = 0.0) -> bool:
        """Advance and draw the card (and the presenter) into ``rect`` of ``surface``. False: nothing drawn."""
        s = self._session
        if s is None:
            return False
        drawn = False
        end: str | None = None
        with self._lock:
            if s.ended or self._session is not s:
                return False
            now = self._clock()
            dt = max(0.0, min(float(dt), MAX_DT))
            s.last_render = s.last_tick = now
            try:
                self._advance(s, dt, now)
                self._paint(s, surface, rect)
                s.errors = 0
                drawn = True
            except Exception:  # noqa: BLE001 - a broken card must never take the face down
                s.errors += 1
                if s.errors <= 3:
                    log.exception("card %s frame failed", s.card.kind)
                if s.errors >= MAX_FRAME_ERRORS:
                    end = "error"
        if end is not None:
            self._end(s, end)
            return False
        self._check(s)
        return drawn

    def handle_tap(self, nx: float, ny: float) -> bool:
        """A tap at normalised (nx, ny). On the presenter's eyes it closes the card; elsewhere it goes to
        the card. True while a card shows (taps never fall through to the face underneath)."""
        x, y = _canvas(nx, ny)
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            if not _on_eyes(s.card, x, y):
                self._tap(s, x, y)
                return True
        self._end(s, "tap")
        return True

    def handle_press(self, nx: float, ny: float) -> bool:
        """The finger went down. On the presenter's eyes: close. On a card that can ``drag``: wait to see
        whether it is a tap or a drag; on any other card: its tap, now. True while a card shows."""
        x, y = _canvas(nx, ny)
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            if _on_eyes(s.card, x, y):
                pass
            elif callable(getattr(s.card, "drag", None)):
                s.press, s.last_xy, s.dragging = (x, y), (x, y), False
                self._touched(s)
                return True
            else:
                self._tap(s, x, y)
                return True
        self._end(s, "tap")
        return True

    def handle_drag(self, nx: float, ny: float) -> bool:
        """The finger moved (button held). A card with ``drag(dx, dy)`` scrolls once it moved past the slop."""
        x, y = _canvas(nx, ny)
        with self._lock:
            s = self._session
            if s is None or s.ended or s.press is None:
                return False
            if not s.dragging and abs(x - s.press[0]) + abs(y - s.press[1]) < DRAG_SLOP:
                return True
            lx, ly = s.last_xy
            s.dragging, s.last_xy = True, (x, y)
            self._touched(s)
            try:
                s.card.drag(x - lx, y - ly)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.exception("card %s drag failed", s.card.kind)
            return True

    def handle_release(self, nx: float, ny: float) -> bool:
        """The finger came up: the end of a drag, or (it never moved) a tap where it went down."""
        with self._lock:
            s = self._session
            if s is None or s.ended or s.press is None:
                return False
            press, dragging = s.press, s.dragging
            s.press, s.dragging = None, False
            if not dragging:
                self._tap(s, *press)
                return True
            end = getattr(s.card, "drag_end", None)
            try:
                if callable(end):
                    end()
            except Exception:  # noqa: BLE001
                log.exception("card %s drag end failed", s.card.kind)
            return True

    def _tap(self, s: _Session, x: float, y: float) -> None:
        """A tap for the card (caller holds the lock)."""
        s.taps += 1
        self._touched(s)
        if s.zoom >= 0:  # the picture full screen: a tap shows the whole card again
            s.zoom, s.zoomed, s.zoom_cmd = -1, None, None
            return
        try:
            s.card.tap(x, y)
        except Exception:  # noqa: BLE001
            log.exception("card %s tap failed", s.card.kind)

    def handle_key(self, name: str) -> bool:
        """A key press while a card shows. "escape" closes it; others go to the card."""
        name = str(name or "").lower()
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            if name != "escape":
                self._touched(s)
                try:
                    return bool(s.card.key(name))
                except Exception:  # noqa: BLE001
                    log.exception("card %s key failed", s.card.kind)
                    return False
        self._end(s, "keyboard")
        return True

    # ================================================================ internals
    def _result(self, ok: bool, error: str | None) -> dict[str, Any]:
        if not ok:
            self.last_error = error
        return {"ok": ok, "showing": self.describe(), "error": error}

    def _blocked(self) -> str | None:
        """Why a card can't go up right now: "disabled", "busy" or None."""
        if not self.enabled:
            return "disabled"
        ctx = self.ctx
        games = getattr(ctx, "games", None)
        try:
            if games is not None and games.active:
                return "busy"
        except Exception:  # noqa: BLE001
            log.debug("games status failed", exc_info=True)
        holder = ctx.arbiter.holder(Resource.SCREEN)
        if holder is not None and holder.owner != OWNER:
            return "busy"
        acts = getattr(ctx, "activities", None)
        try:
            if acts is not None and acts.status().get("running"):
                return "busy"
        except Exception:  # noqa: BLE001
            log.debug("activities status failed", exc_info=True)
        if ctx.state.state in _BUSY_STATES:
            return "busy"
        return None

    def _advance(self, s: _Session, dt: float, now: float) -> None:
        """Move the card and the presenter's mouth on by ``dt`` (caller holds the lock)."""
        s.t += dt
        level, shape, when = self._voice
        fresh = now - when < VOICE_FRESH
        target = level if fresh else 0.0
        k = min(1.0, MOUTH_RATE * dt) if dt > 0 else 0.0
        s.mouth += (target - s.mouth) * k
        if s.mouth < 0.01:
            s.mouth = 0.0
        s.shape = shape if fresh and target > 0.02 else ""
        s.card.update(dt)

    def _paint(self, s: _Session, surface: Any, rect: Any) -> None:
        import pygame  # noqa: PLC0415

        from littlebot.games import art  # noqa: PLC0415
        from littlebot.screens import presenter  # noqa: PLC0415

        rect = pygame.Rect(rect) if rect is not None else surface.get_rect()
        direct = rect.size == (W, H) and surface.get_rect().contains(rect)
        if direct:
            canvas = surface.subsurface(rect)
        else:
            if self._canvas is None:
                self._canvas = pygame.Surface((W, H), 0, 32)
            canvas = self._canvas
        if not self._draw_picture(s, canvas):
            canvas.blit(art.background((W, H)), (0, 0))  # the same calm pink under every card
            s.card.draw(canvas)
        backing = bool(getattr(getattr(self.ctx.config, "screens", None), "presenter_backing", True))
        presenter.draw(canvas, _box(s.card), mood=str(s.card.mood or "neutral"), mouth=s.mouth, shape=s.shape,
                       color=s.host.eye_color, t=s.t, backing=backing)
        if direct:
            return
        if self._scaled is None or self._scaled.get_size() != rect.size:
            self._scaled = pygame.Surface(rect.size, 0, 32)
        pygame.transform.smoothscale(canvas, rect.size, self._scaled)
        surface.blit(self._scaled, rect)

    def _check(self, s: _Session, *, force: bool = False) -> None:
        """Lifetime: done / closed by the card, the chat's linger, headless driving, keep-awake."""
        end: str | None = None
        awake = False
        with self._lock:
            if s.ended or self._session is not s:
                return
            now = self._clock()
            if not force and now - s.last_check < WATCH_PERIOD:
                return
            s.last_check = now
            card = s.card
            try:
                if now - s.last_render > HEADLESS_AFTER:  # no window: keep the card's clock running
                    self._advance(s, max(0.0, min(now - s.last_tick, 0.25)), now)
                    s.last_tick = now
            except Exception:  # noqa: BLE001
                log.exception("card %s update failed", card.kind)
            if s.close_req:
                end = s.close_req
            elif card.done:
                end = "done"
            elif self.voice_on():
                s.quiet_since = None
            else:
                if s.quiet_since is None:
                    s.quiet_since = now
                if not card.sticky and now - s.quiet_since >= self._linger(s):
                    end = "chat over"
            if end is None and card.sticky and now - s.awake_t >= KEEP_AWAKE_EVERY:
                s.awake_t = now
                awake = True
        if end is not None:
            self._end(s, end)
        elif awake:
            self._publish(KEEP_AWAKE, {"kind": s.card.kind})

    def _end(self, s: _Session, reason: str) -> bool:
        """Single, idempotent cleanup path for every way a card goes."""
        with self._lock:
            if s.ended:
                return False
            s.ended = True
            if self._session is s:
                self._session = None
            close = getattr(s.card, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.debug("card close failed", exc_info=True)
        task, loop = s.task, s.loop
        if task is not None and loop is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # loop closed
                pass
        if s.lease is not None:
            s.lease.release()
        seconds = round(max(0.0, self._clock() - s.shown), 1)
        self._last = {"kind": s.card.kind, "title": s.card.title, "reason": reason, "seconds": seconds,
                      "source": s.source, "ended_at": time.time()}
        log.info("card %s closed (%s) after %.0fs", s.card.kind, reason, seconds)
        self._publish(CARD_CLOSED, {"kind": s.card.kind, "reason": reason})
        s.zoomed = None
        self._free_memory()
        return True

    def _free_memory(self) -> None:
        """No card on the screen any more: its drawings (text, panels, the presenter), its pictures and the
        canvas are let go at once, not kept for a next card (tens of MB on a 2 GB Pi; a new card draws its own
        in a few ms), and the memory is given back to the system (littlebot/core/memory.py; ~25 ms, so on a
        thread of its own). Not while another card took its place or a game (which shares the drawings) is on."""
        with self._lock:
            if self._session is not None:
                return
            self._canvas = self._scaled = None
        games = getattr(self.ctx, "games", None)
        try:
            if games is not None and games.active:
                return
        except Exception:  # noqa: BLE001
            return
        self._freeing = threading.Thread(target=self._free_now, name="card-memory", daemon=True)
        self._freeing.start()

    def _free_now(self) -> None:
        try:
            from littlebot.core import memory  # noqa: PLC0415
            from littlebot.games import art  # noqa: PLC0415
            from littlebot.screens import images  # noqa: PLC0415
            from littlebot.screens.cards import _kit  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - pygame not there (a headless robot): nothing was drawn
            return
        with self._lock:
            if self._session is not None:  # a new card came meanwhile: it is drawing with them
                return
            freed = art.cache_bytes() + images.cache_bytes()
            art.clear_caches()
            images.clear_cache()
            _kit.wrap.cache_clear()
        memory.give_back()
        log.info("card memory freed (%.1f MB of drawings and pictures)", freed / 1e6)

    def _start_watch(self, s: _Session) -> None:
        try:
            s.loop = asyncio.get_running_loop()
            s.task = s.loop.create_task(self._watch(s), name=f"card:{s.card.kind}")
            return
        except RuntimeError:
            pass
        runtime = self.ctx.runtime
        if runtime is not None and runtime.running:
            s.loop = runtime.loop
            s.task = None
            runtime.submit(self._watch(s))

    async def _watch(self, s: _Session) -> None:
        while not s.ended:
            await asyncio.sleep(WATCH_PERIOD)
            try:
                self._check(s, force=True)
            except Exception:  # noqa: BLE001
                log.exception("card watchdog failed")

    def _submit(self, make: Callable[[Any], Coroutine[Any, Any, Any]], what: str) -> None:
        """Run ``make(ctx.audio)`` on the robot's loop, fire-and-forget (from any thread)."""
        audio = self.ctx.audio
        if audio is None:
            return
        try:
            coro = make(audio)
        except Exception:  # noqa: BLE001
            log.debug("%s failed", what, exc_info=True)
            return
        runtime = self.ctx.runtime
        try:
            if runtime is not None and runtime.running:
                runtime.submit(coro).add_done_callback(lambda f: _log_failure(f, what))
                return
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()  # no loop to play it on (headless, no runtime)

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="screens")
        except Exception:  # noqa: BLE001
            log.exception("publishing %s failed", topic)

    # ---------------------------------------------------------------- bus callbacks
    def _on_viseme(self, event: Event) -> None:
        try:
            level = float(event.get("level") or 0.0)
        except (TypeError, ValueError):
            level = 0.0
        self._voice = (min(1.0, max(0.0, level)), str(event.get("shape") or ""), self._clock())

    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _STOP_STATES and self._session is not None:
            self.close(f"robot {str(new).lower()}")

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "screens.enabled" and not event.get("value"):
            self.close("switched off")


def _canvas(nx: float, ny: float) -> tuple[float, float]:
    return min(1.0, max(0.0, float(nx))) * W, min(1.0, max(0.0, float(ny))) * H


def _on_eyes(card: Card, x: float, y: float) -> bool:
    bx, by, bw, bh = _box(card)
    return bx - TAP_MARGIN <= x <= bx + bw + TAP_MARGIN and by - TAP_MARGIN <= y <= by + bh + TAP_MARGIN


def _box(card: Card) -> tuple[int, int, int, int]:
    """The card's presenter box, kept on the canvas (the eyes are what make it LittleBot presenting)."""
    try:
        x, y, w, h = (int(v) for v in card.presenter_box)
    except (TypeError, ValueError):
        from littlebot.screens.base import PRESENTER_BOX  # noqa: PLC0415

        x, y, w, h = PRESENTER_BOX
    w, h = max(60, min(w, W)), max(40, min(h, H))
    return max(0, min(x, W - w)), max(0, min(y, H - h)), w, h


def _log_failure(fut: Any, what: str) -> None:
    try:
        fut.result()
    except Exception as e:  # noqa: BLE001
        log.debug("card %s failed: %s", what, e)


__all__ = ["CARD_CLOSED", "CARD_SHOWN", "KEEP_AWAKE", "OWNER", "SCREEN_NEWS", "ScreenService"]
