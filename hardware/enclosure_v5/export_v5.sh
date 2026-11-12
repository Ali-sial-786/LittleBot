#!/usr/bin/env bash
# Export v5's printed parts (stl/, print orientation) and the assembled components (stl/assembled/).
#   OPENSCAD=... ./export_v5.sh          # then: python3 make_v5_project.py; python3 make_v5_assembly.py
set -euo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
mkdir -p stl stl/assembled
PRINT=(tub pod_left pod_right tray deck body cap hub neck slider mic_holder mic_key spk_keeper)
ASSY=(tub pods tray deck body cap slider hub neck gears servo_frame servos arm_left arm_right head_frame head_inlay head_back camera_bracket
      screen screen_plugs eyes mic_holder mic_key spk_keeper respeaker speaker pi_ups stack_keeper motors wheels casters aa_holder switch boards charge_socket)
one() { "$OPENSCAD" -q -o "$3/$2.stl" -D "part=\"$1\"" -D "which=\"$2\"" littlebot_v5.scad 2>&1 | grep -v -i warning || true; }
export -f one; export OPENSCAD
{ for w in "${PRINT[@]}"; do echo "print5 $w stl"; done; for w in "${ASSY[@]}"; do echo "assy5 $w stl/assembled"; done; } |
    xargs -P "${JOBS:-4}" -L 1 bash -c 'one "$@"' _
for w in "${PRINT[@]}"; do python3 ../enclosure_v2/stl_tools.py binary stl/$w.stl; done
python3 -c 'import sys; sys.path.insert(0, "../enclosure_v2"); from pathlib import Path; from stl_tools import read_stl, write_binary
p = Path("stl/mic_holder.stl"); t = read_stl(p); t[..., 2] -= t[..., 2].min(); write_binary(p, t)'   # its base is curved: onto the bed
python3 ../enclosure_v2/stl_tools.py report $(for w in "${PRINT[@]}"; do echo stl/$w.stl; done) || true
