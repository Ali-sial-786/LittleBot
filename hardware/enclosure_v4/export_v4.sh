#!/usr/bin/env bash
# Export v4's printed parts (stl/, print orientation) and the assembled components (stl/assembled/).
#   OPENSCAD=... ./export_v4.sh          # then: python3 make_v4_project.py; python3 make_v4_assembly.py
set -euo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
mkdir -p stl stl/assembled
PRINT=(body ring neck_plate head_frame head_inlay head_back puck port_panel arm_left arm_right ups_slider)
ASSY=(ring body neck_plate ups_slider port_panel arm_left arm_right puck head_frame head_inlay head_back camera_bracket floor floor_clips
      coupler gears servo_frame servos pi_ups pca speaker mic aa_box charge_socket screen screen_plugs eyes camera)
one() { "$OPENSCAD" -q -o "$3/$2.stl" -D "part=\"$1\"" -D "which=\"$2\"" littlebot_v4.scad 2>&1 | grep -v -i warning || true; }
export -f one; export OPENSCAD
{ for w in "${PRINT[@]}"; do echo "print4 $w stl"; done; for w in "${ASSY[@]}"; do echo "assy4 $w stl/assembled"; done; } |
    xargs -P "${JOBS:-4}" -L 1 bash -c 'one "$@"' _
for w in "${PRINT[@]}"; do python3 ../enclosure_v2/stl_tools.py binary stl/$w.stl; done
python3 ../enclosure_v2/stl_tools.py report $(for w in "${PRINT[@]}"; do echo stl/$w.stl; done) || true
