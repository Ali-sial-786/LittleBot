"""Thread-safe publish/subscribe event bus.

Every module communicates through the bus instead of holding references to
each other's internals. Three delivery styles cover LittleBot's threads:

* ``subscribe(pattern, fn)``          - fn runs synchronously in the publisher's
                                        thread. Keep it tiny and thread-safe.
* ``subscribe_queue(pattern)``        - events land in a bounded queue that the
                                        consumer drains (the face render loop).
* ``subscribe_async(pattern, fn, loop)`` - fn (sync or coroutine) is scheduled on
                                        an asyncio loop (behaviour modules).

Patterns are fnmatch globs over dotted topics: ``"vision.*"``, ``"*"``.
"""

from __future__ import annotations

import asyncio
import collections
import fnmatch
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

log = logging.getLogger(__name__)


class Topics:
    """Canonical topic names. Payload keys are documented inline."""

    # --- application / state ---
    STATE_CHANGED = "state.changed"  # {old, new, reason}
    SYSTEM_STARTED = "system.started"  # {mode}
    SYSTEM_SHUTDOWN = "system.shutdown"  # {reason}
    SYSTEM_ERROR = "system.error"  # {module, error, recoverable}
    SYSTEM_HEALTH = "system.health"  # {cpu_percent, memory_mb, temperature_c, ...}
    SETTINGS_CHANGED = "settings.changed"  # {key, value}
    PRIVACY_CHANGED = "privacy.changed"  # {camera_enabled, microphone_enabled, cloud_enabled}
    STOP_ALL = "system.stop_all"  # {source} - parent pressed the big stop button

    # --- face ---
    FACE_EXPRESSION = "face.expression"  # {expression} (notification after it changed)
    FACE_BLINK = "face.blink"  # {double}
    FACE_GAZE = "face.gaze"  # {x, y}  (0..1 screen coords, 0.5,0.5 = centre)
    FACE_VISIBLE = "face.visible"  # {visible} False while video owns the screen (published by the face only)

    # --- vision ---
    VISION_FACES = "vision.faces"  # {faces: [{x, y, w, h, gaze_x, gaze_y, score, primary}], frame_w, frame_h, simulated}
    VISION_PERSON_PRESENT = "vision.person_present"  # {gaze_x, gaze_y}
    VISION_PERSON_LEFT = "vision.person_left"  # {}
    VISION_GESTURE = "vision.gesture"  # {gesture: "wave"|"motion", confidence}
    VISION_CAMERA_STATUS = "vision.camera_status"  # {status: "ok"|"disconnected"|"disabled"|"unavailable", detail, backend}

    # --- audio ---
    AUDIO_SPEECH_STARTED = "audio.speech_started"  # {text, duration_estimate}
    AUDIO_SPEECH_FINISHED = "audio.speech_finished"  # {text, interrupted}
    AUDIO_VISEME = "audio.level"  # {level} 0..1 mouth/eye pulse while speaking
    AUDIO_SOUND_STARTED = "audio.sound_started"  # {name}
    AUDIO_SOUND_FINISHED = "audio.sound_finished"  # {name, interrupted}
    AUDIO_LISTENING_STARTED = "audio.listening_started"  # {}
    AUDIO_LISTENING_STOPPED = "audio.listening_stopped"  # {}
    AUDIO_UTTERANCE = "audio.utterance"  # {text, confidence, source: "stt"|"simulated"}
    AUDIO_DEVICE_STATUS = "audio.device_status"  # {device: "microphone"|"speaker", status: ok|simulated|disconnected|unavailable|disabled, detail, backend, name}

    # --- conversation ---
    BRAIN_REPLY = "brain.reply"  # {text, provider, filtered}
    BRAIN_STATUS = "brain.status"  # {provider, status: ready|ok|error|auth_error|attention, detail}
    # (for "attention", detail is only a category label: safety|water|swallowed|emergency|stranger|hurt|lost|scared|help|danger
    #  - never the child's words)

    # --- activities ---
    ACTIVITY_STARTED = "activity.started"  # {id, name}
    ACTIVITY_STEP = "activity.step"  # {id, step_index, kind, text}
    ACTIVITY_PROMPT = "activity.prompt"  # {id, prompt_id, text, options: [str]} caregiver prompt
    ACTIVITY_PROMPT_CLEARED = "activity.prompt_cleared"  # {id, prompt_id, response}
    ACTIVITY_PAUSED = "activity.paused"  # {id}
    ACTIVITY_RESUMED = "activity.resumed"  # {id}
    ACTIVITY_FINISHED = "activity.finished"  # {id, completed: bool, reason}

    # --- media ---
    MEDIA_STARTED = "media.started"  # {item_id, title, kind}
    MEDIA_PAUSED = "media.paused"  # {item_id}
    MEDIA_RESUMED = "media.resumed"  # {item_id}
    MEDIA_STOPPED = "media.stopped"  # {item_id, reason: "ended"|"stopped"|"limit"|"error"}
    MEDIA_ERROR = "media.error"  # {item_id, error}

    # --- motion ---
    MOTION_POSITIONS = "motion.positions"  # {joints: {name: angle}, moving: bool}
    MOTION_GESTURE_STARTED = "motion.gesture_started"  # {gesture}
    MOTION_GESTURE_FINISHED = "motion.gesture_finished"  # {gesture, cancelled}
    MOTION_ESTOP = "motion.estop"  # {active: bool, source}

    # --- dashboard ---
    DASHBOARD_SETUP = "dashboard.setup"  # {active, code, url} first-run setup code (shown on the robot's screen)

    # --- caregiver / simulator input ---
    CAREGIVER_RESPONSE = "caregiver.response"  # {response, prompt_id}
    SIM_INPUT = "sim.input"  # {kind, ...} simulator-injected input


@dataclass(frozen=True)
class Event:
    topic: str
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    ts: float = field(default_factory=time.monotonic)
    seq: int = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


Callback = Callable[[Event], Any]


def topic_matches(pattern: str, topic: str) -> bool:
    return pattern == topic or pattern == "*" or fnmatch.fnmatchcase(topic, pattern)


class Subscription:
    """Handle returned by subscribe*(); call cancel() to stop delivery."""

    _ids = itertools.count(1)

    def __init__(self, bus: "EventBus", pattern: str):
        self.id = next(self._ids)
        self.pattern = pattern
        self._bus = bus
        self.active = True

    def cancel(self) -> None:
        if self.active:
            self.active = False
            self._bus._remove(self)

    def _deliver(self, event: Event) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cancel()


class _CallbackSubscription(Subscription):
    def __init__(self, bus: "EventBus", pattern: str, fn: Callback):
        super().__init__(bus, pattern)
        self._fn = fn

    def _deliver(self, event: Event) -> None:
        self._fn(event)


class QueueSubscription(Subscription):
    """Bounded queue of events. When full, the oldest event is dropped."""

    def __init__(self, bus: "EventBus", pattern: str, maxsize: int = 256):
        super().__init__(bus, pattern)
        self._dq: collections.deque[Event] = collections.deque(maxlen=maxsize)
        self._cv = threading.Condition()
        self.dropped = 0

    def _deliver(self, event: Event) -> None:
        with self._cv:
            if len(self._dq) == self._dq.maxlen:
                self.dropped += 1
            self._dq.append(event)
            self._cv.notify()

    def drain(self) -> list[Event]:
        with self._cv:
            items = list(self._dq)
            self._dq.clear()
            return items

    def get(self, timeout: float | None = None) -> Event | None:
        with self._cv:
            if not self._dq:
                self._cv.wait(timeout)
            return self._dq.popleft() if self._dq else None

    def __len__(self) -> int:
        return len(self._dq)


class _AsyncSubscription(Subscription):
    def __init__(self, bus: "EventBus", pattern: str, fn: Callable[[Event], Any], loop: asyncio.AbstractEventLoop):
        super().__init__(bus, pattern)
        self._fn = fn
        self._loop = loop

    def _deliver(self, event: Event) -> None:
        if self._loop.is_closed():
            return

        def _run() -> None:
            if not self.active:
                return
            try:
                result = self._fn(event)
                if asyncio.iscoroutine(result):
                    task = self._loop.create_task(result)
                    task.add_done_callback(_log_task_exception)
            except Exception:  # noqa: BLE001
                log.exception("async subscriber for %s failed", self.pattern)

        try:
            self._loop.call_soon_threadsafe(_run)
        except RuntimeError:
            pass  # loop shutting down


def _log_task_exception(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("event handler task failed", exc_info=task.exception())


class EventBus:
    def __init__(self, history: int = 300):
        self._subs: list[Subscription] = []
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._history: collections.deque[Event] = collections.deque(maxlen=history)

    # -- publishing -----------------------------------------------------
    def publish(self, topic: str, data: Mapping[str, Any] | None = None, source: str = "") -> Event:
        event = Event(topic=topic, data=dict(data or {}), source=source, seq=next(self._seq))
        with self._lock:
            self._history.append(event)
            targets = [s for s in self._subs if s.active and topic_matches(s.pattern, topic)]
        for sub in targets:
            try:
                sub._deliver(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break others
                log.exception("subscriber %s failed on %s", sub.pattern, topic)
        return event

    # -- subscribing ----------------------------------------------------
    def subscribe(self, pattern: str, fn: Callback) -> Subscription:
        return self._add(_CallbackSubscription(self, pattern, fn))

    def subscribe_queue(self, pattern: str, maxsize: int = 256) -> QueueSubscription:
        sub = QueueSubscription(self, pattern, maxsize)
        self._add(sub)
        return sub

    def subscribe_async(
        self, pattern: str, fn: Callable[[Event], Any], loop: asyncio.AbstractEventLoop | None = None
    ) -> Subscription:
        loop = loop or asyncio.get_running_loop()
        return self._add(_AsyncSubscription(self, pattern, fn, loop))

    async def wait_for(
        self,
        pattern: str,
        timeout: float | None = None,
        predicate: Callable[[Event], bool] | None = None,
    ) -> Event | None:
        """Await the next matching event (published from any thread). None on timeout."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Event] = loop.create_future()

        def _on(event: Event) -> None:
            if predicate is not None:
                try:
                    if not predicate(event):
                        return
                except Exception:  # noqa: BLE001
                    log.exception("wait_for predicate failed")
                    return

            def _set() -> None:
                if not fut.done():
                    fut.set_result(event)

            loop.call_soon_threadsafe(_set)

        sub = self.subscribe(pattern, _on)
        try:
            return await asyncio.wait_for(fut, timeout) if timeout is not None else await fut
        except asyncio.TimeoutError:
            return None
        finally:
            sub.cancel()

    def recent(self, n: int = 50, pattern: str = "*") -> list[Event]:
        with self._lock:
            items = [e for e in self._history if topic_matches(pattern, e.topic)]
        return items[-n:]

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # -- internals ------------------------------------------------------
    def _add(self, sub: Subscription) -> Subscription:
        with self._lock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def clear(self) -> None:
        with self._lock:
            for s in self._subs:
                s.active = False
            self._subs.clear()


def collect(bus: EventBus, patterns: Iterable[str] = ("*",)) -> list[Event]:
    """Test helper: returns a live list that records every matching event."""
    seen: list[Event] = []
    for p in patterns:
        bus.subscribe(p, seen.append)
    return seen


AsyncHandler = Callable[[Event], Awaitable[None]]
