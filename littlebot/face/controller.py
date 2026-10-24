"""FaceController: the face Service implementing ``core.interfaces.FaceAPI``.

Threading model
---------------
* Public API methods (``set_expression``, ``look_at``, ``track_gaze`` ...) only
  validate their arguments and append a command to a bounded deque. They never
  block and are safe from any thread (dashboard, asyncio loop, bus callbacks).
* Commands are applied to the :class:`FaceAnimator` under a short lock by
  whoever advances or inspects the face: ``update()`` (display loop or the
  headless ticker), ``frame()`` / ``render()`` and ``status()`` - so
  ``status()`` always reflects every command issued before it.
* Bus events the face reacts to (speech start/finish/level, listening) are
  turned into commands by tiny subscriber callbacks. ``audio.level`` events
  carry ``{"level", "shape"}`` (loudness and mouth shape of the sound being
  heard); a bare ``{"level"}`` still works (the shape is guessed).
* With no display (headless runs, tests) ``start()`` runs a ~10 Hz ticker
  thread so blinks, holds and status keep working. The display loop calls
  :meth:`attach_display` to take over driving ``update()`` itself.

Dozing
------
When nothing is happening the idle face dozes (droopy eyes slowly closing, a
floating "zzz"); it is only a look, the robot stays IDLE and everything keeps
listening. :class:`~littlebot.face.doze.DozeTimer` decides, fed by bus events
(see ``_DOZE_HOLDS`` / ``_DOZE_ENDS`` / ``_DOZE_POKES``: a voice chat, speech,
sounds, cards, games, activities, videos, a busy robot state, someone walking
in or moving) and by the face's own commands (an expression, animation or look
someone asked for). ``update()`` applies its decision. Settings:
``face.doze_enabled``, ``face.doze_after_seconds``, ``face.awake_on_arrival_seconds``.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import TYPE_CHECKING, Any

from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.interfaces import FACE_ANIMATIONS, FACE_STATES
from littlebot.core.service import Service, ServiceStatus
from littlebot.face.animator import FaceAnimator
from littlebot.face.doze import (
    AWAKE_ON_ARRIVAL_S,
    DOZE_AFTER_S,
    MAX_SECONDS,
    MIN_DOZE_AFTER_S,
    DozeTimer,
)
from littlebot.face.expressions import EXPRESSION_PARAMS
from littlebot.face.frame import FaceFrame
from littlebot.face.mouth import VISEMES

if TYPE_CHECKING:  # pragma: no cover
    import random
    from collections.abc import Callable

    import pygame

    from littlebot.core.context import RobotContext
    from littlebot.face.renderer import FaceRenderer

TICK_HZ = 10.0  # headless update rate
HIDDEN_FPS = 5  # frame rate while a video owns the screen
DOZE_FPS = 10  # frame rate while dozing (the lids and the "zzz" move slowly)
MAX_COMMANDS = 512

_BUS_TOPICS = (
    Topics.AUDIO_SPEECH_STARTED,
    Topics.AUDIO_SPEECH_FINISHED,
    Topics.AUDIO_VISEME,
    Topics.AUDIO_LISTENING_STARTED,
    Topics.AUDIO_LISTENING_STOPPED,
)

# ---- dozing: what keeps the face awake (topics owned by other subsystems are named as they publish them)
DOZE_SETTINGS = ("face.doze_enabled", "face.doze_after_seconds", "face.awake_on_arrival_seconds")
GLANCE_S = 5.0  # an expression / animation / look someone asked for keeps the face awake at least this long
QUIET_EXPRESSIONS = frozenset({"neutral", "sleepy"})  # a calm reset (STOP ALL) or a yawn doesn't wake it
MOVE_THRESHOLD = 0.15  # the tracked face moved this far (0..1 of the view): someone moved
NEW_FACE_GAP = 10.0  # s: one more face than any time in this long counts as someone new
MAX_FACES_COUNTED = 8
SHORT_HOLD_S = 600.0  # safety limit if a "finished" event never comes (speech, sounds, listening)
LONG_HOLD_S = 7200.0  # ... (a chat, a game, an activity)
SLEEP_REASON = "LittleBot went to sleep"

# topic -> (hold key, why it keeps the face awake, safety limit in seconds or None)
_DOZE_HOLDS: dict[str, tuple[str, str, float | None]] = {
    "live.call_started": ("chat", "a voice chat", LONG_HOLD_S),  # littlebot/chatgpt/live.py
    Topics.AUDIO_LISTENING_STARTED: ("listening", "listening", SHORT_HOLD_S),
    Topics.AUDIO_SPEECH_STARTED: ("speech", "LittleBot is talking", SHORT_HOLD_S),
    Topics.AUDIO_SOUND_STARTED: ("sound", "a sound is playing", SHORT_HOLD_S),
    Topics.ACTIVITY_STARTED: ("activity", "an activity", LONG_HOLD_S),
    "game.started": ("game", "a game", LONG_HOLD_S),  # littlebot/games/service.py
    Topics.MEDIA_STARTED: ("media", "a video", None),
    Topics.MEDIA_RESUMED: ("media", "a video", None),
    "screen.card_shown": ("card", "a card on the screen", None),  # littlebot/screens/service.py
    "screen.keep_awake": ("card", "a card on the screen", None),  # a running timer card, every minute
}
_DOZE_ENDS: dict[str, str] = {
    "live.call_ended": "chat",
    Topics.AUDIO_LISTENING_STOPPED: "listening",
    Topics.AUDIO_SPEECH_FINISHED: "speech",
    Topics.AUDIO_SOUND_FINISHED: "sound",
    Topics.ACTIVITY_FINISHED: "activity",
    "game.finished": "game",
    Topics.MEDIA_STOPPED: "media",
    Topics.MEDIA_ERROR: "media",
    "screen.card_closed": "card",
}
_DOZE_POKES: dict[str, str] = {
    "audio.wake_word": "the wake word",  # littlebot/audio/wakeword.py
    Topics.AUDIO_UTTERANCE: "someone spoke",
    Topics.BRAIN_REPLY: "a reply",
    Topics.CAREGIVER_RESPONSE: "a grown-up answered",
    Topics.ACTIVITY_STEP: "an activity",
}
# robot state -> why it keeps the face awake (IDLE is the only state the face can doze in)
_BUSY_STATES = {
    "LISTENING": "listening", "THINKING": "thinking", "SPEAKING": "talking", "PLAYING_ACTIVITY": "an activity",
    "PLAYING_MEDIA": "a video", "SLEEPING": SLEEP_REASON, "ERROR": "an error",
}
_DOZE_TOPICS = tuple(dict.fromkeys((
    *_DOZE_HOLDS, *_DOZE_ENDS, *_DOZE_POKES, Topics.STATE_CHANGED, Topics.STOP_ALL, Topics.SETTINGS_CHANGED,
    Topics.VISION_PERSON_PRESENT, Topics.VISION_FACES, Topics.VISION_GESTURE,
)))


def _finite(*values: Any) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


def _seconds_validator(key: str, lo: int, hi: int) -> Callable[[Any], int]:
    """Settings validator: whole seconds clamped into ``lo..hi``."""
    def check(value: Any) -> int:
        if isinstance(value, bool) or not _finite(value):
            raise ValueError(f"{key}: expected a number of seconds")
        return max(lo, min(hi, round(float(value))))
    return check


class FaceController(Service):
    """Animated face service. See module docstring for the threading model."""

    name = "face"

    def __init__(self, ctx: "RobotContext", *, rng: "random.Random | None" = None, seed: int | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        # core.service.Service keeps lifecycle state in ``self.status``, which would shadow the
        # FaceAPI ``status()`` method: keep it in ``service_status`` (same pattern as audio/media).
        self.__dict__.pop("status", None)
        self.service_status = ServiceStatus.STOPPED
        self.ctx = ctx
        self.bus = ctx.bus
        self.display_config = ctx.config.display
        self.animator = FaceAnimator(
            ctx.config.face, width=ctx.config.display.width, height=ctx.config.display.height, rng=rng, seed=seed,
        )
        self._lock = threading.RLock()
        self._cmds: collections.deque[tuple[str, tuple[Any, ...]]] = collections.deque()
        self.dropped_commands = 0
        self._renderer: "FaceRenderer | None" = None
        self._display_attached = False
        self._ticker: threading.Thread | None = None
        self._ticker_stop = threading.Event()
        self._subs: list[Subscription] = []
        self._render_times: collections.deque[float] = collections.deque(maxlen=90)
        self._render_ms: collections.deque[float] = collections.deque(maxlen=90)
        self._update_errors = 0
        # dozing (see the module docstring); the timer has its own small lock (bus callbacks feed it)
        self._clock: Callable[[], float] = clock or time.monotonic
        self._doze_lock = threading.Lock()
        self.doze = DozeTimer(now=self._clock())
        self._face_anchor: tuple[float, float] | None = None  # where the tracked face was (movement)
        self._faces_seen = 0
        self._faces_seen_at: dict[int, float] = {}  # number of faces -> when last seen (someone new)
        self._load_doze_settings(first=True)
        state = getattr(ctx, "state", None)
        if state is not None and getattr(state, "state", None) is not None:
            self._doze_state(str(state.state.value), self._clock())
        self._subscribe()

    # ================================================================ FaceAPI
    def set_expression(self, name: str, *, hold: float | None = None) -> None:
        if name not in EXPRESSION_PARAMS:
            self.log.warning("unknown expression %r ignored", name)
            return
        if hold is not None and not _finite(hold):
            self.log.warning("invalid hold %r for expression %s ignored", hold, name)
            hold = None
        if name not in QUIET_EXPRESSIONS:
            self._glance(hold)
        self._enqueue("set_expression", name, None if hold is None else float(hold))

    def look_at(self, x: float, y: float, *, hold: float | None = None) -> None:
        if not _finite(x, y):
            self.log.warning("look_at(%r, %r) ignored: not finite numbers", x, y)
            return
        if hold is not None and not _finite(hold):
            hold = None
        self._glance(hold)
        self._enqueue("look_at", float(x), float(y), None if hold is None else float(hold))

    def track_gaze(self, x: float, y: float) -> None:
        """Soft gaze target from face tracking (explicit ``look_at`` wins while it holds)."""
        if not _finite(x, y):
            return
        self._enqueue("track_gaze", float(x), float(y))

    def clear_gaze(self) -> None:
        self._enqueue("clear_gaze")

    def blink(self, double: bool = False) -> None:
        self._glance(None)
        self._enqueue("blink", bool(double))

    def close_eyes(self) -> None:
        self._glance(None)
        self._enqueue("close_eyes")

    def open_eyes(self) -> None:
        self._enqueue("open_eyes")

    def set_state(self, state: str) -> None:
        if state not in FACE_STATES:
            self.log.warning("unknown face state %r ignored", state)
            return
        self._enqueue("set_state", state)

    def play_animation(self, name: str) -> None:
        if name not in FACE_ANIMATIONS:
            self.log.warning("unknown face animation %r ignored", name)
            return
        if name != "zzz":
            self._glance(None)
        self._enqueue("play_animation", name)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._drain_locked()
            a = self.animator
            gx, gy = a.gaze()
            out: dict[str, Any] = {
                "expression": a.expression,
                "state": a.state,
                "gaze": {"x": round(gx, 3), "y": round(gy, 3)},
                "eyes_closed": bool(a.eyes_closed or a.state == "sleeping" or a.doze_lid >= 0.9),
                "dozing": bool(a.dozing),
                "fps": self.fps,
                "visible": a.visible,
                "talking": a.talking,
                "listening": a.listening_active or a.state == "listening",
                "animations": a.active_animations(),
                "display": self._display_attached,
            }
            events = a.pop_events()
        self._publish(events)
        return out

    # ================================================================ driving
    def update(self, dt: float) -> None:
        """Advance animation by ``dt`` seconds (display loop / ticker / tests)."""
        with self._lock:
            self._drain_locked()
            self.animator.update(dt)
            events = self.animator.pop_events()
        self._publish(events)
        self._check_doze()

    def frame(self) -> FaceFrame:
        """Snapshot of the current face as plain data."""
        with self._lock:
            self._drain_locked()
            f = self.animator.frame()
            events = self.animator.pop_events()
        self._publish(events)
        return f

    def render(self, surface: "pygame.Surface", rect: "pygame.Rect | None" = None) -> None:
        """Draw the current frame onto ``surface`` (main/display thread only)."""
        f = self.frame()
        if self._renderer is None:
            from littlebot.face.renderer import FaceRenderer

            self._renderer = FaceRenderer((self.display_config.width, self.display_config.height))
        self._renderer.render(f, surface, rect)
        now = time.monotonic()
        self._render_times.append(now)
        self._render_ms.append(self._renderer.last_ms)

    @property
    def renderer(self) -> "FaceRenderer | None":
        return self._renderer

    def wants_high_fps(self) -> bool:
        with self._lock:
            return self.animator.is_animating()

    def target_fps(self) -> int:
        """Frame rate the display should use right now (drops when calm / hidden / dozing)."""
        with self._lock:
            a = self.animator
            if a.state == "hidden" and a.hidden_amount >= 1.0:
                return min(HIDDEN_FPS, self.display_config.idle_fps)
            animating = a.is_animating()
            dozed_off = a.dozing and a.doze_amount >= 1.0
        if animating:
            return int(self.display_config.fps)
        if dozed_off:
            return int(min(DOZE_FPS, self.display_config.idle_fps))
        return int(self.display_config.idle_fps)

    @property
    def dozing(self) -> bool:
        """True while the idle face dozes (a look only: the robot is still IDLE and listening)."""
        return bool(self.animator.dozing)

    @property
    def fps(self) -> float:
        times = list(self._render_times)
        if len(times) < 2 or time.monotonic() - times[-1] > 2.0:
            return 0.0
        span = times[-1] - times[0]
        return round((len(times) - 1) / span, 1) if span > 0 else 0.0

    @property
    def render_ms(self) -> float:
        ms = list(self._render_ms)
        return round(sum(ms) / len(ms), 2) if ms else 0.0

    # -- display hand-over ---------------------------------------------------
    def attach_display(self) -> None:
        """The display loop now drives ``update()``; the headless ticker idles."""
        self._display_attached = True

    def detach_display(self) -> None:
        self._display_attached = False
        self._render_times.clear()

    @property
    def display_attached(self) -> bool:
        return self._display_attached

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._subscribe()
        self.start_ticker()
        self.set_status(ServiceStatus.RUNNING)

    async def stop(self) -> None:
        import asyncio

        await asyncio.to_thread(self.stop_ticker)
        self._unsubscribe()
        self.set_status(ServiceStatus.STOPPED)

    def start_ticker(self) -> None:
        if self._ticker is not None and self._ticker.is_alive():
            return
        self._ticker_stop.clear()
        self._ticker = threading.Thread(target=self._tick_loop, name="face-ticker", daemon=True)
        self._ticker.start()

    def stop_ticker(self, timeout: float = 2.0) -> None:
        self._ticker_stop.set()
        t = self._ticker
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._ticker = None

    @property
    def ticker_running(self) -> bool:
        return self._ticker is not None and self._ticker.is_alive()

    def set_status(self, status: ServiceStatus, detail: str = "") -> None:  # type: ignore[override]
        if status != self.service_status or detail != self.status_detail:
            self.log.info("status %s%s", status.value, f" ({detail})" if detail else "")
        self.service_status, self.status_detail = status, detail

    def health(self) -> dict[str, Any]:
        h = {"name": self.name, "status": self.service_status.value, "detail": self.status_detail}
        h.update({"display": self._display_attached, "fps": self.fps, "ticker": self.ticker_running,
                  "render_ms": self.render_ms, "dropped_commands": self.dropped_commands})
        with self._doze_lock:
            h["doze"] = self.doze.status(self._clock())
        return h

    # ================================================================ internals
    def _tick_loop(self) -> None:
        period = 1.0 / TICK_HZ
        last = time.monotonic()
        while not self._ticker_stop.wait(period):
            now = time.monotonic()
            dt, last = now - last, now
            if self._display_attached:
                continue
            try:
                self.update(dt)
                self._update_errors = 0
            except Exception:  # noqa: BLE001 - never let the ticker die
                self._update_errors += 1
                if self._update_errors <= 3:
                    self.log.exception("face update failed")

    def _enqueue(self, method: str, *args: Any) -> None:
        if len(self._cmds) >= MAX_COMMANDS:
            try:
                self._cmds.popleft()
            except IndexError:
                pass
            self.dropped_commands += 1
            if self.dropped_commands in (1, 100, 1000):
                self.log.warning("face command queue full; dropped %d commands", self.dropped_commands)
        self._cmds.append((method, args))

    def _drain_locked(self) -> None:
        a = self.animator
        while True:
            try:
                method, args = self._cmds.popleft()
            except IndexError:
                return
            try:
                getattr(a, method)(*args)
            except Exception:  # noqa: BLE001 - a bad command must not break the face
                self.log.exception("face command %s%r failed", method, args)

    def _publish(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        for topic, data in events:
            try:
                self.bus.publish(topic, data, source="face")
            except Exception:  # noqa: BLE001
                self.log.exception("publishing %s failed", topic)

    def _subscribe(self) -> None:
        if self._subs:
            return
        self._subs = [self.bus.subscribe(t, self._on_bus_event) for t in _BUS_TOPICS]
        self._subs += [self.bus.subscribe(t, self._on_doze_event) for t in _DOZE_TOPICS]

    def _unsubscribe(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs = []

    def _on_bus_event(self, event: Event) -> None:
        topic = event.topic
        if topic == Topics.AUDIO_VISEME:
            level = event.get("level", 0.0)
            if _finite(level):
                shape = event.get("shape")
                self._enqueue("speech_level", float(level), shape if shape in VISEMES else None)
        elif topic == Topics.AUDIO_SPEECH_STARTED:
            est = event.get("duration_estimate")
            self._enqueue("speech_started", float(est) if _finite(est) else None)
        elif topic == Topics.AUDIO_SPEECH_FINISHED:
            self._enqueue("speech_finished")
        elif topic == Topics.AUDIO_LISTENING_STARTED:
            self._enqueue("listening_started")
        elif topic == Topics.AUDIO_LISTENING_STOPPED:
            self._enqueue("listening_stopped")

    # ================================================================ dozing
    def _check_doze(self) -> None:
        """Apply the doze timer's decision (called after every update)."""
        with self._doze_lock:
            change = self.doze.update(self._clock())
        if change is None:
            return
        what, why = change
        with self._lock:
            self.animator.set_doze(what == "doze")
        self._publish([(Topics.FACE_DOZE, {"dozing": what == "doze", "reason": why})])  # e.g. the battery sign on waking
        if what == "doze":
            self.log.info("face: dozing (%s)", why)
        elif why == SLEEP_REASON:
            self.log.info("face: no longer dozing (%s)", why)
        else:
            self.log.info("face: awake (%s)", why)

    def _glance(self, hold: float | None) -> None:
        """Someone asked the face to show something: awake at least while it plays."""
        seconds = max(GLANCE_S, float(hold) if hold is not None and _finite(hold) else 0.0)
        with self._doze_lock:
            self.doze.glance(self._clock(), seconds, "the face was asked to show something")

    def _load_doze_settings(self, *, first: bool = False) -> None:
        settings = getattr(self.ctx, "settings", None)
        if settings is None:
            return
        try:
            if first:
                for key, lo in (("face.doze_after_seconds", int(MIN_DOZE_AFTER_S)),
                                ("face.awake_on_arrival_seconds", 0)):
                    settings.register_validator(key, _seconds_validator(key, lo, int(MAX_SECONDS)))
            enabled = settings.get("face.doze_enabled", True)
            after = settings.get("face.doze_after_seconds", DOZE_AFTER_S)
            arrival = settings.get("face.awake_on_arrival_seconds", AWAKE_ON_ARRIVAL_S)
        except Exception:  # noqa: BLE001 - settings trouble must never break the face
            self.log.debug("reading the doze settings failed", exc_info=True)
            return
        with self._doze_lock:
            self.doze.configure(enabled=enabled is not False, doze_after=after, awake_on_arrival=arrival)

    def _doze_state(self, new: str, now: float) -> None:
        why = _BUSY_STATES.get(new)
        if why is None:  # IDLE: the quiet wait starts now
            self.doze.end("state", now)
        else:
            self.doze.begin("state", now, why)

    def _on_doze_event(self, event: Event) -> None:
        """Bus events that keep the face awake (tiny: runs in the publisher's thread)."""
        topic = event.topic
        if topic == Topics.VISION_FACES:
            self._doze_faces(event.get("faces") or [])
            return
        if topic == Topics.SETTINGS_CHANGED:
            if event.get("key") in DOZE_SETTINGS:
                self._load_doze_settings()
            return
        now = self._clock()
        with self._doze_lock:
            d = self.doze
            if topic in _DOZE_HOLDS:
                key, why, limit = _DOZE_HOLDS[topic]
                d.begin(key, now, why, limit)
            elif topic in _DOZE_ENDS:
                d.end(_DOZE_ENDS[topic], now)
            elif topic in _DOZE_POKES:
                d.activity(now, _DOZE_POKES[topic])
            elif topic == Topics.STATE_CHANGED:
                self._doze_state(str(event.get("new", "")), now)
            elif topic == Topics.STOP_ALL:
                d.clear(now)
                d.activity(now, "everything was stopped")
            elif topic == Topics.VISION_PERSON_PRESENT:
                d.arrival(now, "someone walked in")
            elif topic == Topics.VISION_GESTURE:
                d.arrival(now, "someone waved" if event.get("gesture") == "wave" else "someone moved")

    def _doze_faces(self, faces: list[Any]) -> None:
        """Camera faces: a new face (one more than lately) or the tracked face moving wakes the face up."""
        if not faces:
            self._face_anchor = None
            self._faces_seen = 0
            return
        now = self._clock()
        n = min(len(faces), MAX_FACES_COUNTED)
        confirmed = min(n, self._faces_seen)  # seen in two detections running: not a one-frame false face
        self._faces_seen = n
        why = None
        if confirmed >= 2 and now - self._faces_seen_at.get(confirmed, -1e9) > NEW_FACE_GAP:
            why = "someone new walked in"
        for k in range(1, confirmed + 1):
            self._faces_seen_at[k] = now
        try:
            face = next((f for f in faces if f.get("primary")), faces[0])
            pos = (float(face.get("gaze_x", 0.5)), float(face.get("gaze_y", 0.5)))
        except (AttributeError, TypeError, ValueError):
            pos = None
        if pos is not None and _finite(*pos):
            anchor = self._face_anchor
            if anchor is None:
                self._face_anchor = pos
            elif math.hypot(pos[0] - anchor[0], pos[1] - anchor[1]) > MOVE_THRESHOLD:
                self._face_anchor = pos
                why = why or "someone moved"
        if why is not None:
            with self._doze_lock:
                self.doze.arrival(now, why)
