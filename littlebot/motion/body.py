"""Body language: the conversation turned into gaze, nods and small gestures (the way Vector, Cozmo and
Reachy feel alive).

One small module, started by ``app/behaviours.py`` when ``motion.body_language`` is true. It only
listens to bus events and calls ``ctx.face`` (FaceAPI) and ``ctx.motion`` (MotionAPI):

=======================  ==========================================================================
conversation             what LittleBot does
=======================  ==========================================================================
listening / hearing      eyes look at the person (the tracked face, else the centre); ``perk_up``
                         (head lifts a little, arms twitch); the head keeps following the person
the person stopped       a small face nod ("got it")
thinking                 eyes glance up and to the side (again every few seconds); ``look_up_think``
speaking                 sentence ends: a face nod + ``nod_small``; a sentence's expression:
                         celebrating -> ``celebrate``, happy / excited -> ``arm_wiggle``,
                         curious -> ``tilt_curious``, sad -> ``droop``, confused -> ``head_shake``,
                         surprised -> ``perk_up``; a final nod when the answer ends
a card is shown          ``point_screen`` (an arm points at the screen)
interrupted              stop (back to neutral, smoothly) and look at the person
session ends             neutral pose, the eyes go back to their own wandering
idle                     the head follows a detected face (eyes lead, head follows: look_at);
                         now and then a tiny ``idle_shift``
=======================  ==========================================================================

Inputs: ``assistant.phase`` / ``assistant.sentence`` / ``assistant.heard`` / ``assistant.interrupted`` /
``assistant.session`` (the new pipeline, littlebot/assistant/events.py), ``state.changed`` (LISTENING /
THINKING / SPEAKING: the live voice and the older conversation engine), ``live.call_ended``,
``screen.card_shown``, ``vision.faces``, ``motion.estop``, ``system.stop_all``, ``game.started`` /
``game.finished``.

Rules
-----
* Face-only parts (glances, face nods) work with no motion at all: that is what makes the robot feel
  alive today. Expressions themselves are not set here (the sentence's expression is the face's job).
* At most one gesture per ``motion.body_language_gap_seconds`` (1.5 s), and one face nod per the same.
* Movements run at ``Priority.AMBIENT``: the motion controller holds the MOTION lease at that priority
  for them, so an activity, a game, a video or a parent's command overrides them at once, and a lease
  held by anyone else means body language does not move at all.
* Nothing moves during an emergency stop (no gesture, no look_at); the face still reacts.
* Quiet while LittleBot sleeps, plays a video / an activity, shows an error or runs a game.
* No motion (``ctx.motion`` None): the motion parts cost nothing (no calls, no tasks).

Threading: everything except ``vision.faces`` runs on the runtime loop (``subscribe_async``);
``vision.faces`` (10 Hz) is handled in the vision thread: it only stores the face and calls the
thread-safe ``MotionAPI.look_at``.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import time
from typing import Any, Callable

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Event, Subscription, Topics

log = logging.getLogger("littlebot.motion.body")

PRIORITY = int(Priority.AMBIENT)
LEASE_OWNERS_OK = frozenset({"motion"})  # the motion controller's own lease (it hosts our moves)

# topics owned by other subsystems, named as they publish them
PHASE = "assistant.phase"  # littlebot/assistant/events.py
SENTENCE = "assistant.sentence"
HEARD = "assistant.heard"
INTERRUPTED = "assistant.interrupted"
SESSION = "assistant.session"
LIVE_CALL_ENDED = "live.call_ended"  # littlebot/chatgpt/live.py
CARD_SHOWN = "screen.card_shown"  # littlebot/screens/service.py
GAME_STARTED = "game.started"  # littlebot/games/service.py
GAME_FINISHED = "game.finished"

IDLE, ATTENDING, THINKING, SPEAKING = "idle", "attending", "thinking", "speaking"
_PHASE_MODES = {"listening": ATTENDING, "hearing": ATTENDING, "thinking": THINKING, "speaking": SPEAKING,
                "idle": IDLE}
_STATE_MODES = {"LISTENING": ATTENDING, "THINKING": THINKING, "SPEAKING": SPEAKING, "IDLE": IDLE}
_QUIET_STATES = frozenset({"SLEEPING", "PLAYING_MEDIA", "PLAYING_ACTIVITY", "ERROR"})

EXPRESSION_GESTURES: dict[str, str] = {
    "celebrating": "celebrate",
    "happy": "arm_wiggle",
    "excited": "arm_wiggle",
    "curious": "tilt_curious",
    "sad": "droop",
    "confused": "head_shake",
    "surprised": "perk_up",
}

CENTRE = (0.5, 0.45)  # where "the person" is when no face is tracked (a little above the middle)
FACE_FRESH_S = 1.5  # a tracked face older than this is not "the person" any more
LOOK_HOLD_S = 1.5  # explicit glance at the person
THINK_GLANCE = (0.22, 0.24)  # up-and-aside: x offset from the centre, y
THINK_HOLD_S = 1.2
THINK_REGLANCE_S = 3.0  # a long think glances again (the other way)
PERK_EVERY_S = 8.0  # perk_up when listening starts, not again within this long (follow-up turns)
IDLE_JITTER = (0.6, 1.4)


class BodyLanguage:
    name = "body_language"

    def __init__(self, ctx: Any, *, clock: Callable[[], float] | None = None, rng: random.Random | None = None,
                 poll_seconds: float = 1.0) -> None:
        self.ctx = ctx
        cfg = ctx.config.motion
        self._now = clock or time.monotonic
        self._rng = rng or random.Random()
        self.gap = max(0.0, float(cfg.body_language_gap_seconds))
        self.idle_every = max(0.0, float(cfg.idle_motion_seconds))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._subs: list[Subscription] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._poll_task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.mode = IDLE
        self._mode_since = self._now()
        self._state = str(getattr(getattr(ctx.state, "state", None), "value", "IDLE"))
        self._estop = False
        self._game = False
        self._session = False
        self._face: tuple[float, float, float] | None = None  # x, y, seen at
        self._last_gesture = -1e9
        self._last_face_nod = -1e9
        self._last_perk = -1e9
        self._think_glance_at = -1e9
        self._think_side = 1.0
        self._spoke_last = False  # the answer's last sentence started
        self._interrupted = False
        self._next_idle = self._now() + self._idle_interval()
        self.gestures_run: collections.deque[str] = collections.deque(maxlen=50)  # the latest, for status / tests
        self.gesture_count = 0
        self.face_nods = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._subs:
            return
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        topics = (PHASE, SENTENCE, HEARD, INTERRUPTED, SESSION, LIVE_CALL_ENDED, CARD_SHOWN, GAME_STARTED,
                  GAME_FINISHED, Topics.STATE_CHANGED, Topics.MOTION_ESTOP, Topics.STOP_ALL)
        self._subs = [bus.subscribe_async(t, self.handle, self._loop) for t in topics]
        self._subs.append(bus.subscribe(Topics.VISION_FACES, self._on_faces))  # vision thread: tiny
        self._estop = bool(self._motion_status().get("estop", False))
        if self._has_motion() or self.ctx.face is not None:
            self._poll_task = self._loop.create_task(self._poll_loop(), name="body-language")

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        tasks = [t for t in (self._poll_task, *self._tasks) if t is not None and not t.done()]
        self._poll_task = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "motion": self._has_motion(), "estop": self._estop, "game": self._game,
                "session": self._session, "gestures": self.gesture_count,
                "last_gesture": self.gestures_run[-1] if self.gestures_run else None, "face_nods": self.face_nods}

    # ------------------------------------------------------------------ events
    def handle(self, event: Event) -> None:
        """One bus event (on the runtime loop). Public so tests can drive it directly."""
        topic = event.topic
        try:
            if topic == PHASE:
                self._set_mode(_PHASE_MODES.get(str(event.get("phase") or ""), self.mode))
            elif topic == Topics.STATE_CHANGED:
                self._state = str(event.get("new") or self._state)
                mode = _STATE_MODES.get(self._state)
                if mode is not None:
                    self._set_mode(mode)
                elif self._state in _QUIET_STATES:
                    self._set_mode(IDLE, quiet=True)
            elif topic == SENTENCE:
                self._on_sentence(event)
            elif topic == HEARD:
                self._face_nod()
            elif topic == INTERRUPTED:
                self._on_interrupted()
            elif topic in (SESSION, LIVE_CALL_ENDED):
                active = bool(event.get("active", False)) if topic == SESSION else False
                if active:
                    self._session = True
                else:
                    self._on_session_end()
            elif topic == CARD_SHOWN:
                if self._can_act():
                    self._gesture("point_screen")
            elif topic == Topics.MOTION_ESTOP:
                self._estop = bool(event.get("active", False))
                if self._estop:
                    self._cancel_tasks()
            elif topic == Topics.STOP_ALL:
                self._cancel_tasks()
                self.mode, self._mode_since = IDLE, self._now()
                self._last_gesture = self._now()  # calm: nothing new for a moment
            elif topic == GAME_STARTED:
                self._game = True
            elif topic == GAME_FINISHED:
                self._game = False
        except Exception:  # noqa: BLE001 - body language must never break anything
            log.exception("body language failed on %s", topic)

    def _set_mode(self, mode: str, *, quiet: bool = False) -> None:
        if mode == self.mode:
            return
        old, self.mode, self._mode_since = self.mode, mode, self._now()
        if old == SPEAKING:
            if self._spoke_last and not self._interrupted and not quiet:
                self._nod()  # the answer ended: a small closing nod
            self._spoke_last = False
        if quiet or not self._can_act():
            return
        if mode == ATTENDING:
            self._interrupted = False
            self._look_at_person()
            if self._now() - self._last_perk >= PERK_EVERY_S and self._gesture("perk_up"):
                self._last_perk = self._now()
        elif mode == THINKING:
            self._think_glance()
            self._gesture("look_up_think")
        elif mode == SPEAKING:
            self._interrupted = False
            self._look_at_person(hold=1.0)
        elif mode == IDLE:
            self._next_idle = self._now() + self._idle_interval()

    def _on_sentence(self, event: Event) -> None:
        if self.mode != SPEAKING:  # a sentence means LittleBot is talking, whatever came before
            self._set_mode(SPEAKING)
        if bool(event.get("last", False)):
            self._spoke_last = True
        if not self._can_act():
            return
        expression = str(event.get("expression") or "")
        gesture = EXPRESSION_GESTURES.get(expression)
        if gesture is not None and self._gesture(gesture):
            return
        try:
            index = int(event.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        if index > 0:  # the previous sentence just ended
            self._nod()

    def _on_interrupted(self) -> None:
        self._interrupted = True
        self._spoke_last = False
        if not self._can_act():
            return
        if self._motion_ok():
            self._cancel_tasks()
            self._spawn("neutral", self.ctx.motion.neutral(priority=PRIORITY))  # stop, smoothly back to rest
            self._last_gesture = self._now()
        self._look_at_person()

    def _on_session_end(self) -> None:
        was = self._session
        self._session = False
        self._spoke_last = False
        self.mode, self._mode_since = IDLE, self._now()
        face = self.ctx.face
        if face is not None and self._face_ok():
            face.clear_gaze()
        if self._motion_ok() and (was or self.gesture_count):
            self._spawn("neutral", self.ctx.motion.neutral(priority=PRIORITY))
        self._next_idle = self._now() + self._idle_interval()

    def _on_faces(self, event: Event) -> None:
        """vision.faces (vision thread, ~10 Hz): remember the person; the head follows them."""
        faces = event.get("faces") or []
        if not faces:
            return
        try:
            f = next((f for f in faces if f.get("primary")), None) or max(
                faces, key=lambda f: f.get("w", 0) * f.get("h", 0))
            x, y = float(f["gaze_x"]), float(f["gaze_y"])
        except (KeyError, TypeError, ValueError):
            return
        self._face = (x, y, self._now())
        if self.mode == THINKING or not self._motion_ok() or not self._can_act():
            return
        settings = getattr(self.ctx, "settings", None)
        if settings is not None and not settings.get("vision.follow_faces", True):
            return
        look = getattr(self.ctx.motion, "look_at", None)
        if callable(look):
            look(x, y, priority=PRIORITY)

    # ------------------------------------------------------------------ time
    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("body language poll failed")

    def poll(self, now: float | None = None) -> None:
        """Time-based behaviour: a long think glances again; idle motion now and then."""
        t = self._now() if now is None else now
        if not self._can_act():
            return
        if self.mode == THINKING and t - self._think_glance_at >= THINK_REGLANCE_S:
            self._think_glance()
        elif (self.mode == IDLE and self.idle_every > 0 and not self._session and t >= self._next_idle
              and self._state == "IDLE"):
            self._next_idle = t + self._idle_interval()
            if not _dozing(self.ctx.face):
                self._gesture("idle_shift")

    def _idle_interval(self) -> float:
        return self.idle_every * self._rng.uniform(*IDLE_JITTER) if self.idle_every > 0 else float("inf")

    # ------------------------------------------------------------------ actions
    def _can_act(self) -> bool:
        return not self._game and self._state not in _QUIET_STATES

    def _face_ok(self) -> bool:
        return self.ctx.face is not None and self._can_act()

    def _has_motion(self) -> bool:
        return self.ctx.motion is not None

    def _motion_ok(self) -> bool:
        if self.ctx.motion is None or self._estop:
            return False
        holder = self.ctx.arbiter.holder(Resource.MOTION) if getattr(self.ctx, "arbiter", None) else None
        return holder is None or holder.owner in LEASE_OWNERS_OK or holder.priority <= PRIORITY

    def _motion_status(self) -> dict[str, Any]:
        motion = self.ctx.motion
        if motion is None:
            return {}
        try:
            st = motion.status()
        except Exception:  # noqa: BLE001
            return {}
        return st if isinstance(st, dict) else {}

    def _gesture(self, name: str) -> bool:
        """Start a gesture (rate limited). True if one was started."""
        if not self._motion_ok() or not self._can_act():
            return False
        now = self._now()
        if now - self._last_gesture < self.gap:
            return False
        motion = self.ctx.motion
        try:
            if name not in motion.gestures():
                return False
        except Exception:  # noqa: BLE001
            return False
        self._last_gesture = now
        self.gestures_run.append(name)
        self.gesture_count += 1
        self._spawn(name, motion.run_gesture(name, priority=PRIORITY))
        return True

    def _nod(self) -> None:
        """A sentence ended: a face nod (works without servos) and a small head nod (rate limited)."""
        self._face_nod()
        self._gesture("nod_small")

    def _face_nod(self) -> None:
        face = self.ctx.face
        if face is None or not self._face_ok():
            return
        now = self._now()
        if now - self._last_face_nod < self.gap:
            return
        self._last_face_nod = now
        self.face_nods += 1
        face.play_animation("nod")

    def _person(self) -> tuple[float, float]:
        f = self._face
        if f is not None and self._now() - f[2] <= FACE_FRESH_S:
            return f[0], f[1]
        return CENTRE

    def _look_at_person(self, hold: float = LOOK_HOLD_S) -> None:
        x, y = self._person()
        face = self.ctx.face
        if face is not None and self._face_ok():
            face.look_at(x, y, hold=hold)
        if self._motion_ok():
            look = getattr(self.ctx.motion, "look_at", None)
            if callable(look):
                look(x, y, priority=PRIORITY)

    def _think_glance(self) -> None:
        self._think_glance_at = self._now()
        face = self.ctx.face
        if face is None or not self._face_ok():
            return
        dx, y = THINK_GLANCE
        face.look_at(0.5 + self._think_side * dx, y, hold=THINK_HOLD_S)
        self._think_side = -self._think_side

    def _spawn(self, label: str, coro: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        task = loop.create_task(coro, name=f"body-{label}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("body language movement failed: %s", task.exception())

    def _cancel_tasks(self) -> None:
        """Stop our own movements (the controller brakes them smoothly); others' motion is untouched."""
        for t in list(self._tasks):
            t.cancel()


def _dozing(face: Any) -> bool:
    return getattr(face, "dozing", False) is True


__all__ = ["BodyLanguage", "EXPRESSION_GESTURES", "PRIORITY"]
