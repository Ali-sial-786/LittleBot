#!/usr/bin/env bash
# Render every printable LittleBot v2 part to stl/ and the preview images to images/.
#   ./build.sh            # all parts + images
#   ./build.sh head arm_left   # just these parts
set -euo pipefail
cd "$(dirname "$0")"
SCAD=${SCAD:-littlebot_v2.scad}
OUT=${OUT:-stl}
IMG=${IMG:-images}
PARTS=(base base_floor base_floor_mobile shelf well_cover front_blank front_sonar rear_blank rear_usb
       torso torso_back coupler ring_gear pinion servo_frame
       head head_back visor ear_left ear_right camera_pod camera pi_carrier arm_left arm_right)
(($#)) && PARTS=("$@")
mkdir -p "$OUT" "$IMG"
render() { openscad -q -o "$OUT/$1.stl" -D "part=\"$1\"" "$SCAD" && python3 stl_tools.py binary "$OUT/$1.stl" && echo "==> $1"; }
export -f render; export OUT SCAD
printf '%s\n' "${PARTS[@]}" | xargs -P "${JOBS:-4}" -I{} bash -c 'render {}'
files=(); for p in "${PARTS[@]}"; do files+=("$OUT/$p.stl"); done
status=0; python3 stl_tools.py report "${files[@]}" || status=1
(($# == 0)) && { python3 make_plates.py || status=1; }   # group the parts into build plates
if (($# == 0)) && command -v xvfb-run >/dev/null; then
    shot() { xvfb-run -a -s "-screen 0 1600x1200x24" openscad -q -o "$IMG/$1.png" -D "part=\"$2\"" -D "section_cut=${4:-0}" \
             --imgsize=1100,1300 --camera="$3" --colorscheme=Tomorrow --projection="${5:-p}" "$SCAD"; }
    shot front assembly "0,76,165,72,0,25,1250" &
    shot back assembly "0,76,165,68,0,210,1250" &
    shot side assembly "0,76,165,90,0,90,1150" 0 o &
    shot exploded exploded "0,76,200,72,0,30,1600" &
    wait
    shot section_side section_side "0,76,165,90,0,90,1150" 0 o &
    shot section_plan section_plan "0,76,60,0,0,0,700" 150 o &
    wait
fi
exit $status
