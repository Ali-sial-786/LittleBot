#!/usr/bin/env python3
"""Benchmark the face: update + render cost per frame for every expression / state / animation.

    .venv/bin/python scripts/benchmark_face.py               # table, 150 frames per scenario
    .venv/bin/python scripts/benchmark_face.py --frames 300 --json
    .venv/bin/python scripts/benchmark_face.py --scale 2      # include the window up-scale cost

Runs headless (SDL dummy driver). "cpu@30fps" estimates the share of ONE core the
face would need at 30 fps (mean_ms * 30 / 10). Frames are rendered with a fixed
1/30 s time step so animations actually move (cache misses included); a "calm"
scenario shows the steady-state cost when the eye-shape cache is warm.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


def _scenarios() -> list[tuple[str, list[tuple[str, tuple[Any, ...]]]]]:
    from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS

    out: list[tuple[str, list[tuple[str, tuple[Any, ...]]]]] = [("calm (neutral, cache warm)", [])]
    out += [(f"expr {e}", [("set_expression", (e,))]) for e in EXPRESSIONS]
    out += [(f"state {s}", [("set_state", (s,))]) for s in ("listening", "thinking", "speaking", "sleeping", "hidden")]
    out += [(f"anim {a}", [("play_animation", (a,))]) for a in FACE_ANIMATIONS]
    out += [("expression cycling", [("_cycle", ())])]
    return out


def run(frames: int, scale: float, seed: int) -> dict[str, Any]:
    import pygame

    from littlebot.core.interfaces import EXPRESSIONS
    from littlebot.face.controller import FaceController
    from littlebot.testing import make_test_context

    pygame.display.init()
    pygame.font.init()
    native = pygame.Surface((480, 320))
    window = pygame.Surface((int(480 * scale), int(320 * scale))) if scale != 1.0 else None
    rows = []
    dt = 1 / 30
    for name, setup in _scenarios():
        ctx = make_test_context(fakes=False)
        face = FaceController(ctx, seed=seed)
        for method, args in setup:
            if method != "_cycle":
                getattr(face, method)(*args)
        # warm-up so transitions finish (except the cycling scenario)
        for _ in range(20):
            face.update(dt)
            face.render(native)
        upd, ren, tot = [], [], []
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        for i in range(frames):
            if setup and setup[0][0] == "_cycle" and i % 12 == 0:
                face.set_expression(EXPRESSIONS[(i // 12) % len(EXPRESSIONS)])
            if setup and setup[0][0] == "play_animation" and i % 45 == 0:
                face.play_animation(setup[0][1][0])
            t0 = time.perf_counter()
            face.update(dt)
            t1 = time.perf_counter()
            if window is not None:
                face.render(window, window.get_rect())
            else:
                face.render(native)
            t2 = time.perf_counter()
            upd.append((t1 - t0) * 1000)
            ren.append((t2 - t1) * 1000)
            tot.append((t2 - t0) * 1000)
        cpu = time.process_time() - cpu0
        wall = time.perf_counter() - wall0
        tot_sorted = sorted(tot)
        mean = statistics.fmean(tot)
        rows.append({
            "scenario": name,
            "update_ms": round(statistics.fmean(upd), 3),
            "render_ms": round(statistics.fmean(ren), 3),
            "mean_ms": round(mean, 3),
            "p95_ms": round(tot_sorted[int(len(tot_sorted) * 0.95) - 1], 3),
            "max_ms": round(tot_sorted[-1], 3),
            "cpu_at_30fps_percent": round(mean * 30 / 10, 1),
            "cpu_ratio": round(cpu / wall, 2) if wall > 0 else None,
            "cache": face.renderer.cache_info() if face.renderer else {},
        })
        ctx.db.close()
    pygame.display.quit()
    means = [r["mean_ms"] for r in rows]
    return {
        "frames_per_scenario": frames,
        "scale": scale,
        "python": sys.version.split()[0],
        "pygame": pygame.version.ver,
        "overall_mean_ms": round(statistics.fmean(means), 3),
        "worst_mean_ms": round(max(means), 3),
        "worst_p95_ms": round(max(r["p95_ms"] for r in rows), 3),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", type=int, default=150)
    p.add_argument("--scale", type=float, default=1.0, help="also scale into a window-sized surface")
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = p.parse_args(argv)
    res = run(max(10, args.frames), args.scale, args.seed)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    print(f"LittleBot face benchmark  ({res['frames_per_scenario']} frames/scenario, scale {res['scale']}, "
          f"python {res['python']}, pygame-ce {res['pygame']})")
    hdr = f"{'scenario':30} {'update':>8} {'render':>8} {'mean':>8} {'p95':>8} {'max':>8} {'cpu@30fps':>10}"
    print(hdr)
    print("-" * len(hdr))
    for r in res["rows"]:
        print(f"{r['scenario']:30} {r['update_ms']:8.3f} {r['render_ms']:8.3f} {r['mean_ms']:8.3f} "
              f"{r['p95_ms']:8.3f} {r['max_ms']:8.3f} {r['cpu_at_30fps_percent']:9.1f}%")
    print("-" * len(hdr))
    print(f"overall mean {res['overall_mean_ms']:.3f} ms/frame, worst scenario mean {res['worst_mean_ms']:.3f} ms, "
          f"worst p95 {res['worst_p95_ms']:.3f} ms  (all times in ms; cpu = share of one core)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
