#!/usr/bin/env bash
# Render every printable LittleBot part to stl/ and the preview images to images/.
#   ./build.sh            # all parts + images
#   ./build.sh head arm   # just these parts
set -euo pipefail
cd "$(dirname "$0")"
SCAD=${SCAD:-littlebot_enclosure.scad}
OUT=${OUT:-stl}
IMG=${IMG:-images}
PARTS=(body base head head_back coupler ring_gear pinion arm servo_frame camera lcd_tabs)
(($#)) && PARTS=("$@")
mkdir -p "$OUT" "$IMG"
for p in "${PARTS[@]}"; do
    echo "==> $p"
    openscad -q -o "$OUT/$p.stl" -D "part=\"$p\"" "$SCAD"
    python3 stl_tools.py binary "$OUT/$p.stl"
done
python3 stl_tools.py report "$OUT"/*.stl
if (($# == 0)) && command -v xvfb-run >/dev/null; then
    shot() { xvfb-run -a -s "-screen 0 1600x1200x24" openscad -q -o "$IMG/$1.png" -D "part=\"$2\"" -D "section_cut=${4:-0}" \
             --imgsize=1300,1000 --camera="$3" --colorscheme=Tomorrow --projection="${5:-p}" "$SCAD"; }
    shot front assembly "0,63,105,70,0,25,600"
    shot back assembly "0,63,105,62,0,215,560"
    shot exploded exploded "0,63,140,70,0,30,820"
    shot section_side section_side "0,63,105,90,0,90,520" 0 o
    shot section_plan section_plan "0,63,40,0,0,0,420" 80 o
fi
