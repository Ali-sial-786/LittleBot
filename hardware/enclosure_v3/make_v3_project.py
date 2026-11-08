#!/usr/bin/env python3
"""LittleBot v3: pack the printed parts onto as few 256 mm plates as possible and write ONE Bambu Studio
project (Bambu Lab A1, 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic, Textured PEI plate).

    python3 make_v3_project.py            # reads stl/*.stl (export_v3.sh), writes LittleBot_v3_A1.3mf + plates_v3.png

Plates (colours are never mixed on a plate; small parts nest inside the big hollow ones):
  1  white: body, with the left arm inside its footprint, and the camera pod
  2  white: head frame (face down, tree supports from the bed under its 4 screen bosses) + the head's back cover
  4  white: right arm + neck plate (both arms don't fit inside the body, and white parts never share the dark plate)
  3  dark:  base ring, with the UPS switch slider inside it. The ring's top 2 mm
           (z 22-24) switches to the blue filament: the accent line.
Filament slots: 1 white, 2 dark grey, 3 light blue (Bambu Studio maps them to AMS slots when printing).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
V2 = HERE.parent / "enclosure_v2"
sys.path.insert(0, str(V2))
from stl_tools import read_stl, vertex_ids                      # noqa: E402
import bambu_project as bp                                         # noqa: E402  (settings + presets as v2)

BED, EDGE, GAP, RES = 256.0, 6.0, 2.5, 0.5
COLOURS = {"white": "#F2EFE8", "dark": "#2B2D31", "blue": "#58B7F0"}
SLOTS = ["white", "dark", "blue"]
PLATES = [("body", "white", ["body", "arm_left", "pod"]),
          ("head", "white", ["head_frame", "head_back"]),
          ("ring", "dark", ["ring", "ups_slider"]),
          ("arm", "white", ["arm_right", "neck_plate"])]
SUPPORT = {"head_frame"}                 # tree supports from the build plate only
BRIM = {"body": 5.0}                     # 173 mm tall
BAND = (22.0, 24.0)                      # the ring's accent line (robot z = print z for the ring)


def load(name):
    t = read_stl(HERE / "stl" / f"{name}.stl").astype(float)
    lo = t.reshape(-1, 3).min(0)
    return t - [lo[0], lo[1], lo[2]]


def raster(tris, zmax=None, grow=0.0):
    """Top-view footprint (cells) of the triangles with any vertex below zmax, grown by `grow` mm."""
    hi = tris.reshape(-1, 3).max(0)
    n = (int(np.ceil((hi[0] + 2 * grow) / RES)) + 2, int(np.ceil((hi[1] + 2 * grow) / RES)) + 2)
    m = np.zeros(n, bool)
    sel = tris if zmax is None else tris[tris[:, :, 2].min(1) < zmax]
    for tri in sel:
        p = (tri[:, :2] + grow) / RES
        x0, y0 = np.floor(p.min(0)).astype(int); x1, y1 = np.ceil(p.max(0)).astype(int)
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1), indexing="ij")
        pts = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], 1)
        a, b, c = p
        d = lambda u, v, w: (v[0] - u[0]) * (w[:, 1] - u[1]) - (v[1] - u[1]) * (w[:, 0] - u[0])
        d1, d2, d3 = d(a, b, pts), d(b, c, pts), d(c, a, pts)
        inside = ~((np.minimum(np.minimum(d1, d2), d3) < -1e-9) & (np.maximum(np.maximum(d1, d2), d3) > 1e-9))
        # thin triangles (walls seen edge-on) cover at least the cells their edges cross
        for u, v, dd in ((a, b, d1), (b, c, d2), (c, a, d3)):     # cells within ~0.7 cell of an edge segment
            L = np.hypot(*(v - u))
            if L > 1e-9:
                tpar = ((pts[:, 0] - u[0]) * (v[0] - u[0]) + (pts[:, 1] - u[1]) * (v[1] - u[1])) / (L * L)
                inside |= (np.abs(dd) / L < 0.7) & (tpar > -0.05) & (tpar < 1.05)
        ok = inside & (xs.ravel() >= 0) & (ys.ravel() >= 0) & (xs.ravel() < n[0]) & (ys.ravel() < n[1])
        m[xs.ravel()[ok], ys.ravel()[ok]] = True
    if grow > 0:   # dilate
        r = int(np.ceil(grow / RES)); g = m.copy()
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    g |= np.roll(np.roll(m, dx, 0), dy, 1)
        m = g
    return m


def rot(tris, k):
    a = np.radians(45 * k); c, s = np.cos(a), np.sin(a)
    t = np.stack([c * tris[..., 0] - s * tris[..., 1], s * tris[..., 0] + c * tris[..., 1], tris[..., 2]], -1)
    lo = t.reshape(-1, 3).min(0)
    return t - [lo[0], lo[1], 0]


def free_spots(occ, fp):
    """Positions (ix, iy) where footprint fp fits in the free cells of occ (FFT correlation)."""
    from scipy.signal import fftconvolve
    hit = fftconvolve(occ.astype(np.float32), fp[::-1, ::-1].astype(np.float32), mode="valid")
    return np.argwhere(hit < 0.5)


def pack(names):
    """First part centred; the others placed one by one in free spots (inside hollow parts when they are low enough),
    backtracking over the earlier small parts' turns when a later one finds no room."""
    N = int(BED / RES); e = int(EDGE / RES)
    first = load(names[0]); hi = first.reshape(-1, 3).max(0)
    y0 = EDGE if names[0] == "head_frame" else (BED - hi[1]) / 2
    base = [(names[0], first + [(BED - hi[0]) / 2, y0, 0])]
    rest = [load(n) for n in names[1:]]

    def occ_for(placed, h):
        occ = np.ones((N, N), bool); occ[e:N - e, e:N - e] = False
        for _, pt in placed:
            m = raster(pt, zmax=h + 1.0, grow=GAP)
            occ[:min(N, m.shape[0]), :min(N, m.shape[1])] |= m[:N, :N]
        return occ

    def place(i, placed):
        if i == len(rest):
            return placed
        options = []
        for k in range(8):
            t = rot(rest[i], k); h = t[:, :, 2].max()
            spots = free_spots(occ_for(placed, h), raster(t))
            if len(spots):
                for corner in (lambda s: s[:, 1] * N + s[:, 0], lambda s: -s[:, 1] * N + s[:, 0],
                               lambda s: s[:, 0] * N + s[:, 1], lambda s: -s[:, 0] * N - s[:, 1]):
                    ix, iy = spots[np.argmin(corner(spots))]
                    options.append(t + [ix * RES - RES, iy * RES - RES, 0])
        for t in options:
            r = place(i + 1, placed + [(names[i + 1], t)])
            if r is not None:
                return r
        return None

    out = place(0, base)
    if out is None:
        raise SystemExit(f"{names}: they don't all fit on one plate")
    return out


def project(plates, out):
    objects, items, obj_cfg, plate_cfg, assemble = [], [], [], [], []
    k = 0
    cols = 2
    for i, (pname, colour, parts) in enumerate(plates):
        origin = np.array([(i % cols) * bp.STRIDE, -(i // cols) * bp.STRIDE, 0.0])
        inst = []
        for name, tris in parts:
            k += 1
            lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
            assert lo[0] >= 0 and lo[1] >= 0 and hi[0] <= BED and hi[1] <= BED, (pname, name, lo, hi)
            centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
            ids = vertex_ids(tris); verts = np.zeros((ids.max() + 1, 3)); verts[ids.reshape(-1)] = tris.reshape(-1, 3) - centre
            v = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in verts)
            t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in ids if len({a, b, c}) == 3)
            objects.append(f'<object id="{k}" name="{name}" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>')
            p = centre + origin
            tf = f"1 0 0 0 1 0 0 0 1 {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}"
            items.append(f'<item objectid="{k}" transform="{tf}" printable="1"/>')
            meta = [("name", name), ("extruder", SLOTS.index(colour) + 1)]
            meta += [("brim_type", "outer_only"), ("brim_width", BRIM[name])] if name in BRIM else [("brim_type", "no_brim")]
            if name in SUPPORT:
                meta += [("enable_support", 1), ("support_type", "tree(auto)"), ("support_on_build_plate_only", 1)]
            md = "".join(f'    <metadata key="{a}" value="{b}"/>\n' for a, b in meta)
            obj_cfg.append(f'  <object id="{k}">\n{md}    <part id="{k}" subtype="normal_part">\n      <metadata key="name" value="{name}"/>\n'
                           f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n  </object>\n')
            inst.append(f'    <model_instance>\n      <metadata key="object_id" value="{k}"/>\n      <metadata key="instance_id" value="0"/>\n'
                        f'      <metadata key="identify_id" value="{100 + k}"/>\n    </model_instance>\n')
            assemble.append(f'   <assemble_item object_id="{k}" instance_id="0" transform="{tf}" offset="0 0 0"/>\n')
        plate_cfg.append(f'  <plate>\n    <metadata key="plater_id" value="{i + 1}"/>\n    <metadata key="plater_name" value="{i + 1} {pname} ({colour})"/>\n'
                         f'    <metadata key="locked" value="false"/>\n{"".join(inst)}  </plate>\n')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">\n'
             f' <metadata name="Application">{bp.APP}</metadata>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
             f' <metadata name="Title">LittleBot v3</metadata>\n <resources>{"".join(objects)}</resources>\n <build>{"".join(items)}</build>\n</model>\n')
    config = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(obj_cfg) + "".join(plate_cfg) + "  <assemble>\n" + "".join(assemble) + "  </assemble>\n</config>\n"
    ring_plate = 1 + [p[0] for p in plates].index("ring")
    gcode = ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n'
             f'<plate_info id="{ring_plate}"/>\n'
             f'<layer top_z="{BAND[0] + 0.2:.1f}" type="2" extruder="3" color="{COLOURS["blue"]}" extra="" gcode="tool_change"/>\n'
             f'<layer top_z="{BAND[1] + 0.2:.1f}" type="2" extruder="2" color="{COLOURS["dark"]}" extra="" gcode="tool_change"/>\n'
             '<mode value="MultiAsSingle"/>\n</plate>\n</custom_gcodes_per_layer>\n')
    settings = bp.project_settings(["main", "black", "glow"], "A1")
    settings["filament_colour"] = [COLOURS[s] for s in SLOTS]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   '<Default Extension="config" ContentType="text/xml"/><Default Extension="xml" ContentType="text/xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", config)
        z.writestr("Metadata/custom_gcode_per_layer.xml", gcode)
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=4))
    print(f"{out.name}: {len(plates)} plates, {k} parts")


def preview(plates, out):
    from PIL import Image, ImageDraw
    S = 2; W = int(BED * S)
    img = Image.new("RGB", (len(plates) * (W + 20) + 20, W + 60), "white"); d = ImageDraw.Draw(img)
    fill = {"white": (232, 228, 218), "dark": (60, 62, 68)}
    for i, (pname, colour, parts) in enumerate(plates):
        ox = 20 + i * (W + 20); d.rectangle([ox, 40, ox + W, 40 + W], outline=(0, 0, 0))
        d.text((ox, 12), f"plate {i + 1}: {pname} ({colour})", fill=(0, 0, 0))
        for name, tris in sorted(parts, key=lambda p: -p[1][:, :, 2].max()):
            m = raster(tris)
            ys, xs = np.nonzero(m.T)
            for x, y in zip(xs[::2], ys[::2]):
                d.point((ox + x * RES * S, 40 + W - y * RES * S), fill=fill[colour] if name not in ("body", "ring") else ((200, 196, 186) if colour == "white" else (35, 36, 40)))
            lo = tris.reshape(-1, 3).min(0); hi = tris.reshape(-1, 3).max(0)
            d.text((ox + (lo[0] + hi[0]) / 2 * S - 20, 40 + W - (lo[1] + hi[1]) / 2 * S), name, fill=(200, 30, 30))
    img.save(out)


if __name__ == "__main__":
    plates = [(pname, colour, pack(names)) for pname, colour, names in PLATES]
    project(plates, HERE / "LittleBot_v3_A1.3mf")
    preview(plates, HERE / "plates_v3.png")
