"""Info cards: what LittleBot shows on its screen while it talks (weather, a word's meaning, a recipe step).

The screen is an extension of what the voice says: a card shows the gist, big and readable, while
LittleBot's own eyes sit small in a corner as the presenter. A card is a plain object driven by the
display loop, like an on-screen game (littlebot/games/base.py):

* ``update(dt)`` advances it by ``dt`` seconds (pure logic, no drawing);
* ``draw(surface)`` paints the whole 720x480 canvas (:data:`W` x :data:`H`), leaving
  :attr:`Card.presenter_box` free: the screen service draws the presenter's eyes there afterwards;
* ``tap(x, y)`` / ``key(name)`` deliver touch (canvas pixels) and keys (pygame key names);
* ``command(action, args)`` is the voice steering it ("show me tomorrow": ``("day", {"offset": 1})``,
  "next step": ``("next", {})``); each card lists its actions in :attr:`CardType.actions`;
* ``describe()`` is one line for the voice: what is on the screen right now.

Cards never touch the robot directly: they ask their :class:`CardHost` (sounds, a quiet note to the
voice, a picture). :class:`RecordingHost` stands in for tests and screenshots
(``scripts/screen_preview.py``).

Where cards come from: :func:`register` adds a card type (its ``kind``, a one-line description and
the JSON fields the AI fills in, see :class:`CardType`); littlebot/screens/director.py picks one from
:func:`card_types` for what was just asked and builds it with :func:`build`.

Readability rules every card keeps (a 3.5" screen across a room; a parent cooking):
* text at least :data:`BODY_PX` (titles :data:`TITLE_PX`, a recipe step :data:`STEP_PX`);
* at most :data:`MAX_LINES` lines of text at a time: more is paged (tap, swipe or "next");
* the gist, not the transcript: the voice says the rest.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("littlebot.screens")

W, H = 720, 480  # the canvas (same as the games: the robot's 720x480 screen)
TITLE_PX = 44
BODY_PX = 32
SMALL_PX = 26  # the smallest text anywhere (labels under a number, a source line)
STEP_PX = 42
MAX_LINES = 5
# Every card with a picture (a ``slot``, littlebot/screens/cards/_kit.py PictureSlot) understands this action:
# the screen service shows its picture full screen, zoomed ("zoom in", "make the picture fill the screen").
PICTURE_ACTION = "picture"
PICTURE_ACTION_HELP = ("the picture full screen or zoomed: {\"view\": \"fill\" | \"in\" | \"out\" | \"back\"} (back: "
                       "the whole card again)")
PICTURE_ZOOMS = (1.0, 1.5, 2.25, 3.0)  # fill the screen, then "in" steps (the picture's middle)
# Where the presenter's eyes sit by default: top-left, (x, y, w, h) on the canvas. A card may move it
# (presenter_box) but keeps it on screen: the eyes are what make it LittleBot presenting.
PRESENTER_BOX = (12, 10, 150, 86)


class Picture(Protocol):
    """A picture being fetched for a card (littlebot/screens/images.py). Never saved anywhere."""

    @property
    def ready(self) -> bool: ...  # fetched (``surface`` is set) or failed (``surface`` stays None)

    @property
    def surface(self) -> Any: ...  # a pygame.Surface once fetched, else None

    @property
    def credit(self) -> str: ...  # where it came from ("Wikipedia"), shown small under it


class CardHost(Protocol):
    """What a card may ask of the robot. Every method is fire-and-forget and never blocks."""

    eye_color: tuple[int, int, int]

    def now(self) -> float:
        """Monotonic seconds."""

    def sound(self, name: str) -> None:
        """A built-in sound effect (``core.interfaces.SOUND_NAMES``: "chime", "bell", "pop", ...)."""

    def say(self, text: str) -> None:
        """Speak a few words out loud (a timer that rings when no voice chat is on)."""

    def tell(self, text: str) -> None:
        """A quiet note for the live voice, when a chat is on (the timer rang; "the screen shows step 3")."""

    def picture(self, query: str = "", url: str = "") -> Picture:
        """Start fetching a picture of ``query`` (or from ``url``); poll ``ready``."""

    def close(self) -> None:
        """Take this card off the screen (the face comes back)."""


class Card:
    """Base class. Subclasses set ``kind`` and override ``draw`` (and ``update``, ``tap``, ``key``,
    ``command``, ``describe`` as needed)."""

    kind = ""
    sticky = False  # True: stays after the chat ends until it is done (a running timer); else it lingers a while

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        self.spec = dict(spec)
        self.host: CardHost = host if host is not None else RecordingHost()
        self.title = str(spec.get("title") or "")[:80]
        self.t = 0.0  # seconds on screen
        self.presenter_box = PRESENTER_BOX
        self.mood = "neutral"  # the presenter's eyes: "neutral", "happy", "surprised", "curious"
        self.done = False  # set when the card has nothing more to show (the service then closes it)

    def update(self, dt: float) -> None:
        self.t += dt

    def draw(self, surf: Any) -> None:
        raise NotImplementedError

    def tap(self, x: float, y: float) -> bool:
        return False

    def key(self, name: str) -> bool:
        return False

    def command(self, action: str, args: dict[str, Any]) -> bool:
        """Steer the card from the voice. True when it understood ``action``."""
        return False

    def describe(self) -> str:
        return self.title or self.kind

    def status(self) -> dict[str, Any]:
        """For the dashboard and tests (no pictures, no audio)."""
        return {"kind": self.kind, "title": self.title, "showing": self.describe(), "seconds": round(self.t, 1)}


Builder = Callable[[dict[str, Any], Any, CardHost], Awaitable[Card | None]]


@dataclass(frozen=True)
class CardType:
    """One kind of card, as the AI sees it.

    ``fields``: the JSON the AI fills in (JSON Schema "properties"); ``required`` of them must be there.
    ``build(spec, ctx, host)``: fetches what it needs (the forecast) and returns the card, or None
    when there is nothing to show. ``actions``: what ``command()`` understands, for the AI
    ({"day": "offset (int, 0 today) or a weekday name", "next": "the next step"}).
    """

    kind: str
    description: str
    fields: dict[str, Any]
    build: Builder
    required: tuple[str, ...] = ()
    actions: dict[str, str] = field(default_factory=dict)


_TYPES: dict[str, CardType] = {}


def register(card_type: CardType) -> CardType:
    _TYPES[card_type.kind] = card_type
    return card_type


def card_types() -> dict[str, CardType]:
    """Every registered card type (importing littlebot.screens.cards registers the built-in ones)."""
    import littlebot.screens.cards  # noqa: F401, PLC0415

    return dict(_TYPES)


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> Card | None:
    """The card ``spec["card"]`` asks for, or None (unknown kind, missing fields, nothing to show)."""
    kind = str(spec.get("card") or "")
    ct = card_types().get(kind)
    if ct is None:
        log.info("screens: no card type %r", kind[:40])
        return None
    missing = [f for f in ct.required if spec.get(f) in (None, "", [])]
    if missing:
        log.info("screens: %s card without %s", kind, ", ".join(missing))
        return None
    try:
        return await ct.build(spec, ctx, host)
    except Exception:  # noqa: BLE001 - a card that can't be made is simply not shown
        log.warning("screens: building a %s card failed", kind, exc_info=True)
        return None


class _NoPicture:
    ready = True
    surface = None
    credit = ""


class RecordingHost:
    """A host that records what a card asked for (tests, screenshots, headless runs)."""

    def __init__(self, *, eye_color: tuple[int, int, int] = (90, 210, 255), clock: Any = None,
                 pictures: dict[str, Any] | None = None) -> None:
        self.eye_color = eye_color
        self.sounds: list[str] = []
        self.spoken: list[str] = []
        self.told: list[str] = []
        self.asked_pictures: list[tuple[str, str]] = []
        self.pictures = pictures or {}  # query or url -> a Picture to hand back
        self.closed = False
        self._clock = clock or time.monotonic

    def now(self) -> float:
        return float(self._clock())

    def sound(self, name: str) -> None:
        self.sounds.append(name)

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def tell(self, text: str) -> None:
        self.told.append(text)

    def picture(self, query: str = "", url: str = "") -> Picture:
        self.asked_pictures.append((query, url))
        return self.pictures.get(query or url, _NoPicture())

    def close(self) -> None:
        self.closed = True


__all__ = ["BODY_PX", "H", "MAX_LINES", "PRESENTER_BOX", "SMALL_PX", "STEP_PX", "TITLE_PX", "W", "Card",
           "CardHost", "CardType", "Picture", "RecordingHost", "build", "card_types", "register"]
