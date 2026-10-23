#!/usr/bin/env python3
"""Group the printable parts into build plates (one STL per print job).

    python3 make_plates.py [BED_MM]      # default 220 (a 220 x 220 mm bed)

Reads stl/*.stl (already in print orientation), lays the parts of each plate out in rows
with a 6 mm gap, checks that every plate fits the bed, and writes plates/NN_name.stl.
The robot itself can't be printed as one piece: it is taller than most printers, the
electronics go inside, and the head and arms have to move.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from stl_tools import read_stl, write_binary

GAP = 6.0

# (file name, colour, rows of parts; a part name ending in "@90" is turned 90 degrees)
PLATES = [
    ("01_base",        "main colour",           [["base"]]),
    ("02_torso",       "main colour",           [["torso"]]),
    ("03_head",        "main colour",           [["head", "camera_pod"]]),
    ("04_floor",       "main colour",           [["base_floor"], ["well_cover", "well_cover"]]),
    ("05_back_cap",    "main colour",           [["head_back", "shelf"]]),
    ("06_arms",        "main colour",           [["torso_back", "arm_left"], ["arm_right"]]),
    ("07_small_parts", "main colour, print slowly (gears)",
                                                [["coupler", "ring_gear", "pinion"], ["servo_frame", "camera", "rear_usb"], ["pi_carrier"]]),
    ("08_visor",       "black",                 [["visor"]]),
    ("09_glow",        "white / natural / translucent", [["ear_left", "ear_right"], ["front_blank"]]),
    # later, for the wheel phase
    ("10_mobile_floor", "main colour (wheel phase)", [["base_floor_mobile"]]),
    ("11_sonar_plate",  "any (wheel phase)",        [["front_sonar"]]),
]


def load(name: str) -> np.ndarray:
    turn = name.endswith("@90")
    t = read_stl(Path("stl") / f"{name.removesuffix('@90')}.stl").astype(np.float64)
    if turn:
        t = t[..., [1, 0, 2]] * np.array([-1.0, 1.0, 1.0])
    return t - t.reshape(-1, 3).min(0)          # bounding box corner at the origin, sitting on z = 0


def layout(rows: list[list[str]]) -> tuple[np.ndarray, np.ndarray]:
    placed, y = [], 0.0
    for row in rows:
        x, depth = 0.0, 0.0
        for name in row:
            t = load(name)
            size = t.reshape(-1, 3).max(0)
            placed.append(t + np.array([x, y, 0.0]))
            x += size[0] + GAP
            depth = max(depth, size[1])
        y += depth + GAP
    tris = np.concatenate(placed)
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
    return tris - centre, hi - lo


def main(argv: list[str]) -> int:
    bed = float(argv[0]) if argv else 220.0
    out = Path("plates")
    out.mkdir(exist_ok=True)
    ok = True
    print(f"{'plate':<18}{'parts':>6}  {'footprint (mm)':<18}{'height':>7}  fits {bed:.0f} x {bed:.0f}  filament")
    for name, colour, rows in PLATES:
        tris, size = layout(rows)
        fits = size[0] <= bed and size[1] <= bed
        ok &= fits
        write_binary(out / f"{name}.stl", tris.astype(np.float32))
        n = sum(len(r) for r in rows)
        print(f"{name:<18}{n:>6}  {size[0]:6.1f} x {size[1]:6.1f}   {size[2]:6.1f}  {'yes' if fits else 'NO':<12}  {colour}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
