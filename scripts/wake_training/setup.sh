#!/bin/bash
# Installs the tools that train LittleBot's own wake word ("Hey LittleBot", openWakeWord) on the robot.
# Everything goes into ~/wwtrain, in its own Python environment (~/wwtrain/venv): LittleBot's own
# environment is not touched. About 2 GB. Safe to run again (it carries on where it stopped).
#
#   bash scripts/wake_training/setup.sh
#
# Then: bash scripts/wake_training/download.sh, then train.py (docs/WAKE_TRAINING.md).
set -euo pipefail

ROOT="${WWTRAIN:-$HOME/wwtrain}"
mkdir -p "$ROOT"
cd "$ROOT"
log() { echo "[$(date '+%F %T')] $*"; }

# The two projects the training uses, at the versions it was written against.
if [ ! -d openWakeWord ]; then
  git clone -q https://github.com/dscripka/openWakeWord.git
  git -C openWakeWord checkout -q 368c037
fi
if [ ! -d piper-sample-generator ]; then
  git clone -q https://github.com/rhasspy/piper-sample-generator.git
  git -C piper-sample-generator checkout -q 2971426
fi

if [ ! -x venv/bin/python ]; then
  log "making the Python environment"
  python3 -m venv venv
fi
PIP="venv/bin/python -m pip"
$PIP install -q -U pip wheel setuptools

log "PyTorch (CPU only)"
$PIP install -q torch torchaudio --index-url https://download.pytorch.org/whl/cpu

log "the speech generator and the training libraries"
$PIP install -q "piper-tts==1.3.0" "audiomentations==0.33.0" onnxruntime onnx scipy scikit-learn tqdm \
  requests pyyaml torchinfo torchmetrics pronouncing mutagen soundfile pyarrow
$PIP install -q --no-deps -e openWakeWord
$PIP install -q --no-deps -e piper-sample-generator

venv/bin/python - <<'EOF'
import torch, torchaudio, onnxruntime, piper, openwakeword  # noqa: F401
print("torch", torch.__version__, "| torchaudio", torchaudio.__version__, "| onnxruntime", onnxruntime.__version__)
EOF
log "setup done"
