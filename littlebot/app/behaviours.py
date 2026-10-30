"""Cross-module glue: how LittleBot reacts to what happens around it.

Keeps reactive rules in one place instead of scattering them through modules:

* RobotState changes        -> face state (listening / thinking / sleeping ...)
* person appears / leaves   -> curious/happy greeting glance, gaze tracking
* detected faces            -> eyes follow the nearest face (if enabled)
* speech start / finish     -> speaking animation (handled by face) + state
* long idle                 -> sleepy, then SLEEPING; a person or parent wakes it
  (the shorter "dozing" look while nothing happens is the face's own: littlebot/face/doze.py)
* parent STOP               -> everything calms down
* the conversation          -> gaze, nods and small gestures: littlebot/motion/body.py
                               (``motion.body_language``; the face parts work without servos)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.runtime import cancel_and_wait
from littlebot.core.state import FACE_STATE_FOR, RobotState

log = logging.getLogger("littlebot.behaviours")


class Behaviours:
    name = "behaviours"

    def __init__(self, ctx: RobotContext) -> None:
        self.ctx = ctx
        self._subs: list[Subscription] = []
        self._idle_task: asyncio.Task[Any] | None = None
        self._last_activity = time.monotonic()
        self._greeted_at = 0.0
        self.body: Any = None
        if ctx.config.motion.body_language:
            from littlebot.motion.body import BodyLanguage

            self.body = BodyLanguage(ctx)

    async def start(self) -> None:
        bus = self.ctx.bus
        self._subs = [
            bus.subscribe(Topics.STATE_CHANGED, self._on_state),
            bus.subscribe(Topics.VISION_FACES, self._on_faces),
            bus.subscribe(Topics.VISION_PERSON_PRESENT, self._on_person_present),
            bus.subscribe(Topics.VISION_PERSON_LEFT, self._on_person_left),
            bus.subscribe("activity.*", self._touch),
            bus.subscribe("media.*", self._touch),
            bus.subscribe("screen.*", self._touch),  # info cards (a running timer keeps LittleBot awake)
            bus.subscribe(Topics.AUDIO_UTTERANCE, self._touch),
            bus.subscribe(Topics.CAREGIVER_RESPONSE, self._touch),
        ]
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_watch())
        face = self.ctx.face
        if face is not None:
            face.set_state(FACE_STATE_FOR[self.ctx.state.state])
        if self.body is not None:
            await self.body.start()

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        if self.body is not None:
            await self.body.stop()
        await cancel_and_wait(self._idle_task)

    # ---------------------------------------------------------------- events
    def _touch(self, _event: Event | None = None) -> None:
        self._last_activity = time.monotonic()

    def _on_state(self, event: Event) -> None:
        self._touch()
        face = self.ctx.face
        if face is None:
            return
        face.set_state(str(event.get("face_state", "idle")))
        new = event.get("new")
        if new == RobotState.SLEEPING.value:
            face.set_expression("sleepy")
        elif event.get("old") == RobotState.SLEEPING.value:
            face.open_eyes()
            face.set_expression("happy", hold=2.0)
            face.play_animation("bounce")

    def _on_faces(self, event: Event) -> None:
        face = self.ctx.face
        if face is None or not self.ctx.settings.get("vision.follow_faces", True):
            return
        if self.ctx.state.is_(RobotState.PLAYING_MEDIA, RobotState.SLEEPING):
            return
        faces = event.get("faces") or []
        if not faces:
            return
        # Vision marks the face it is tracking (stable across similar-sized faces, smoothed gaze);
        # fall back to the biggest (closest) box for producers that don't mark one.
        biggest = next((f for f in faces if f.get("primary")), None) or max(
            faces, key=lambda f: f.get("w", 0) * f.get("h", 0))
        # Tracking gaze is a "soft" gaze: explicit look_at() calls still win.
        tracker = getattr(face, "track_gaze", None)
        if callable(tracker):
            tracker(biggest["gaze_x"], biggest["gaze_y"])
        else:
            face.look_at(biggest["gaze_x"], biggest["gaze_y"], hold=0.5)

    def _on_person_present(self, event: Event) -> None:
        self._touch()
        face = self.ctx.face
        state = self.ctx.state
        if face is None:
            return
        if state.is_(RobotState.SLEEPING):
            state.transition(RobotState.IDLE, "person arrived")
            return
        if state.is_(RobotState.IDLE) and time.monotonic() - self._greeted_at > 20:
            self._greeted_at = time.monotonic()
            face.set_expression("happy", hold=2.5)
            if not _dozing(face):  # a dozing face wakes with its own little stretch, not a jump
                face.play_animation("bounce")

    def _on_person_left(self, _event: Event) -> None:
        face = self.ctx.face
        if face is not None and self.ctx.state.is_(RobotState.IDLE):
            if not _dozing(face):  # someone leaving doesn't wake a dozing face
                face.set_expression("curious", hold=2.0)
            face.clear_gaze()

    async def _idle_watch(self) -> None:
        """Get sleepy after a long quiet period (configurable, 0 disables)."""
        sleepy_shown = False
        while True:
            await asyncio.sleep(5)
            minutes = float(self.ctx.settings.get("system.sleep_after_idle_minutes", 10) or 0)
            if minutes <= 0 or not self.ctx.state.is_(RobotState.IDLE):
                sleepy_shown = False
                continue
            quiet = time.monotonic() - self._last_activity
            if quiet > minutes * 60:
                self.ctx.state.transition(RobotState.SLEEPING, "idle timeout")
                sleepy_shown = False
            elif quiet > minutes * 60 * 0.8 and not sleepy_shown and self.ctx.face is not None:
                if not _dozing(self.ctx.face):  # (a dozing face looks sleepy already)
                    self.ctx.face.set_expression("sleepy", hold=4.0)
                sleepy_shown = True


def _dozing(face: Any) -> bool:
    """True while the idle face dozes (littlebot/face/doze.py; fakes without it never doze)."""
    return getattr(face, "dozing", False) is True
