"""Demo sequence coverage and simulator panel robustness."""

from __future__ import annotations

import inspect

import pytest

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES, FaceAPI
from littlebot.face.demo import DEMO_STEPS, DemoSequence, DemoStep
from littlebot.testing import FakeFace


def test_demo_covers_everything():
    exprs = {s.args[0] for s in DEMO_STEPS if s.method == "set_expression"}
    anims = {s.args[0] for s in DEMO_STEPS if s.method == "play_animation"}
    states = {s.args[0] for s in DEMO_STEPS if s.method == "set_state"}
    # expressions reached via a state default count too
    exprs |= {"thinking", "listening", "speaking"} & {s for s in states}
    assert set(EXPRESSIONS) <= exprs | {"sleepy"}
    assert set(FACE_ANIMATIONS) <= anims
    assert set(FACE_STATES) <= states
    methods = {m for m, _ in inspect.getmembers(FaceAPI) if not m.startswith("_")}
    assert {s.method for s in DEMO_STEPS} <= methods
    assert any(s.method == "look_at" for s in DEMO_STEPS) and any(s.method == "blink" for s in DEMO_STEPS)


def test_demo_runs_and_loops():
    face = FakeFace()
    demo = DemoSequence(face, loop=True)
    total = demo.total_seconds
    assert 40 < total < 120
    t = 0.0
    while t < total * 2 + 1:
        demo.update(0.1)
        t += 0.1
    assert demo.loops == 2
    called = {c[0] for c in face.calls}
    assert {"set_expression", "play_animation", "set_state", "look_at", "blink", "close_eyes", "open_eyes"} <= called
    assert demo.caption


def test_demo_once_and_errors_do_not_escape():
    class Boom:
        def set_state(self, *_a, **_k):
            raise RuntimeError("nope")

    demo = DemoSequence(Boom(), loop=False, steps=(DemoStep(0.0, "set_state", ("idle",)),
                                                  DemoStep(0.1, "missing_method")))
    demo.update(1.0)
    assert demo.done
    demo.update(1.0)
    demo.reset()
    assert not demo.done and demo.index == 0


def test_arm_rotation_handles_mirrored_servos():
    from littlebot.face.sim_panel import arm_rotation

    left = {"angle": 20, "min": 10, "max": 150, "neutral": 20}
    right = {"angle": 160, "min": 30, "max": 170, "neutral": 160}
    assert arm_rotation(left) == pytest.approx(20)
    assert arm_rotation(right) == pytest.approx(20)  # same pose, mirrored servo
    assert arm_rotation({**right, "angle": 30}) == pytest.approx(150)
    assert arm_rotation({"angle": "bad"}) == pytest.approx(90)


def test_panel_draws_with_partial_or_odd_data(pygame_display):
    pygame = pygame_display
    from littlebot.face.sim_panel import SimPanel

    panel = SimPanel()
    surf = pygame.Surface((300, 460))
    rect = surf.get_rect()
    panel.draw(surf, rect, None, None)
    panel.draw(surf, rect, {"state": "weird", "face": None, "system": {"cpu_percent": "x"}}, {"joints": "nope"})
    full = {
        "name": "LittleBot", "mode": "simulation",
        "state": {"state": "PLAYING_ACTIVITY", "since_seconds": 12.3},
        "face": {"expression": "happy", "state": "idle"},
        "activities": {"running": True, "name": "Peekaboo", "step_index": 1, "total_steps": 5, "paused": True},
        "media": {"state": "playing"},
        "vision": {"camera": "ok", "backend": "mock", "faces": [{}]},
        "audio": {"speaker": {"status": "ok"}, "microphone": {"status": "disabled"}, "listening": True},
        "brain": {"provider": "mock"},
        "system": {"cpu_percent": 12.5, "memory_mb": 140.2, "temperature_c": 51.0},
    }
    motion = {"estop": True, "moving": True, "physical_enabled": False,
              "joints": {"head": {"angle": 120, "target": 100, "min": 50, "max": 130, "neutral": 90},
                         "left_arm": {"angle": 150, "target": 150, "min": 10, "max": 150, "neutral": 20}}}
    panel.draw(surf, rect, full, motion, fps=29.7, caption="demo: happy")
    assert panel.estop_rect is not None and rect.contains(panel.estop_rect)
    # E-STOP pill is drawn red when active
    c = surf.get_at((panel.estop_rect.x + 30, panel.estop_rect.centery))
    assert c.r > 150 and c.g < 120
