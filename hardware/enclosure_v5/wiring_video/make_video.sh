#!/usr/bin/env bash
# Render the narrated wiring video to ../LittleBot_v5_wiring.mp4:
#   PIPER=/path/to/piper VOICE=/path/to/en_US-lessac-medium.onnx ./make_video.sh
# Needs Piper TTS (pip install piper-tts) and its en_US-lessac-medium voice, Node + Playwright's Chromium, ffmpeg.
# steps.json is the script: what each step says and shows. wiring.html draws it (2D canvas); render.js steps it frame by frame.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
python3 tts.py build                                    # build/audio/*.wav, build/timeline.json, build/narration.wav
cp wiring.html steps.json render.js deck_view.png robot_front.png build/
rm -rf build/frames
NODE_PATH=${NODE_PATH:-$(npm root -g)} node build/render.js "$PWD/build/frames" 30 "${WORKERS:-6}"
ffmpeg -loglevel error -y -framerate 30 -i build/frames/f%05d.jpg -i build/narration.wav -c:v libx264 -preset slow -crf 22 \
       -pix_fmt yuv420p -c:a aac -b:a 128k -shortest -movflags +faststart build/LittleBot_v5_wiring.mp4
cp build/LittleBot_v5_wiring.mp4 ../LittleBot_v5_wiring.mp4
echo "==> ../LittleBot_v5_wiring.mp4"
