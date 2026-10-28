#!/usr/bin/env python3
"""Pack the printable parts onto as few build plates as a bigger bed allows (3MF, one object per part).

    python3 pack_plates.py [BED_MM]      # default 256 (Bambu Lab A1 / P1S / P2S / X1C)

Writes plates_<BED>/NN_<colour>.3mf and images/plates_<BED>.png (top view). Unlike make_plates.py
(fixed rows for a 220 mm bed) the parts are packed automatically: largest first, each into the
tightest free spot, turned 90 degrees when that fits better.

Colours are never mixed on a plate, even with an AMS: the black visor is 33 mm tall, so a mixed
plate changes filament on every layer up to there (hundreds of swaps, hours and lots of purge).
Each part is its own named object, so per-object settings (slow gears) are easy in the slicer.
The stationary build only; the wheel-phase parts stay in plates/.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np

from stl_tools import read_stl, vertex_ids

GAP = 10.0                # between parts: room for a 4 mm brim on each side
MARGIN = 5.0              # from the bed edge
EXCLUDE = (18.0, 28.0)    # front-left corner Bambu Lab X1 / P1 keep free

GROUPS = [
    ("main", "main colour", ["base", "torso", "head", "camera_pod", "base_floor", "well_cover", "well_cover",
                             "head_back", "shelf", "torso_back", "arm_left", "arm_right", "coupler", "ring_gear",
                             "pinion", "servo_frame", "camera", "rear_usb", "pi_carrier"]),
    ("black", "black", ["visor"]),
    ("glow", "white / natural / translucent", ["ear_left", "ear_right", "front_blank"]),
]


def load(name: str) -> np.ndarray:
    t = read_stl(Path("stl") / f"{name}.stl").astype(np.float64)
    return t - t.reshape(-1, 3).min(0)


class Plate:
    """MaxRects bin: free rectangles, each part padded by GAP on its right and back."""

    def __init__(self, size: float) -> None:
        self.free = [(0.0, 0.0, size + GAP, size + GAP)]
        self.parts: list[tuple[str, float, float, bool]] = []
        self.take(0.0, 0.0, EXCLUDE[0] - MARGIN + GAP, EXCLUDE[1] - MARGIN + GAP)

    def spot(self, w: float, h: float):
        best = None
        for fx, fy, fw, fh in self.free:
            for rw, rh, turned in ((w, h, False), (h, w, True)):
                if rw <= fw + 1e-9 and rh <= fh + 1e-9:
                    key = (min(fw - rw, fh - rh), max(fw - rw, fh - rh))
                    if best is None or key < best[0]:
                        best = (key, fx, fy, rw, rh, turned)
        return best

    def take(self, x: float, y: float, w: float, h: float) -> None:
        free = []
        for fx, fy, fw, fh in self.free:
            if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
                free.append((fx, fy, fw, fh))
                continue
            if x > fx:
                free.append((fx, fy, x - fx, fh))
            if x + w < fx + fw:
                free.append((x + w, fy, fx + fw - x - w, fh))
            if y > fy:
                free.append((fx, fy, fw, y - fy))
            if y + h < fy + fh:
                free.append((fx, y + h, fw, fy + fh - y - h))
        self.free = [a for a in free if not any(a != b and a[0] >= b[0] and a[1] >= b[1] and a[0] + a[2] <= b[0] + b[2]
                                                and a[1] + a[3] <= b[1] + b[3] for b in free)]


def pack(names: list[str], usable: float) -> list[Plate]:
    sizes = {n: load(n).reshape(-1, 3).max(0)[:2] for n in set(names)}
    plates: list[Plate] = []
    for n in sorted(names, key=lambda n: -sizes[n][0] * sizes[n][1]):
        w, h = sizes[n] + GAP
        for p in plates + [Plate(usable)]:
            s = p.spot(w, h)
            if s:
                p.take(*s[1:5])
                p.parts.append((n, s[1], s[2], s[5]))
                if p not in plates:
                    plates.append(p)
                break
        else:
            raise SystemExit(f"{n} does not fit a {usable + 2 * MARGIN:.0f} mm bed")
    return plates


def placed(p: Plate, bed: float) -> list[tuple[str, np.ndarray]]:
    """The packed parts on the bed, moved together towards the middle (the flattest part of a bed)."""
    out = []
    for n, x, y, turned in p.parts:
        t = load(n)
        if turned:
            t = t[..., [1, 0, 2]] * np.array([-1.0, 1.0, 1.0])
            t -= t.reshape(-1, 3).min(0)
        out.append((n, t + np.array([MARGIN + x, MARGIN + y, 0.0])))
    lo = np.min([t.reshape(-1, 3).min(0) for _, t in out], axis=0)
    hi = np.max([t.reshape(-1, 3).max(0) for _, t in out], axis=0)
    shift = np.maximum(0.0, (bed - hi - lo) / 2) * np.array([1.0, 1.0, 0.0])
    return [(n, t + shift) for n, t in out]


def write_3mf(path: Path, parts: list[tuple[str, np.ndarray]], label: str) -> None:
    objects, items = [], []
    for i, (name, tris) in enumerate(parts, 1):
        lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
        centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
        ids = vertex_ids(tris)
        verts = np.zeros((ids.max() + 1, 3))
        verts[ids.reshape(-1)] = tris.reshape(-1, 3) - centre
        v = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in verts)
        f = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in ids if len({a, b, c}) == 3)
        objects.append(f'<object id="{i}" name="{name} ({label})" type="model"><mesh><vertices>{v}</vertices>'
                       f'<triangles>{f}</triangles></mesh></object>')
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 {centre[0]:.4f} {centre[1]:.4f} 0"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<metadata name="Title">LittleBot v2 {path.stem}</metadata>'
             f'<resources>{"".join(objects)}</resources><build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.'
                   '3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://'
                   'schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)


def check(parts: list[tuple[str, np.ndarray]], bed: float) -> list[str]:
    """Every part on the bed, out of the corner, and >= GAP from every other part (bounding boxes)."""
    boxes = [(n, t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)) for n, t in parts]
    bad = [f"{n} off the bed" for n, lo, hi in boxes if lo[:2].min() < MARGIN - 1e-6 or hi[:2].max() > bed - MARGIN + 1e-6]
    bad += [f"{n} in the corner" for n, lo, hi in boxes if lo[0] < EXCLUDE[0] and lo[1] < EXCLUDE[1]]
    for i, (a, alo, ahi) in enumerate(boxes):
        for b, blo, bhi in boxes[i + 1:]:
            if not (alo[0] >= bhi[0] + GAP - 1e-6 or blo[0] >= ahi[0] + GAP - 1e-6
                    or alo[1] >= bhi[1] + GAP - 1e-6 or blo[1] >= ahi[1] + GAP - 1e-6):
                bad.append(f"{a} and {b} closer than {GAP:.0f} mm")
    return bad


def preview(path: Path, plates: list[tuple[str, str, list[tuple[str, np.ndarray]]]], bed: float) -> None:
    from PIL import Image, ImageDraw
    px, pad, cols = 2.2, 30, 3
    side, rows = int(bed * px), -(-len(plates) // cols)
    img = Image.new("RGB", (cols * (side + pad) + pad, rows * (side + pad + 20) + pad), "white")
    d = ImageDraw.Draw(img)
    fill = {"main": (120, 170, 220), "black": (60, 60, 60), "glow": (235, 225, 170)}
    for k, (name, group, parts) in enumerate(plates):
        ox, oy = pad + k % cols * (side + pad), pad + 20 + k // cols * (side + pad + 20)
        d.rectangle([ox, oy, ox + side, oy + side], outline=(0, 0, 0), fill=(245, 245, 245))
        d.rectangle([ox, oy + side - EXCLUDE[1] * px, ox + EXCLUDE[0] * px, oy + side], fill=(255, 210, 210))
        d.text((ox, oy - 18), name, fill=(0, 0, 0))
        for n, t in parts:
            for tri in t[:, :, :2]:
                d.polygon([(ox + x * px, oy + side - y * px) for x, y in tri], fill=fill[group])
            c = t.reshape(-1, 3)
            d.text((ox + (c[:, 0].min() + 2) * px, oy + side - (c[:, 1].max() - 2) * px), n, fill=(0, 0, 0))
    img.save(path)


def main(argv: list[str]) -> int:
    bed = float(argv[0]) if argv else 256.0
    out = Path(f"plates_{bed:.0f}")
    out.mkdir(exist_ok=True)
    for old in out.glob("*.3mf"):
        old.unlink()
    done, bad, k = [], [], 0
    print(f"{'plate':<14}{'parts':>6}  {'filament':<32}parts")
    for group, colour, names in GROUPS:
        for p in pack(names, bed - 2 * MARGIN):
            k += 1
            name = f"{k:02d}_{group}"
            parts = placed(p, bed)
            bad += [f"{name}: {b}" for b in check(parts, bed)]
            write_3mf(out / f"{name}.3mf", parts, colour.split(" /")[0])
            done.append((name, group, parts))
            print(f"{name:<14}{len(parts):>6}  {colour:<32}{', '.join(n for n, _ in parts)}")
    Path("images").mkdir(exist_ok=True)
    preview(Path("images") / f"plates_{bed:.0f}.png", done, bed)
    for b in bad:
        print("PROBLEM:", b)
    print(f"{k} plates for a {bed:.0f} x {bed:.0f} mm bed -> {out}/")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
