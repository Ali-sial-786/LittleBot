#!/usr/bin/env python3
"""LittleBot v4 "Soft Arc": pack the printed parts onto 256 mm plates and write ONE Bambu Studio project
(Bambu Lab A1, 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic, Textured PEI plate).

    ./export_v4.sh && python3 make_v4_project.py      # reads stl/*.stl, writes LittleBot_v4_A1.3mf + plates_v4.png

Plates (colours never mixed on a plate, except the head frame's dark arch, printed in the same job):
  1  white: body, both arms inside its footprint (5 mm brim on the body)
  2  white: head frame, face down, with its dark arch (the face's first 8 layers, 2nd filament);
            tree supports from the bed under the screen's 4 bosses and the camera's pivot bosses
  3  white: head back cover (rim down, tree supports inside its dome) + neck plate
  4  dark:  base ring, black neck (puck), AA box door, UPS switch slider
Filament slots: 1 white, 2 dark grey (Bambu Studio maps them to AMS slots when printing).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "enclosure_v2"))
sys.path.insert(0, str(HERE.parent / "enclosure_v3"))
from stl_tools import read_stl, vertex_ids                      # noqa: E402
import bambu_project as bp                                         # noqa: E402
from make_v3_project import raster, rot, free_spots, BED, EDGE, GAP, RES   # noqa: E402  (v3's packer pieces)

COLOURS = {"white": "#F2EFE8", "dark": "#2B2D31"}
SLOTS = ["white", "dark"]
GROUPS = {"head_frame": [("head_frame", "white"), ("head_inlay", "dark")]}     # one object, two filaments
PLATES = [("body", "white", ["body", "arm_left", "arm_right"]),
          ("head", "white", ["head_frame"]),
          ("back", "white", ["head_back", "neck_plate"]),
          ("dark", "dark", ["ring", "puck", "door", "ups_slider"])]
SUPPORT = {"head_frame", "head_back"}   # tree supports from the build plate only
NO_NEST = {"head_frame", "head_back"}   # nothing inside their footprint (their supports go there)
BRIM = {"body": 5.0}


def load(name):
    """An object: [(part name, colour, triangles)], all shifted by the object's common minimum."""
    parts = GROUPS.get(name, [(name, None)])
    meshes = [(p, c, read_stl(HERE / "stl" / f"{p}.stl").astype(float)) for p, c in parts]
    lo = np.concatenate([m.reshape(-1, 3) for _, _, m in meshes]).min(0)
    return [(p, c, m - lo) for p, c, m in meshes]


def whole(obj):
    return np.concatenate([m for _, _, m in obj])


def footprint(name, tris, zmax=None, grow=0.0):
    m = raster(tris, zmax=zmax, grow=grow)
    if name in NO_NEST:
        from scipy.ndimage import binary_fill_holes
        m = binary_fill_holes(raster(tris, grow=grow))
    return m


def pack(names):
    """First object centred; the others placed one by one in free spots (inside hollow ones when low enough)."""
    N = int(BED / RES); e = int(EDGE / RES)
    objs = [load(n) for n in names]
    first = whole(objs[0]); hi = first.reshape(-1, 3).max(0)
    off = np.array([(BED - hi[0]) / 2, EDGE if names[0] in NO_NEST and len(names) > 1 else (BED - hi[1]) / 2, 0])   # a big solid one to an edge
    base = [(names[0], [(p, c, m + off) for p, c, m in objs[0]])]

    def occ_for(placed, h):
        occ = np.ones((N, N), bool); occ[e:N - e, e:N - e] = False
        for name, obj in placed:
            m = footprint(name, whole(obj), zmax=h + 1.0, grow=GAP)
            occ[:min(N, m.shape[0]), :min(N, m.shape[1])] |= m[:N, :N]
        return occ

    def place(i, placed):
        if i == len(objs) - 1:
            return placed
        obj = objs[i + 1]; name = names[i + 1]
        options = []
        for k in range(8):
            rparts = [(p, c, m) for p, c, m in obj]
            t_all = rot(whole(obj), k); shift = whole(obj)
            # rotate every part the same way, shifted as the whole
            a = np.radians(45 * k); cs, sn = np.cos(a), np.sin(a)
            R = lambda m: np.stack([cs * m[..., 0] - sn * m[..., 1], sn * m[..., 0] + cs * m[..., 1], m[..., 2]], -1)
            lo = R(shift).reshape(-1, 3).min(0)
            rparts = [(p, c, R(m) - [lo[0], lo[1], 0]) for p, c, m in obj]
            h = t_all[:, :, 2].max()
            spots = free_spots(occ_for(placed, h), footprint(name, t_all))
            if len(spots):
                for corner in (lambda s: s[:, 1] * N + s[:, 0], lambda s: -s[:, 1] * N + s[:, 0],
                               lambda s: s[:, 0] * N + s[:, 1], lambda s: -s[:, 0] * N - s[:, 1]):
                    ix, iy = spots[np.argmin(corner(spots))]
                    d = np.array([ix * RES - RES, iy * RES - RES, 0])
                    options.append([(p, c, m + d) for p, c, m in rparts])
        for o in options:
            r = place(i + 1, placed + [(name, o)])
            if r is not None:
                return r
        return None

    out = place(0, base)
    if out is None:
        raise SystemExit(f"{names}: they don't all fit on one plate")
    return out


def mesh_xml(oid, name, tris, centre):
    ids = vertex_ids(tris); verts = np.zeros((ids.max() + 1, 3)); verts[ids.reshape(-1)] = tris.reshape(-1, 3) - centre
    v = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in verts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in ids if len({a, b, c}) == 3)
    return f'<object id="{oid}" name="{name}" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>'


def project(plates, out):
    objects, items, obj_cfg, plate_cfg, assemble = [], [], [], [], []
    k = 0; nparts = 0
    cols = 2
    for i, (pname, colour, objs) in enumerate(plates):
        origin = np.array([(i % cols) * bp.STRIDE, -(i // cols) * bp.STRIDE, 0.0])
        inst = []
        for name, parts in objs:
            allp = whole(parts).reshape(-1, 3); lo, hi = allp.min(0), allp.max(0)
            assert lo[0] >= 0 and lo[1] >= 0 and hi[0] <= BED and hi[1] <= BED, (pname, name, lo, hi)
            centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
            part_ids, part_cfg = [], []
            for p, c, m in parts:
                k += 1; nparts += 1
                objects.append(mesh_xml(k, p, m, centre)); part_ids.append(k)
                ext = SLOTS.index(c or colour) + 1
                part_cfg.append(f'    <part id="{k}" subtype="normal_part">\n      <metadata key="name" value="{p}"/>\n'
                                f'      <metadata key="extruder" value="{ext}"/>\n'
                                f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n')
            if len(parts) == 1:
                top = part_ids[0]
            else:
                k += 1; top = k
                comps = "".join(f'<component objectid="{j}"/>' for j in part_ids)
                objects.append(f'<object id="{top}" name="{name}" type="model"><components>{comps}</components></object>')
            p3 = centre + origin
            tf = f"1 0 0 0 1 0 0 0 1 {p3[0]:.4f} {p3[1]:.4f} {p3[2]:.4f}"
            items.append(f'<item objectid="{top}" transform="{tf}" printable="1"/>')
            meta = [("name", name), ("extruder", SLOTS.index(colour) + 1)]
            meta += [("brim_type", "outer_only"), ("brim_width", BRIM[name])] if name in BRIM else [("brim_type", "no_brim")]
            if name in SUPPORT:
                meta += [("enable_support", 1), ("support_type", "tree(auto)"), ("support_on_build_plate_only", 1)]
            md = "".join(f'    <metadata key="{a}" value="{b}"/>\n' for a, b in meta)
            obj_cfg.append(f'  <object id="{top}">\n{md}{"".join(part_cfg)}  </object>\n')
            inst.append(f'    <model_instance>\n      <metadata key="object_id" value="{top}"/>\n      <metadata key="instance_id" value="0"/>\n'
                        f'      <metadata key="identify_id" value="{1000 + top}"/>\n    </model_instance>\n')
            assemble.append(f'   <assemble_item object_id="{top}" instance_id="0" transform="{tf}" offset="0 0 0"/>\n')
        plate_cfg.append(f'  <plate>\n    <metadata key="plater_id" value="{i + 1}"/>\n    <metadata key="plater_name" value="{i + 1} {pname} ({colour})"/>\n'
                         f'    <metadata key="locked" value="false"/>\n{"".join(inst)}  </plate>\n')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">\n'
             f' <metadata name="Application">{bp.APP}</metadata>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
             f' <metadata name="Title">LittleBot v4 Soft Arc</metadata>\n <resources>{"".join(objects)}</resources>\n <build>{"".join(items)}</build>\n</model>\n')
    config = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(obj_cfg) + "".join(plate_cfg) + "  <assemble>\n" + "".join(assemble) + "  </assemble>\n</config>\n"
    settings = bp.project_settings(["main", "black"], "A1")
    settings["filament_colour"] = [COLOURS[s] for s in SLOTS]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   '<Default Extension="config" ContentType="text/xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", config)
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=4))
    print(f"{out.name}: {len(plates)} plates, {sum(len(o) for _, _, o in plates)} objects, {nparts} parts")


def preview(plates, out):
    from PIL import Image, ImageDraw
    S = 2; W = int(BED * S)
    img = Image.new("RGB", (len(plates) * (W + 20) + 20, W + 60), "white"); d = ImageDraw.Draw(img)
    fill = {"white": (226, 222, 212), "dark": (60, 62, 68)}
    for i, (pname, colour, objs) in enumerate(plates):
        ox = 20 + i * (W + 20); d.rectangle([ox, 40, ox + W, 40 + W], outline=(0, 0, 0))
        d.text((ox, 12), f"plate {i + 1}: {pname} ({colour})", fill=(0, 0, 0))
        for name, parts in sorted(objs, key=lambda o: -whole(o[1])[:, :, 2].max()):
            for p, c, m in parts:
                ys, xs = np.nonzero(raster(m).T)
                for x, y in zip(xs[::2], ys[::2]):
                    d.point((ox + x * RES * S, 40 + W - y * RES * S), fill=fill[c or colour])
            allp = whole(parts).reshape(-1, 3); lo, hi = allp.min(0), allp.max(0)
            d.text((ox + (lo[0] + hi[0]) / 2 * S - 20, 40 + W - (lo[1] + hi[1]) / 2 * S), name, fill=(200, 30, 30))
    img.save(out)


if __name__ == "__main__":
    plates = [(pname, colour, pack(names)) for pname, colour, names in PLATES]
    project(plates, HERE / "LittleBot_v4_A1.3mf")
    preview(plates, HERE / "plates_v4.png")
