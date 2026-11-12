#!/usr/bin/env python3
"""LittleBot v4 "Soft Arc" fully assembled, as ONE coloured 3MF (to look at, not to print):

    python3 make_v4_assembly.py         # reads stl/assembled/*.stl (part="assy4" exports, export_v4.sh), writes LittleBot_v4_assembled.3mf

One object made of parts (so it stays assembled in Bambu Studio), each part coloured: 3MF base materials for any
viewer (Windows 3D Viewer, PrusaSlicer...) and one filament colour per part colour for Bambu Studio.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "enclosure_v2"))
from stl_tools import read_stl, vertex_ids          # noqa: E402
import bambu_project as bp                            # noqa: E402

COLOURS = {   # name: hex
    "white": "#F2EFE8", "dark": "#2B2D31", "v2": "#E3DED2", "metal": "#9AA0A8", "black": "#141518",
    "servo": "#3A6FB0", "board": "#2E7D4F", "eye": "#4FE3E0"}
PARTS = [  # (stl name, colour, label)
    ("ring", "dark", "base ring"), ("body", "white", "body"), ("neck_plate", "white", "neck plate"),
    ("ups_slider", "white", "UPS switch slider"), ("port_panel", "dark", "port panel (USB-C + UPS switch)"),
    ("arm_left", "white", "left arm"), ("arm_right", "white", "right arm"), ("puck", "dark", "black neck"),
    ("head_frame", "white", "head frame"), ("head_inlay", "dark", "camera arch"), ("head_back", "white", "head back"),
    ("camera_bracket", "v2", "camera bracket (v2)"), ("floor", "v2", "floor (v2)"), ("floor_clips", "v2", "stack keeper + PCA clamps (v2)"),
    ("coupler", "v2", "neck coupler (v2)"), ("gears", "v2", "neck gears (v2)"), ("servo_frame", "v2", "servo frame (v2)"),
    ("servos", "servo", "MG90S servos"), ("pi_ups", "board", "Pi 5 + UPS HAT (E)"), ("pca", "board", "PCA9685"),
    ("speaker", "black", "USB speaker"), ("respeaker", "black", "ReSpeaker Lite (2 mics)"), ("mic_holder", "white", "ReSpeaker holder (in the chest)"), ("mic_key", "white", "ReSpeaker holder's key (in the old mic pocket)"), ("spk_keeper", "white", "speaker keeper (behind the speaker)"), ("aa_box", "black", "4xAA box (in the head)"), ("charge_socket", "metal", "USB-C charging socket + cable"),
    ("screen", "black", "7in screen (EP-0177)"), ("screen_plugs", "metal", "HDMI ribbon plug, USB-C, HDMI coupler"),
    ("eyes", "eye", "eyes"), ("camera", "black", "camera"),
]


def main(out: Path) -> None:
    names = list(COLOURS)
    meshes, objs = [], []
    allpts = []
    for stl, colour, label in PARTS:
        t = read_stl(HERE / "stl" / "assembled" / f"{stl}.stl").astype(float)
        meshes.append((stl, colour, label, t)); allpts.append(t.reshape(-1, 3))
    pts = np.concatenate(allpts); lo, hi = pts.min(0), pts.max(0)
    shift = np.array([128 - (lo[0] + hi[0]) / 2, 128 - (lo[1] + hi[1]) / 2, -lo[2]])   # centred on a 256 mm bed, standing on it
    base = "".join(f'<base name="{n}" displaycolor="{c}FF"/>' for n, c in COLOURS.items())
    res = [f'<basematerials id="1">{base}</basematerials>']
    for i, (stl, colour, label, t) in enumerate(meshes, start=2):
        ids = vertex_ids(t); verts = np.zeros((ids.max() + 1, 3)); verts[ids.reshape(-1)] = t.reshape(-1, 3) + shift
        v = "".join(f'<vertex x="{a:.3f}" y="{b:.3f}" z="{c:.3f}"/>' for a, b, c in verts)
        f = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in ids if len({a, b, c}) == 3)
        res.append(f'<object id="{i}" name="{label}" type="model" pid="1" pindex="{names.index(colour)}"><mesh><vertices>{v}</vertices><triangles>{f}</triangles></mesh></object>')
    top = len(meshes) + 2
    comps = "".join(f'<component objectid="{i}"/>' for i in range(2, top))
    res.append(f'<object id="{top}" name="LittleBot v4 (assembled)" type="model"><components>{comps}</components></object>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">\n'
             f' <metadata name="Application">{bp.APP}</metadata>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
             ' <metadata name="Title">LittleBot v4 Soft Arc assembled</metadata>\n'
             f' <resources>{"".join(res)}</resources>\n <build><item objectid="{top}" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/></build>\n</model>\n')
    parts = "".join(f'    <part id="{i}" subtype="normal_part">\n      <metadata key="name" value="{label}"/>\n'
                    f'      <metadata key="extruder" value="{names.index(colour) + 1}"/>\n'
                    f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n'
                    for i, (stl, colour, label, t) in enumerate(meshes, start=2))
    config = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
              f'  <object id="{top}">\n    <metadata key="name" value="LittleBot v4 (assembled)"/>\n    <metadata key="extruder" value="1"/>\n{parts}  </object>\n'
              f'  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value="assembled"/>\n    <metadata key="locked" value="false"/>\n'
              f'    <model_instance>\n      <metadata key="object_id" value="{top}"/>\n      <metadata key="instance_id" value="0"/>\n'
              f'      <metadata key="identify_id" value="{top + 100}"/>\n    </model_instance>\n  </plate>\n'
              f'  <assemble>\n   <assemble_item object_id="{top}" instance_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0"/>\n  </assemble>\n</config>\n')
    settings = bp.project_settings(["main"] * len(names), "A1")
    settings["filament_colour"] = [COLOURS[n] for n in names]
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
    print(f"{out.name}: {len(meshes)} parts, {hi[0] - lo[0]:.0f} x {hi[1] - lo[1]:.0f} x {hi[2] - lo[2]:.0f} mm")


if __name__ == "__main__":
    main(HERE / "LittleBot_v4_assembled.3mf")
