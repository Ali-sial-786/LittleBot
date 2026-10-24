#!/usr/bin/env python3
"""Standalone preview of LittleBot's animated face (no audio / camera / AI needed).

    .venv/bin/python scripts/face_preview.py                 # window, simulator panel, press H for keys
    .venv/bin/python scripts/face_preview.py --demo          # loop the demo sequence
    .venv/bin/python scripts/face_preview.py --scale 1 --no-panel
    .venv/bin/python scripts/face_preview.py --fullscreen
    .venv/bin/python scripts/face_preview.py --screenshot-dir artifacts/face   # render PNGs and exit

The preview builds a minimal in-memory context (``littlebot.testing.make_test_context``),
a real ``FaceController`` and a tiny stub robot, then runs the same
``run_display`` loop the robot uses. A small simulated motion stub makes the
side panel's arms move (W = wave).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


# --------------------------------------------------------------------------- stubs
class PreviewMotion:
    """Just enough of MotionAPI to animate the simulator panel's arms."""

    def __init__(self, servos: dict[str, Any]) -> None:
        self._joints = {
            name: {"angle": float(s.neutral), "target": float(s.neutral), "min": float(s.min_angle),
                   "max": float(s.max_angle), "neutral": float(s.neutral)}
            for name, s in servos.items()
        }
        self.estop = False
        self.gesture: str | None = None
        self._cancel = False

    def gestures(self) -> list[str]:
        return ["wave", "look_left", "look_right", "raise_arms", "neutral"]

    async def run_gesture(self, name: str) -> bool:
        if self.estop or name not in self.gestures():
            return False
        self.gesture, self._cancel = name, False
        j = self._joints
        try:
            if name == "wave":
                await self._move({"right_arm": 45})
                for _ in range(3):
                    await self._move({"right_arm": 70}, 0.25)
                    await self._move({"right_arm": 40}, 0.25)
            elif name == "raise_arms":
                await self._move({"left_arm": 140, "right_arm": 40})
            elif name == "look_left":
                await self._move({"head": 120})
            elif name == "look_right":
                await self._move({"head": 60})
            await self._move({k: v["neutral"] for k, v in j.items()})
            return not self._cancel
        finally:
            self.gesture = None

    async def _move(self, targets: dict[str, float], seconds: float = 0.5) -> None:
        start = {k: self._joints[k]["angle"] for k in targets}
        for k, v in targets.items():
            self._joints[k]["target"] = float(v)
        steps = max(1, int(seconds * 30))
        for i in range(1, steps + 1):
            if self._cancel or self.estop:
                return
            u = i / steps
            u = u * u * (3 - 2 * u)
            for k, v in targets.items():
                self._joints[k]["angle"] = start[k] + (v - start[k]) * u
            await asyncio.sleep(seconds / steps)

    async def move_joint(self, joint: str, angle: float, *, speed_dps: float | None = None) -> bool:
        await self._move({joint: angle})
        return not self.estop

    async def neutral(self) -> bool:
        await self._move({k: v["neutral"] for k, v in self._joints.items()})
        return not self.estop

    def cancel(self) -> None:
        self._cancel = True

    def emergency_stop(self, source: str = "unknown") -> None:
        self.estop = True
        self._cancel = True

    def reset_emergency_stop(self) -> bool:
        self.estop = False
        return True

    def status(self) -> dict[str, Any]:
        return {"backend": "preview", "physical_enabled": False, "armed": False, "estop": self.estop,
                "moving": self.gesture is not None, "gesture": self.gesture,
                "joints": {k: dict(v) for k, v in self._joints.items()}}


class PreviewRobot:
    """Minimal stand-in for app.robot.LittleBot used by run_display."""

    def __init__(self, ctx: Any) -> None:
        from littlebot.core.runtime import AsyncRuntime

        self.ctx = ctx
        self.runtime = AsyncRuntime("face-preview")
        ctx.runtime = self.runtime
        self.shutdown_event = threading.Event()

    def status(self) -> dict[str, Any]:
        ctx = self.ctx
        out: dict[str, Any] = {
            "name": ctx.config.robot_name, "mode": "face preview", "state": ctx.state.snapshot(),
            "leases": ctx.arbiter.snapshot(), "system": ctx.monitor.status(),
        }
        for attr in ("face", "audio", "vision", "motion", "media", "brain", "activities"):
            svc = getattr(ctx, attr)
            try:
                out[attr] = svc.status() if svc is not None else {"status": "unavailable"}
            except Exception as e:  # noqa: BLE001
                out[attr] = {"status": f"error: {e}"}
        return out

    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        face = self.ctx.face
        if self.ctx.motion is not None:
            self.ctx.motion.cancel()
        face.set_state("idle")
        face.open_eyes()
        face.set_expression("neutral")
        return self.status()

    def request_shutdown(self, reason: str = "requested") -> None:
        logging.getLogger("face_preview").info("shutdown requested: %s", reason)
        self.shutdown_event.set()


def build(args: argparse.Namespace) -> tuple[Any, Any]:
    from littlebot.face.controller import FaceController
    from littlebot.hardware.system import SystemMonitor
    from littlebot.testing import make_test_context

    display: dict[str, Any] = {"headless": False, "show_sim_panel": not args.no_panel, "show_debug": args.debug}
    if args.scale is not None:
        display["scale"] = args.scale
    if args.fullscreen:
        display["fullscreen"] = True
    ctx = make_test_context(fakes=False, display=display, logging={"level": "INFO"})
    try:
        ctx.monitor = SystemMonitor()  # real CPU / memory numbers in the panel
    except Exception:  # noqa: BLE001
        pass
    ctx.face = FaceController(ctx, seed=args.seed)
    ctx.motion = PreviewMotion(ctx.config.motion.servos)  # type: ignore[assignment]
    return ctx, PreviewRobot(ctx)


# --------------------------------------------------------------------------- screenshots
def _settle(face: Any, seconds: float = 1.2, step: float = 1 / 30) -> None:
    for _ in range(int(seconds / step)):
        face.update(step)


def screenshots(out_dir: Path, seed: int) -> list[Path]:
    """Render every expression, state, a few animation frames and the simulator window to PNGs."""
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame

    from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS
    from littlebot.face.controller import FaceController
    from littlebot.face.effects import ANIMATION_DURATIONS
    from littlebot.testing import make_test_context

    pygame.display.init()
    pygame.font.init()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    font = pygame.font.Font(None, 22)

    def fresh() -> Any:
        ctx = make_test_context(fakes=False)
        face = FaceController(ctx, seed=seed)
        face.animator.next_blink = 1e9  # no random blink in the stills
        face.animator.cfg = face.animator.cfg.model_copy(update={"idle_gaze": False})
        return face

    def shot(face: Any, name: str) -> pygame.Surface:
        surf = pygame.Surface((480, 320))
        face.render(surf)
        p = out_dir / f"{name}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surf, str(p))
        saved.append(p)
        return surf

    def sheet(tiles: list[tuple[str, pygame.Surface]], name: str, cols: int = 4) -> None:
        rows = (len(tiles) + cols - 1) // cols
        s = pygame.Surface((cols * 484 - 4, rows * 344))
        s.fill((36, 38, 46))
        for i, (label, img) in enumerate(tiles):
            x, y = (i % cols) * 484, (i // cols) * 344
            s.blit(img, (x, y))
            s.blit(font.render(label, True, (235, 235, 240)), (x + 6, y + 324))
        p = out_dir / f"_{name}.png"
        pygame.image.save(s, str(p))
        saved.append(p)

    tiles = []
    for e in EXPRESSIONS:
        face = fresh()
        face.set_expression(e)
        _settle(face)
        tiles.append((e, shot(face, f"expressions/{e}")))
    sheet(tiles, "expressions")

    tiles = []
    for st in ("idle", "listening", "thinking", "speaking", "sleeping", "error", "hidden"):
        face = fresh()
        face.set_state(st)
        _settle(face, 1.5)
        tiles.append((f"state: {st}", shot(face, f"states/{st}")))
    face = fresh()
    face.close_eyes()
    _settle(face)
    tiles.append(("close_eyes (peekaboo)", shot(face, "states/eyes_closed")))
    # dozing: the idle face's look when nothing is happening (littlebot/face/doze.py), and waking up
    face = fresh()
    face.animator.set_doze(True)
    _settle(face, 3.0)
    tiles.append(("dozing: drooping", shot(face, "states/dozing_droop")))
    _settle(face, 6.0)
    tiles.append(("dozing: asleep", shot(face, "states/dozing")))
    face.animator.set_doze(False)
    _settle(face, 0.8)
    tiles.append(("waking up: stretch", shot(face, "states/waking")))
    sheet(tiles, "states")

    tiles = []
    for anim in FACE_ANIMATIONS:
        for k in (0.3, 0.6):
            face = fresh()
            face.set_expression("happy" if anim in ("confetti", "hearts", "sparkle") else "neutral")
            _settle(face, 0.5)
            face.play_animation(anim)
            _settle(face, ANIMATION_DURATIONS[anim] * k)
            tiles.append((f"{anim} {int(k * 100)}%", shot(face, f"animations/{anim}_{int(k * 100)}")))
    sheet(tiles, "animations")

    tiles = []
    for label, (x, y) in {"look left": (0.0, 0.5), "look right": (1.0, 0.5), "look up": (0.5, 0.0),
                          "look down": (0.5, 1.0), "up-left": (0.1, 0.1)}.items():
        face = fresh()
        face.look_at(x, y, hold=5)
        _settle(face, 0.8)
        tiles.append((label, shot(face, f"gaze/{label.replace(' ', '_')}")))
    face = fresh()
    _settle(face, 0.5)
    face.blink()
    _settle(face, 0.08)
    tiles.append(("mid-blink", shot(face, "gaze/mid_blink")))
    face = fresh()
    face.set_state("speaking")
    _settle(face, 0.5)
    for i, (lv, shape) in enumerate(((0.0, "closed"), (0.5, "ee"), (1.0, "ah"), (0.7, "oo"))):
        for _ in range(6):  # the mouth morphs to the spoken shape within ~100 ms
            face.animator.speech_level(lv, shape)
            face.update(1 / 30)
        tiles.append((f"talking {shape} {lv}", shot(face, f"gaze/talking_{i}")))
    sheet(tiles, "gaze_and_talking")

    # the simulator window itself (panel + help / debug overlays)
    saved += _window_shots(out_dir, seed)
    pygame.display.quit()
    return saved


def _window_shots(out_dir: Path, seed: int) -> list[Path]:
    import pygame

    from littlebot.face.display import DisplayLoop

    args = argparse.Namespace(scale=1.5, fullscreen=False, no_panel=False, debug=False, seed=seed)
    ctx, robot = build(args)
    robot.runtime.start()
    saved = []
    try:
        loop = DisplayLoop(robot, max_frames=20)
        loop.open()
        ctx.face.set_expression("happy")
        ctx.motion._joints["left_arm"]["angle"] = 120.0  # noqa: SLF001 - show a raised arm
        ctx.motion._joints["head"]["angle"] = 110.0  # noqa: SLF001
        loop.run()
        for name, setup in (("window", lambda: None), ("window_help", lambda: setattr(loop, "show_help", True)),
                            ("window_debug", lambda: (setattr(loop, "show_help", False),
                                                      setattr(loop, "show_debug", True)))):
            setup()
            loop.max_frames = loop.frames + 5
            loop.run()
            p = out_dir / f"{name}.png"
            pygame.image.save(loop.window, str(p))
            saved.append(p)
        loop.close()
    finally:
        robot.runtime.stop()
        ctx.db.close()
    return saved


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scale", type=float, default=None, help="window scale (default from config, 2.0)")
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--demo", action="store_true", help="loop the demo sequence")
    p.add_argument("--no-panel", action="store_true", help="hide the simulator side panel")
    p.add_argument("--debug", action="store_true", help="start with the debug overlay")
    p.add_argument("--seed", type=int, default=7, help="animation random seed")
    p.add_argument("--screenshot-dir", type=Path, help="render PNGs of every expression/state/animation and exit")
    p.add_argument("--run-for", type=float, default=None, help="quit after N seconds")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.screenshot_dir:
        files = screenshots(args.screenshot_dir, args.seed)
        print(f"saved {len(files)} images under {args.screenshot_dir}")
        return 0

    from littlebot.face.display import run_display

    ctx, robot = build(args)
    robot.runtime.start()
    robot.runtime.run(ctx.face.start())
    if args.run_for:
        threading.Timer(args.run_for, robot.request_shutdown, args=("--run-for elapsed",)).start()
    try:
        run_display(robot, demo=args.demo)
    except KeyboardInterrupt:
        pass
    finally:
        robot.shutdown_event.set()
        try:
            robot.runtime.run(ctx.face.stop(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        robot.runtime.stop()
        ctx.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
