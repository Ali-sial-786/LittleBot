#!/bin/bash
# Downloads what the wake-word training needs into ~/wwtrain/data (about 19 GB; run setup.sh first).
# Safe to run again: finished files are skipped and a cut-off download carries on where it stopped.
#
#   bash scripts/wake_training/download.sh
#
# What and from where (all public, for research and training):
#   openwakeword_features_ACAV100M_2000_hrs_16bit.npy  17.3 GB  ~2,000 hours of everyday speech, music and
#       noise, already turned into openWakeWord's features: what the wake word must NOT react to
#       (huggingface.co/datasets/davidscripka/openwakeword_features)
#   validation_set_features.npy  0.2 GB  ~11 hours more, kept apart to count false alarms per hour
#   16khz/*.wav  8 MB  270 room echoes (MIT environmental impulse responses), to make the made-up
#       voices sound like a real room (huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses)
#   AudioSet, 2 parts  1.4 GB  ~1,100 ten-second YouTube clips (talk, TV, music, kitchens ...) mixed
#       under the voices as background (huggingface.co/datasets/agkphysics/AudioSet)
#   en_US-libritts_r-medium.pt  0.2 GB  the voice generator: 904 different English speakers
#       (github.com/rhasspy/piper-sample-generator, release v2.0.0)
#   melspectrogram.onnx, embedding_model.onnx  openWakeWord's own feature models (release v0.5.1)
set -euo pipefail

ROOT="${WWTRAIN:-$HOME/wwtrain}"
DATA="$ROOT/data"
mkdir -p "$DATA/rirs" "$DATA/audioset" "$DATA/background"
cd "$DATA" || exit 1
log() { echo "[$(date '+%F %T')] $*"; }

HF=https://huggingface.co/datasets
# fetch URL FILE SIZE: download (resuming) until the file has exactly SIZE bytes.
fetch() {
  local url="$1" out="$2" size="$3" have=0 tries=0
  while :; do
    have=$(stat -c %s "$out" 2>/dev/null || echo 0)
    if [ "$have" = "$size" ]; then return 0; fi
    if [ "$have" -gt "$size" ]; then rm -f "$out"; fi
    tries=$((tries + 1))
    if [ "$tries" -gt 30 ]; then log "giving up on $out"; return 1; fi
    curl -sSfL -C - --retry 5 --retry-delay 10 --speed-time 60 --speed-limit 10000 -o "$out" "$url" ||
      { log "  $out: stopped at $(stat -c %s "$out" 2>/dev/null || echo 0) bytes, trying again"; sleep 10; }
  done
}

log "feature models"
MODELS="$ROOT/openWakeWord/openwakeword/resources/models"
mkdir -p "$MODELS"
for m in melspectrogram.onnx embedding_model.onnx; do
  [ -s "$MODELS/$m" ] || curl -sSfL -o "$MODELS/$m" "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$m"
done

log "voice generator"
GEN="$ROOT/piper-sample-generator/models/en_US-libritts_r-medium.pt"
[ -s "$GEN" ] || curl -sSfL -o "$GEN" \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt

log "room echoes"
curl -sSf "https://huggingface.co/api/datasets/davidscripka/MIT_environmental_impulse_responses/tree/main/16khz" |
  "$ROOT/venv/bin/python" -c 'import json,sys; [print(f["path"]) for f in json.load(sys.stdin) if f["path"].endswith(".wav")]' |
  while read -r p; do
    f="rirs/$(basename "$p")"
    [ -s "$f" ] || curl -sSfL -o "$f" "$HF/davidscripka/MIT_environmental_impulse_responses/resolve/main/$p"
  done
log "  $(ls rirs | wc -l) room echoes"

log "false-alarm check set (0.2 GB)"
fetch "$HF/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy" \
  validation_set_features.npy 184836608

log "background sounds (1.4 GB)"
for part in 00 01; do
  fetch "$HF/agkphysics/AudioSet/resolve/main/data/bal_train/$part.parquet" "audioset/$part.parquet" \
    "$(curl -sSf "https://huggingface.co/api/datasets/agkphysics/AudioSet/tree/main/data/bal_train" |
       "$ROOT/venv/bin/python" -c "import json,sys; print([f['size'] for f in json.load(sys.stdin) if f['path'].endswith('/$part.parquet')][0])")"
done
"$ROOT/venv/bin/python" - "$DATA/audioset" "$DATA/background" <<'EOF'
# AudioSet's clips (FLAC/MP3 inside the parquet files) -> 16 kHz mono WAV files, each once. A few clips at a
# time: reading a whole 700 MB file at once ran the 4 GB Pi out of memory (2026-11-01).
import subprocess, sys, wave
from pathlib import Path
import pyarrow.parquet as pq
src, out = Path(sys.argv[1]), Path(sys.argv[2])
made = skipped = 0
for part in sorted(src.glob("*.parquet")):
    pf = pq.ParquetFile(part)
    rows = (r for b in pf.iter_batches(batch_size=4, columns=["video_id", "audio"]) for r in b.to_pylist())
    for r in rows:
        dst = out / f"{part.stem}_{r['video_id']}.wav"
        if dst.exists():
            continue
        a = r["audio"]
        raw = a.get("bytes") if isinstance(a, dict) else a
        if not raw:
            skipped += 1
            continue
        pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                             input=raw, capture_output=True).stdout
        if len(pcm) < 16000 * 2:
            skipped += 1
            continue
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
        made += 1
print(f"background clips: {made} new, {skipped} skipped, {len(list(out.glob('*.wav')))} in all")
EOF

log "everyday sounds as features (17.3 GB, the long one)"
fetch "$HF/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy" \
  openwakeword_features_ACAV100M_2000_hrs_16bit.npy 17280000128

log "download done: $(du -sh "$DATA" | cut -f1) in $DATA"
