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
from littlebot.face.expressions import EXPRESSION_PARAMS
from littlebot.face.frame import FaceFrame
from littlebot.face.mouth import VISEMES

if TYPE_CHECKING:  # pragma: no cover
    import random

    import pygame

    from littlebot.core.context import RobotContext
    from littlebot.face.renderer import FaceRenderer

TICK_HZ = 10.0  # headless update rate
HIDDEN_FPS = 5  # frame rate while a video owns the screen
MAX_COMMANDS = 512

_BUS_TOPICS = (
    Topics.AUDIO_SPEECH_STARTED,
    Topics.AUDIO_SPEECH_FINISHED,
    Topics.AUDIO_VISEME,
    Topics.AUDIO_LISTENING_STARTED,
    Topics.AUDIO_LISTENING_STOPPED,
)


def _finite(*values: Any) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


class FaceController(Service):
    """Animated face service. See module docstring for the threading model."""

    name = "face"

    def __init__(self, ctx: "RobotContext", *, rng: "random.Random | None" = None, seed: int | None = None) -> None:
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
        self._subscribe()

    # ================================================================ FaceAPI
    def set_expression(self, name: str, *, hold: float | None = None) -> None:
        if name not in EXPRESSION_PARAMS:
            self.log.warning("unknown expression %r ignored", name)
            return
        if hold is not None and not _finite(hold):
            self.log.warning("invalid hold %r for expression %s ignored", hold, name)
            hold = None
        self._enqueue("set_expression", name, None if hold is None else float(hold))

    def look_at(self, x: float, y: float, *, hold: float | None = None) -> None:
        if not _finite(x, y):
            self.log.warning("look_at(%r, %r) ignored: not finite numbers", x, y)
            return
        if hold is not None and not _finite(hold):
            hold = None
        self._enqueue("look_at", float(x), float(y), None if hold is None else float(hold))

    def track_gaze(self, x: float, y: float) -> None:
        """Soft gaze target from face tracking (explicit ``look_at`` wins while it holds)."""
        if not _finite(x, y):
            return
        self._enqueue("track_gaze", float(x), float(y))

    def clear_gaze(self) -> None:
        self._enqueue("clear_gaze")

    def blink(self, double: bool = False) -> None:
        self._enqueue("blink", bool(double))

    def close_eyes(self) -> None:
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
                "eyes_closed": bool(a.eyes_closed or a.state == "sleeping"),
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
        """Frame rate the display should use right now (drops when calm / hidden)."""
        with self._lock:
            a = self.animator
            if a.state == "hidden" and a.hidden_amount >= 1.0:
                return min(HIDDEN_FPS, self.display_config.idle_fps)
            animating = a.is_animating()
        return int(self.display_config.fps if animating else self.display_config.idle_fps)

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
