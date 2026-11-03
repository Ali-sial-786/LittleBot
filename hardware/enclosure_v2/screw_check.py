#!/usr/bin/env python3
"""How much thread every screw gets (checks.scad "screws_thread": the ring between the pilot hole and the screw's
diameter, intersected with the printed parts as assembled). Fails if an M3 screw cuts less than MIN_M3 mm of
thread, or a servo screw less than MIN_SERVO.

    python3 screw_check.py [-D ...]      # the same -D options as the model (e.g. plate variants)
"""
import json, re, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from stl_tools import read_stl, vertex_ids, components

MIN_M3, MIN_SERVO = 3.5, 2.9                # the torso -> base screws reach 3.9 mm (the printed torso's pilots); the arm ribs are 3 mm thick
A12 = lambda d: 6 * (d / 2) ** 2 * 0.5                     # area of the 12-gons checks.scad uses
RING = {3: A12(3.0) - A12(2.75), 2: A12(2.0) - A12(1.85)}
here = Path(__file__).resolve().parent
extra = sys.argv[1:]
with tempfile.TemporaryDirectory() as tmp:
    stl, echo = Path(tmp) / "t.stl", Path(tmp) / "hw.echo"
    subprocess.run(["openscad", "-o", str(stl), "-D", 'check="screws_thread"', *extra, "checks.scad"], cwd=here, check=True, capture_output=True)
    subprocess.run(["openscad", "-o", str(echo), "-D", 'part="hardware_list"', *extra, "littlebot_v2.scad"], cwd=here, check=True, capture_output=True)
    screws = json.loads(re.search(r"ECHO: hw = (.*)", echo.read_text()).group(1))[0]
    tris = read_stl(stl).astype(float) if stl.stat().st_size > 200 else np.zeros((0, 3, 3))
checked = [k for k, sc in enumerate(screws) if sc[2] == 3 or sc[1] == "servo screws"]
length = {k: 0.0 for k in checked}
if len(tris):
    lab = components(vertex_ids(tris.astype(np.float32)))
    for c in np.unique(lab):
        t = tris[lab == c]
        vol = abs(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)
        cen = t.reshape(-1, 3).mean(0)
        def dist(k):                                        # centroid -> the screw's axis segment
            a = np.array(screws[k][5], float); v = np.array(screws[k][6], float); v /= np.linalg.norm(v)
            u = np.clip((cen - a) @ v, 0, screws[k][3]); return np.linalg.norm(cen - (a + u * v))
        k = min(checked, key=dist)
        length[k] += vol / RING[screws[k][2]]
bad = 0
print(f"{'step':<12}{'screw':<15}{'where (mm)':<28}{'thread':>8}")
for k in checked:
    sc = screws[k]; need = MIN_M3 if sc[2] == 3 else MIN_SERVO; ok = length[k] >= need
    bad += not ok
    print(f"{sc[0]:<12}{sc[1]:<15}{'%7.1f %7.1f %7.1f' % tuple(sc[5]):<28}{length[k]:7.1f}  {'' if ok else 'TOO SHORT (< %g)' % need}")
print(f"{len(checked)} screws, {bad} with too little thread")
sys.exit(1 if bad else 0)
