#!/usr/bin/env bash
# Render the v5 assembly video to ../LittleBot_v5_assembly.mp4:
#   OPENSCAD=... ./make_video.sh
# Needs OpenSCAD, Python 3 + numpy, Node + Playwright's Chromium, ffmpeg, and the assembled parts from ../export_v5.sh
# (stl/assembled). The screws, nuts, cable routes and part positions come from the model (video5.scad -> hardware5.json);
# the animation is index.html (plain WebGL, no libraries); render.js steps it frame by frame in headless Chromium.
set -euo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
mkdir -p build/stl
"$OPENSCAD" -o build/hw5.echo -D 'part="hw5"' video5.scad 2>/dev/null
python3 -c 'import json, re, sys
d = json.loads(re.search(r"ECHO: hw5 = (.*)", open(sys.argv[1]).read()).group(1))
json.dump(d, open(sys.argv[2], "w"), separators=(",", ":"), ensure_ascii=False)' build/hw5.echo hardware5.json
SMALL=(ring_gear pinion servo_head servo_l servo_r horn_head horn_l horn_r fpc usbc_plug coupler4 hdmi_plug mic_plug eyes
       pod_l pod_r motor_l motor_r wheel_l wheel_r caster_f caster_r pi_ups)
one() { "$OPENSCAD" -q -o "build/stl/$1.stl" -D 'part="vid5"' -D "which=\"$1\"" video5.scad 2>&1 | grep -v -i warning || true; }
export -f one; export OPENSCAD
if [ -z "${SKIP_STL:-}" ]; then printf '%s\n' "${SMALL[@]}" | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}'; fi
python3 place_parts.py build
cp index.html render.js build/
rm -rf build/frames
NODE_PATH=${NODE_PATH:-$(npm root -g)} node build/render.js "$PWD/build/frames" 30 "${WORKERS:-3}"
ffmpeg -loglevel error -y -framerate 30 -i build/frames/f%05d.jpg -c:v libx264 -preset slow -crf 23 -pix_fmt yuv420p \
       -movflags +faststart build/LittleBot_v5_assembly.mp4
cp build/LittleBot_v5_assembly.mp4 ../LittleBot_v5_assembly.mp4
echo "==> ../LittleBot_v5_assembly.mp4"
