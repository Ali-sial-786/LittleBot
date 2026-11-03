#!/usr/bin/env python3
"""Combine plate files (pack_plates.py) into ONE Bambu Studio project: one plate per file, set up for a
Bambu Lab P2S or A1 (0.4 nozzle, 0.20mm Standard, Bambu PLA Basic, Textured PEI Plate).

    python3 bambu_project.py OUT.3mf plates_256/07_main_P2S.3mf               # P2S (default)
    python3 bambu_project.py --printer A1 OUT.3mf plates_256/08_black_A1.3mf plates_256/09_main_A1.3mf
    python3 bambu_project.py --fetch [P2S|A1]   # re-make bambu_<printer>_pla.json from Bambu's own profiles (GitHub)

Each plate file becomes a plate; each colour gets its own filament slot (main, black, white...), so the parts
show in their colours; when printing, Bambu Studio maps each slot to an AMS slot. Brims follow pack_plates.py:
an 8 mm outer brim on tall parts only, none on the others. The head gets supports on the build plate only.

Bambu Studio only keeps a project's plates if the project carries its printer settings. It selects its
own installed presets by name ("Bambu Lab P2S 0.4 nozzle" ...) and takes every value from them, so the
settings stored here (bambu_p2s_pla.json / bambu_a1_pla.json, Bambu's profiles merged along their
"inherits" and "include" chains, one value per nozzle variant) only matter if those presets are missing.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from pack_plates import BRIM, read_3mf, tall
from stl_tools import vertex_ids

HERE = Path(__file__).resolve().parent
BED = 256.0                                  # P2S and A1
STRIDE = BED * 1.2                           # Bambu Studio lays plates out 20 % apart (PartPlate.cpp)
APP = "BambuStudio-02.02.00.00"              # 2.0 or later: no old-file conversions; older than current releases
PRESETS = {"P2S": ("Bambu Lab P2S 0.4 nozzle", "0.20mm Standard @BBL P2S", "Bambu PLA Basic @BBL P2S"),
           "A1": ("Bambu Lab A1 0.4 nozzle", "0.20mm Standard @BBL A1", "Bambu PLA Basic @BBL A1")}
settings_file = lambda printer: HERE / f"bambu_{printer.lower()}_pla.json"
COLOURS = {"main": "#F5F0E6", "black": "#161616", "glow": "#FFFFFF"}
NAMES = {"main": "main colour", "black": "black", "glow": "white / translucent"}
BRIMMED = {"base", "head", "torso"}         # the README's tall parts (the base is wide, but 72 mm tall; 20 mm from the edges)
SMALL_BRIM = {"camera": 5.0}                 # the camera bracket stands 36 mm tall on a 38 x 5 mm footprint
# the head prints face down: its visor seat needs supports, from the bed only (README, Material and settings)
SUPPORT = {"head": '    <metadata key="enable_support" value="1"/>\n    <metadata key="support_on_build_plate_only" value="1"/>\n'}
PROFILES = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/profiles/"


def fetch(printer: str = "P2S") -> None:
    """Merge Bambu's printer, process and filament profiles into bambu_<printer>_pla.json."""
    PRINTER, PROCESS, FILAMENT = PRESETS[printer]; SETTINGS = settings_file(printer)
    get = lambda path: json.loads(urllib.request.urlopen(PROFILES + urllib.parse.quote(path)).read())
    index = get("BBL.json")
    paths = {e["name"]: e["sub_path"] for sec in ("machine_list", "process_list", "filament_list") for e in index[sec]}
    src = PROFILES.replace("raw.githubusercontent.com", "github.com").replace("/master/", "/blob/master/")
    # which settings hold one value per nozzle variant (Bambu Studio's own lists)
    code = urllib.request.urlopen("https://raw.githubusercontent.com/bambulab/BambuStudio/master/src/libslic3r/PrintConfig.cpp").read().decode("utf-8", "replace")
    def keyset(name):
        return set(re.findall(r'"([^"]+)"', re.search(r"std::set<std::string>\s+" + name + r"\s*=\s*\{(.*?)\};", code, re.S).group(1)))
    one = keyset("print_options_with_variant") | keyset("printer_options_with_variant_1") | keyset("filament_options_with_variant")
    two = keyset("printer_options_with_variant_2")          # machine limits: normal + silent per variant
    meta = {"type", "name", "from", "instantiation", "inherits", "include", "setting_id", "description", "renamed_from",
            "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
            "print_extruder_id", "print_extruder_variant", "printer_extruder_id", "printer_extruder_variant",
            "filament_extruder_variant", "filament_self_index", "extruder_variant_list"}
    cache: dict[str, dict] = {}
    def own(name):
        if name not in cache: cache[name] = get("BBL/" + paths[name])
        return cache[name]
    def flat(name):                                           # parent, then includes, then the profile's own keys
        d = own(name); out = flat(d["inherits"]) if d.get("inherits") else {}
        for inc in d.get("include", []): out.update({k: v for k, v in own(inc).items() if k not in meta})
        out.update({k: v for k, v in d.items() if k not in meta}); return out
    def collapse(cfg):                                        # one value per setting: the standard nozzle's; AMS drying settings
        return {k: (v[:1] if k in one and isinstance(v, list) else v[:2] if k in two and isinstance(v, list) else v)   # (several values
                for k, v in cfg.items() if not k.startswith("filament_dev_")}                                          # per filament) stay out
    printer, process, filament = (collapse(flat(n)) for n in (PRINTER, PROCESS, FILAMENT))
    SETTINGS.write_text(json.dumps({"_source": src, "_profiles_version": index["version"], "printer": printer, "process": process,
                                    "filament": filament, "filament_id": own(FILAMENT).get("filament_id") or flat(FILAMENT).get("filament_id", "")},
                                   indent=1, sort_keys=True))
    print(f"{SETTINGS.name}: printer {len(printer)}, process {len(process)}, filament {len(filament)} settings (profiles {index['version']})")


def project_settings(groups: list[str], printer: str) -> dict:
    PRINTER, PROCESS, FILAMENT = PRESETS[printer]
    s = json.loads(settings_file(printer).read_text())
    n = len(groups)
    cfg = {**s["process"], **s["printer"]}
    for k, v in s["filament"].items():                        # filament settings: one value per filament slot
        cfg[k] = v * n if isinstance(v, list) else v
    cfg.update({"printer_settings_id": PRINTER, "print_settings_id": PROCESS, "filament_settings_id": [FILAMENT] * n,
                "filament_colour": [COLOURS[g] for g in groups], "filament_ids": [s["filament_id"]] * n,
                "inherits_group": [""] * (n + 2), "different_settings_to_system": [""] * (n + 2),
                "curr_bed_type": "Textured PEI Plate",        # the P2S's and A1's plate; the default (Cool Plate) isn't supported
                "from": "project", "name": "project_settings", "version": APP.split("-", 1)[1]})
    return cfg


def build(out: Path, plate_files: list[Path], printer: str = "P2S") -> None:
    r = np.sqrt(len(plate_files)); cols = int(round(r)) + (1 if r > round(r) else 0)    # compute_colum_count() in PartPlate.hpp
    groups = [f.stem.split("_")[1] for f in plate_files]                                # NN_group[_printer]
    colours = list(dict.fromkeys(groups))                    # one filament slot per colour
    objects, items, obj_cfg, plates, assemble = [], [], [], [], []
    k = 0
    for i, f in enumerate(plate_files):
        origin = np.array([(i % cols) * STRIDE, -(i // cols) * STRIDE, 0.0])
        inst = []
        for name, tris in read_3mf(f):
            k += 1
            lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
            assert lo[0] >= 0 and lo[1] >= 0 and hi[0] <= BED and hi[1] <= BED and abs(lo[2]) < 1e-6, (f.name, name)
            centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
            ids = vertex_ids(tris); verts = np.zeros((ids.max() + 1, 3)); verts[ids.reshape(-1)] = tris.reshape(-1, 3) - centre
            v = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in verts)
            t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in ids if len({a, b, c}) == 3)
            label = f"{name} ({NAMES[groups[i]]})"
            objects.append(f'<object id="{k}" name="{label}" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>')
            p = centre + origin
            tf = f"1 0 0 0 1 0 0 0 1 {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}"
            items.append(f'<item objectid="{k}" transform="{tf}" printable="1"/>')
            width = BRIM if tall(hi - lo) or name in BRIMMED else SMALL_BRIM.get(name)
            brim = (f'    <metadata key="brim_type" value="outer_only"/>\n    <metadata key="brim_width" value="{width:g}"/>\n' if width
                    else '    <metadata key="brim_type" value="no_brim"/>\n')        # as pack_plates.py: a brim near the edge lifts the part
            obj_cfg.append(f'  <object id="{k}">\n    <metadata key="name" value="{label}"/>\n    <metadata key="extruder" value="{colours.index(groups[i]) + 1}"/>\n{brim}{SUPPORT.get(name, "")}'
                           f'    <part id="{k}" subtype="normal_part">\n      <metadata key="name" value="{label}"/>\n'
                           f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n  </object>\n')
            inst.append(f'    <model_instance>\n      <metadata key="object_id" value="{k}"/>\n      <metadata key="instance_id" value="0"/>\n'
                        f'      <metadata key="identify_id" value="{100 + k}"/>\n    </model_instance>\n')
            assemble.append(f'   <assemble_item object_id="{k}" instance_id="0" transform="{tf}" offset="0 0 0"/>\n')
        plates.append(f'  <plate>\n    <metadata key="plater_id" value="{i + 1}"/>\n    <metadata key="plater_name" value="{f.stem} ({NAMES[groups[i]]})"/>\n'
                      f'    <metadata key="locked" value="false"/>\n{"".join(inst)}  </plate>\n')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">\n'
             f' <metadata name="Application">{APP}</metadata>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
             f' <metadata name="Title">LittleBot v2 {out.stem}</metadata>\n'
             f' <resources>{"".join(objects)}</resources>\n <build>{"".join(items)}</build>\n</model>\n')
    config = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(obj_cfg) + "".join(plates)
              + "  <assemble>\n" + "".join(assemble) + "  </assemble>\n</config>\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   '<Default Extension="config" ContentType="text/xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                   '</Relationships>')
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", config)
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings(colours, printer), indent=4))
    print(f"{out}: {printer}, {len(plate_files)} plates, {k} parts ({', '.join(f.stem for f in plate_files)})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--fetch"]:
        fetch(*(args[1:2] or ["P2S"]))
    else:
        printer = "P2S"
        if args[:1] == ["--printer"]:
            printer, args = args[1], args[2:]
        build(Path(args[0]), [Path(a) for a in args[1:]], printer)
