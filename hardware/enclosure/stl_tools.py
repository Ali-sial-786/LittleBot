#!/usr/bin/env python3
"""Small STL helpers (no dependencies beyond numpy):

    stl_tools.py binary FILE.stl     convert OpenSCAD's ASCII STL to binary in place (~5x smaller)
    stl_tools.py report FILES...     size, triangle count, watertight check, fits-the-bed check
    stl_tools.py bbox FILE.stl       bounding-box size (used for the INFO checks)
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

BED = (220.0, 220.0, 250.0)   # a common small printer (Ender 3 class); most parts fit 180 x 180


def read_stl(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if data[:5] == b"solid" and b"facet" in data[:400]:
        tris = []
        for line in data.decode("ascii", "ignore").splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                tris.append([float(v) for v in line.split()[1:4]])
        return np.array(tris, dtype=np.float32).reshape(-1, 3, 3)
    n = struct.unpack("<I", data[80:84])[0]
    rec = np.frombuffer(data[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    return rec["v"].copy()


def write_binary(path: Path, tris: np.ndarray) -> None:
    v = tris.astype(np.float64)
    nrm = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.divide(nrm, ln, out=np.zeros_like(nrm), where=ln > 0)
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = nrm, tris
    header = f"LittleBot {path.stem}".encode().ljust(80, b" ")
    path.write_bytes(header + struct.pack("<I", len(tris)) + rec.tobytes())


def watertight(tris: np.ndarray) -> bool:
    q = np.round(tris.astype(np.float64), 4)
    edges = np.concatenate([q[:, [0, 1]], q[:, [1, 2]], q[:, [2, 0]]])
    a = [tuple(e[0]) + tuple(e[1]) for e in edges]
    b = [tuple(e[1]) + tuple(e[0]) for e in edges]
    fwd = {}
    for k in a:
        fwd[k] = fwd.get(k, 0) + 1
    return all(fwd.get(k, 0) == c for k, c in zip(b, (fwd[k] for k in a)))


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "binary":
        for f in argv[1:]:
            p = Path(f)
            write_binary(p, read_stl(p))
        return 0
    if len(argv) == 2 and argv[0] == "bbox":
        t = read_stl(Path(argv[1]))
        size = t.reshape(-1, 3).max(0) - t.reshape(-1, 3).min(0)
        print(f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm (x, depth, height)")
        return 0
    if len(argv) >= 2 and argv[0] == "report":
        ok = True
        print(f"{'part':<14}{'size x*y*z (mm)':<26}{'triangles':>10}  watertight  fits {BED[0]:.0f}x{BED[1]:.0f}x{BED[2]:.0f}")
        for f in argv[1:]:
            t = read_stl(Path(f))
            if len(t) == 0:
                print(f"{Path(f).stem:<14}EMPTY"); ok = False; continue
            lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
            size = hi - lo
            fits = all(s <= b for s, b in zip(size, BED)) and lo[2] > -0.01
            wt = watertight(t)
            ok &= fits and wt
            print(f"{Path(f).stem:<14}{size[0]:6.1f} x {size[1]:6.1f} x {size[2]:6.1f}   {len(t):>10}  {'yes' if wt else 'NO ':<10}  {'yes' if fits else 'NO'}")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
