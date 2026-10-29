#!/usr/bin/env bash
# Render the assembly animation to build/LittleBot_assembly.mp4 (needs Python 3 + numpy, Node + Playwright's
# Chromium, ffmpeg). The animation itself is index.html (plain WebGL, no libraries); render.js steps
# it frame by frame in headless Chromium.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
python3 place_parts.py build/meshes
cp index.html render.js build/
NODE_PATH=${NODE_PATH:-$(npm root -g)} node build/render.js "$PWD/build/frames" 30
ffmpeg -loglevel error -y -framerate 30 -i build/frames/f%05d.jpg -c:v libx264 -preset slow -crf 21 -pix_fmt yuv420p \
       -movflags +faststart build/LittleBot_assembly.mp4
echo "==> build/LittleBot_assembly.mp4"
