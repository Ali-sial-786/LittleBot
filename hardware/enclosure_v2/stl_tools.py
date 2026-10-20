#!/usr/bin/env python3
"""Small STL helpers (no dependencies beyond numpy):

    stl_tools.py binary FILE.stl ...    convert OpenSCAD's ASCII STL to binary in place (~5x smaller)
    stl_tools.py report FILES...        size, triangles, watertight, shells, bed fit, overhangs
                                        (FILE.stl=N: the part is N separate pieces on purpose)
    stl_tools.py overhangs FILE.stl     list the biggest overhang patches (where supports would be needed)

"shells" counts separate solid bodies: a printable part must be exactly 1 (no floating islands).
"overhang" is the area (mm^2) of downward faces flatter than 45 degrees that do not sit on the
bed. Small numbers are normal (screw-hole tops, short bridges); big patches need a closer look.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

BED = (220.0, 220.0, 250.0)   # a common small printer (Ender 3 class); most parts fit 180 x 180
OVERHANG_NZ = -0.75           # normal z below this = flatter than ~41 degrees from horizontal


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


def vertex_ids(tris: np.ndarray) -> np.ndarray:
    q = np.round(tris.reshape(-1, 3).astype(np.float64), 3)
    _, ids = np.unique(q, axis=0, return_inverse=True)
    return ids.reshape(-1, 3)


def watertight(tris: np.ndarray) -> bool:
    ids = vertex_ids(tris)
    e = np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
    fwd = {}
    for a, b in e.tolist():
        fwd[(a, b)] = fwd.get((a, b), 0) + 1
    return all(fwd.get((b, a), 0) == c for (a, b), c in fwd.items())


def components(ids: np.ndarray) -> np.ndarray:
    """Label each triangle with its connected component (triangles sharing a vertex)."""
    parent = list(range(int(ids.max()) + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, c in ids.tolist():
        ra, rb, rc = find(a), find(b), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    return np.array([find(a) for a in ids[:, 0].tolist()])


def normals_areas(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = tris.astype(np.float64)
    cr = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    ln = np.linalg.norm(cr, axis=1)
    nrm = np.divide(cr, ln[:, None], out=np.zeros_like(cr), where=ln[:, None] > 0)
    return nrm, ln / 2


def overhang_mask(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    nrm, area = normals_areas(tris)
    zmin = tris[:, :, 2].min()
    on_bed = tris[:, :, 2].max(axis=1) < zmin + 0.1
    return (nrm[:, 2] < OVERHANG_NZ) & ~on_bed, area


def overhang_patches(tris: np.ndarray, top: int = 6) -> list[tuple[float, np.ndarray, np.ndarray]]:
    mask, area = overhang_mask(tris)
    if not mask.any():
        return []
    sub = tris[mask]
    lab = components(vertex_ids(sub))
    out = []
    for l in np.unique(lab):
        sel = lab == l
        pts = sub[sel].reshape(-1, 3)
        out.append((float(area[mask][sel].sum()), pts.min(0), pts.max(0)))
    return sorted(out, key=lambda t: -t[0])[:top]


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "binary":
        for f in argv[1:]:
            p = Path(f)
            write_binary(p, read_stl(p))
        return 0
    if len(argv) >= 2 and argv[0] == "report":
        ok = True
        print(f"{'part':<18}{'size x*y*z (mm)':<26}{'triangles':>10}  watertight  shells  fits {BED[0]:.0f}x{BED[1]:.0f}x{BED[2]:.0f}  overhang mm2")
        for arg in argv[1:]:
            f, _, want = arg.partition("=")
            t = read_stl(Path(f))
            if len(t) == 0:
                print(f"{Path(f).stem:<18}EMPTY"); ok = False; continue
            lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
            size = hi - lo
            fits = all(s <= b for s, b in zip(size, BED)) and lo[2] > -0.01
            wt = watertight(t)
            shells = len(np.unique(components(vertex_ids(t))))
            mask, area = overhang_mask(t)
            ok &= fits and wt and shells == int(want or 1)
            print(f"{Path(f).stem:<18}{size[0]:6.1f} x {size[1]:6.1f} x {size[2]:6.1f}   {len(t):>10}  {'yes' if wt else 'NO ':<10}  "
                  f"{shells:>6}  {'yes' if fits else 'NO':<13}  {area[mask].sum():9.0f}")
        return 0 if ok else 1
    if len(argv) == 2 and argv[0] == "overhangs":
        for a, lo, hi in overhang_patches(read_stl(Path(argv[1]))):
            print(f"{a:8.1f} mm2   x {lo[0]:7.1f}..{hi[0]:7.1f}   y {lo[1]:7.1f}..{hi[1]:7.1f}   z {lo[2]:6.1f}..{hi[2]:6.1f}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
