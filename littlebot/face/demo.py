"""A polished demo sequence that cycles every expression, state, animation and gaze.

Pure logic: :class:`DemoSequence` calls ``FaceAPI`` methods on whatever face it
is given as time advances, so it works with the real controller, a fake, or a
test recorder. Used by the display's ``M`` key, ``--demo`` and face_preview.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("littlebot.face.demo")


@dataclass(frozen=True)
class DemoStep:
    wait: float  # seconds to wait *before* running this step
    method: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    caption: str | None = None  # shown in the simulator while this part plays


def _s(wait: float, method: str, *args: Any, caption: str | None = None, **kwargs: Any) -> DemoStep:
    return DemoStep(wait, method, args, kwargs, caption)


DEMO_STEPS: tuple[DemoStep, ...] = (
    _s(0.0, "set_state", "idle", caption="hello!"),
    _s(0.0, "set_expression", "neutral"),
    _s(0.8, "look_at", 0.12, 0.45, hold=1.0, caption="looking around"),
    _s(1.0, "look_at", 0.88, 0.45, hold=1.0),
    _s(1.0, "look_at", 0.5, 0.12, hold=0.9),
    _s(0.9, "look_at", 0.5, 0.9, hold=0.9),
    _s(0.9, "clear_gaze"),
    _s(0.4, "blink", caption="blink"),
    _s(0.9, "blink", double=True),
    _s(1.0, "set_expression", "happy", caption="happy"),
    _s(0.5, "play_animation", "bounce"),
    _s(1.6, "set_expression", "excited", caption="excited"),
    _s(0.3, "play_animation", "sparkle"),
    _s(2.0, "set_expression", "curious", caption="curious"),
    _s(0.4, "look_at", 0.8, 0.35, hold=1.4),
    _s(1.8, "set_expression", "surprised", caption="surprised"),
    _s(1.8, "close_eyes", caption="peek-a-boo"),
    _s(1.4, "open_eyes"),
    _s(0.0, "play_animation", "peek"),
    _s(0.0, "set_expression", "happy"),
    _s(1.6, "set_expression", "sleepy", caption="sleepy"),
    _s(2.4, "set_expression", "sad", caption="sad"),
    _s(2.2, "set_state", "thinking", caption="thinking"),
    _s(2.6, "set_state", "listening", caption="listening"),
    _s(2.6, "set_state", "speaking", caption="speaking"),
    _s(2.8, "set_state", "idle"),
    _s(0.0, "set_expression", "confused", caption="confused"),
    _s(0.6, "play_animation", "shake"),
    _s(1.8, "set_expression", "playful", caption="playful"),
    _s(0.4, "play_animation", "wiggle"),
    _s(1.8, "set_expression", "affectionate", caption="affectionate"),
    _s(0.3, "play_animation", "hearts"),
    _s(2.4, "set_expression", "celebrating", caption="celebrating"),
    _s(0.2, "play_animation", "confetti"),
    _s(2.8, "set_expression", "happy", caption="nod & spin"),
    _s(0.2, "play_animation", "nod"),
    _s(1.2, "play_animation", "spin_eyes"),
    _s(1.8, "set_state", "sleeping", caption="sleeping"),
    _s(0.6, "play_animation", "zzz"),
    _s(3.2, "set_state", "idle", caption="good morning!"),
    _s(0.0, "set_expression", "happy", hold=1.5),
    _s(0.0, "play_animation", "bounce"),
    _s(2.0, "set_state", "error", caption="oops (error state)"),
    _s(2.4, "set_state", "hidden", caption="video playing"),
    _s(2.4, "set_state", "idle"),
    _s(0.0, "set_expression", "neutral"),
    _s(1.5, "clear_gaze"),
)


class DemoSequence:
    """Steps through :data:`DEMO_STEPS` as ``update(dt)`` is called."""

    def __init__(self, face: Any, *, loop: bool = True, steps: tuple[DemoStep, ...] = DEMO_STEPS) -> None:
        self.face = face
        self.loop = loop
        self.steps = steps
        self.index = 0
        self.elapsed = 0.0
        self.caption = ""
        self.done = False
        self.loops = 0

    @property
    def total_seconds(self) -> float:
        return sum(s.wait for s in self.steps)

    def reset(self) -> None:
        self.index, self.elapsed, self.done, self.caption = 0, 0.0, False, ""

    def update(self, dt: float) -> None:
        if self.done or not self.steps:
            return
        self.elapsed += max(0.0, dt)
        while not self.done and self.elapsed >= self.steps[self.index].wait:
            step = self.steps[self.index]
            self.elapsed -= step.wait
            self._run(step)
            self.index += 1
            if self.index >= len(self.steps):
                self.loops += 1
                if self.loop:
                    self.index = 0
                else:
                    self.done = True

    def _run(self, step: DemoStep) -> None:
        if step.caption is not None:
            self.caption = step.caption
        fn = getattr(self.face, step.method, None)
        if fn is None:
            return
        try:
            fn(*step.args, **step.kwargs)
        except Exception:  # noqa: BLE001 - the demo must never crash the display
            log.exception("demo step %s failed", step.method)
