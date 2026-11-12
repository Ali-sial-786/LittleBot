#!/usr/bin/env python3
"""LittleBot v5 "Rover": pack the new printed parts onto 256 mm plates and write ONE Bambu Studio project
(Bambu Lab A1, 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic, Textured PEI plate).

    ./export_v5.sh && python3 make_v5_project.py      # reads stl/*.stl, writes LittleBot_v5_A1.3mf + plates_v5.png

Plates (v4's packer and project writer, with v5's parts):
  1  dark:  the tub (floor down)
  2  white: the body (upright, tongue down, 5 mm brim), with the cap inside its footprint
  3  dark:  both wheel pods, the battery drawer, the deck, the hub, the black neck
  4  white: the mic holder + key, the speaker keeper, the slider
Filament slots: 1 white, 2 dark grey.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "enclosure_v4"))
import make_v4_project as m4                                   # noqa: E402

m4.HERE = HERE
m4.GROUPS = {}
m4.SOURCE = {}
m4.SUPPORT = {"mic_holder"}             # tree supports from the build plate under its curved base; nothing else needs any
m4.NO_NEST = {"tub"}
m4.BRIM = {"body": 5.0, "tray": 3.0}
PLATES = [("tub", "dark", ["tub"]),
          ("body", "white", ["body", "cap"]),
          ("drive", "dark", ["deck", "tray", "pod_left", "pod_right", "neck", "hub"]),
          ("small", "white", ["mic_holder", "spk_keeper", "slider", "mic_key"])]

if __name__ == "__main__":
    plates = [(pname, colour, m4.pack(names)) for pname, colour, names in PLATES]
    m4.project(plates, HERE / "LittleBot_v5_A1.3mf", "LittleBot v5 Rover")
    m4.preview(plates, HERE / "plates_v5.png")
