#!/bin/bash
# Runs the whole wake-word training on the robot, each step under a memory limit, and starts the training only
# once download.sh has finished (the voices and features don't need its big file, so they start at once).
#
#   cd ~/wwtrain && (nohup bash ~/LittleBot/scripts/wake_training/run_all.sh > train.log 2>&1 < /dev/null &)
#
# Progress: tail -f ~/wwtrain/train.log. The model: ~/wwtrain/hey_little_bot/hey_little_bot.onnx.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"  # train.py is next to this script
ROOT="${WWTRAIN:-$HOME/wwtrain}"
cd "$ROOT" || exit 1
BIG=data/openwakeword_features_ACAV100M_2000_hrs_16bit.npy
BIG_SIZE=17280000128
log() { echo "[$(date '+%F %T')] $*"; }
step() {  # a step that wants too much memory stops on its own: LittleBot and the robot keep going
  systemd-run --user --scope -q -p MemoryMax=1700M -p MemorySwapMax=200M nice -n 15 venv/bin/python "$HERE/train.py" "$@"
}

step voices || { log "voices failed"; exit 1; }
step features || { log "features failed"; exit 1; }
while pgrep -f "wake_training/download.sh|bash download.sh" > /dev/null; do sleep 60; done
size=$(stat -c %s "$BIG" 2>/dev/null || echo 0)
if [ "$size" != "$BIG_SIZE" ]; then
  log "the everyday-sound file isn't complete ($size of $BIG_SIZE bytes): run download.sh again, then this"
  exit 1
fi
step train || { log "train failed"; exit 1; }
step check || { log "check failed"; exit 1; }
log "all done"
