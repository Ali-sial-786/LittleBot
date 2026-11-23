#!/usr/bin/env python3
"""LittleBot v5 "Rover": pack the new printed parts onto 256 mm plates and write ONE Bambu Studio project
for each printer (Bambu Lab A1 and P2S: 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic, Textured PEI plate).

    ./export_v5.sh && python3 make_v5_project.py      # reads stl/*.stl, writes LittleBot_v5_<A1|P2S|plain>.3mf, plates_v5.png and
                                                      # LittleBot_v5_test_<A1|P2S|plain>.3mf (two short test pieces, a plate each) and
                                                      # LittleBot_v5_pods_<A1|P2S|plain>.3mf (the 2 wheel pods alone),
                                                      # LittleBot_v5_keeper_<...>.3mf, LittleBot_v5_neck_<...>.3mf (one part each)

Plates (v4's packer and project writer, with v5's parts):
  1  dark:  the tub (floor down)
  2  white: the body (upright, tongue down, 8 mm brim inside and out), with the cap inside its footprint
  3  dark:  the hub, the black neck
  4  white: the mic holder + key, the speaker keeper, the slider, the stack keeper
  5  dark:  both wheel pods, the battery drawer, the deck (print these once the bought parts are measured)
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
# the body stands on its 4 mm tongue: a brim inside and out holds it down; the tub's big flat floor gets one against lifting corners
m4.BRIM = {"body": (8.0, "outer_and_inner"), "test_body_base": (8.0, "outer_and_inner"), "tub": 5.0, "test_tub_corner": 5.0, "tray": 3.0}
# solid walls: v5's walls are 3 mm, and the default 2 loops a side (1.74 mm) left a 1.3 mm core of 15 % infill between two
# thin skins. The first body printed that way split along a layer. 4 loops a side fill a 3 mm wall solid.
m4.EXTRA = {n: {"wall_loops": 4} for n in ("body", "tub", "cap", "hub", "neck", "pod_left", "pod_right", "tray", "deck", "test_body_base", "test_tub_corner")}
# the stack keeper (v4's broke at a screw): 5 loops and 40 % infill
m4.EXTRA["stack_keeper"] = {"wall_loops": 5, "sparse_infill_density": "40%"}
PLATES = [("tub", "dark", ["tub"]),
          ("body", "white", ["body", "cap"]),
          ("neck", "dark", ["neck", "hub"]),
          ("small", "white", ["mic_holder", "spk_cage", "slider", "mic_key", "stack_keeper"]),
          ("drive", "dark", ["deck", "tray", "pod_left", "pod_right"])]          # last: print once the motors, casters and AA holder are measured

# the test pieces, each in its real part's colour: the body's first 22 mm, a corner of the tub (floor, wall, the door's post)
TESTS = [("test body", "white", ["test_body_base"]), ("test tub", "dark", ["test_tub_corner"])]
# the wheel pods on their own (reprinted with room for the kits' 67 mm wheels: no roof, walls further out)
PODS = [("wheel pods", "dark", ["pod_left", "pod_right"])]
# the stack keeper on its own (v5's: stronger, with legs down the stack's ends)
KEEPER = [("stack keeper", "white", ["stack_keeper"])]
# the black neck on its own (the first one was exported as its mirror image: its holes came out 20 degrees off the hub's)
NECK = [("black neck", "dark", ["neck"])]
# the speaker's cage on its own: cradle, keeper and top in one piece (the speaker is a pill: its round ends slip past the ribs' ledges)
CAGE = [("speaker cage", "white", ["spk_cage"])]

if __name__ == "__main__":
    plates = [(pname, colour, m4.pack(names)) for pname, colour, names in PLATES]
    tests = [(pname, colour, m4.pack(names)) for pname, colour, names in TESTS]
    pods = [(pname, colour, m4.pack(names)) for pname, colour, names in PODS]
    keeper = [(pname, colour, m4.pack(names)) for pname, colour, names in KEEPER]
    neck = [(pname, colour, m4.pack(names)) for pname, colour, names in NECK]
    cage = [(pname, colour, m4.pack(names)) for pname, colour, names in CAGE]
    for printer in ("A1", "P2S", None):          # None: "plain", no printer or filament settings of ours (Bambu Studio keeps its own)
        tag = printer or "plain"
        m4.project(plates, HERE / f"LittleBot_v5_{tag}.3mf", "LittleBot v5 Rover", printer)
        m4.project(tests, HERE / f"LittleBot_v5_test_{tag}.3mf", "LittleBot v5 test pieces", printer)
        m4.project(pods, HERE / f"LittleBot_v5_pods_{tag}.3mf", "LittleBot v5 wheel pods", printer)
        m4.project(keeper, HERE / f"LittleBot_v5_keeper_{tag}.3mf", "LittleBot v5 stack keeper", printer)
        m4.project(neck, HERE / f"LittleBot_v5_neck_{tag}.3mf", "LittleBot v5 black neck", printer)
        m4.project(cage, HERE / f"LittleBot_v5_speaker_cage_{tag}.3mf", "LittleBot v5 speaker cage", printer)
    m4.preview(plates, HERE / "plates_v5.png")
