#!/usr/bin/env bash
# LittleBot - Android on the robot (Waydroid), to try apps that have no other way in, like Aosu's doorbell video
# (docs/ANDROID_ON_PI.md). One step at a time; nothing is installed or changed without a y from you.
#
#   scripts/setup_waydroid.sh               # = check: what is there and what is missing (changes nothing)
#   scripts/setup_waydroid.sh kernel        # step 1: the Pi's 4K-page kernel and psi=1 (both files backed up), reboot
#   scripts/setup_waydroid.sh install       # step 2: Waydroid (trixie-backports) + Android with Google Play (~1 GB)
#   scripts/setup_waydroid.sh start         # step 3: Android on the robot's screen
#   scripts/setup_waydroid.sh play-id       # step 4: the number to register at google.com/android/uncertified
#   scripts/setup_waydroid.sh aosu          # step 5: open the Aosu app (installed from Play in Android)
#   scripts/setup_waydroid.sh stop          # Android off again (its memory back for LittleBot)
#   scripts/setup_waydroid.sh undo          # the usual 16K kernel back (reboot after); Waydroid stays installed
#   scripts/setup_waydroid.sh undo --remove # ... and Waydroid with its Android images removed too
#
# Run it as the desktop user. Each step that changes something is printed first and only run after you answer y
# on the terminal (sudo then asks for your password itself); not on a terminal, it only prints. play-id only reads.
set -euo pipefail

CONFIG=/boot/firmware/config.txt
CMDLINE=/boot/firmware/cmdline.txt
BACKUP_SUFFIX=.littlebot-16k
MARK_START="# >>> LittleBot waydroid (scripts/setup_waydroid.sh undo removes this)"
MARK_END="# <<< LittleBot waydroid"
AOSU_APP=com.glazero.android  # Aosu's app on Google Play (check the listing if it doesn't open)
BACKPORTS_FILE=/etc/apt/sources.list.d/trixie-backports.list
BACKPORTS_LINE="deb http://deb.debian.org/debian trixie-backports main contrib"

have_cmd() { command -v "$1" >/dev/null 2>&1; }
page_size() { getconf PAGESIZE 2>/dev/null || echo 0; }
ok() { echo "  ok       $*"; }
no() { echo "  missing  $*"; }
note() { echo "  note     $*"; }

ask_and_run() {  # $1: what it does; $2: the command line
  echo
  echo "$1:"
  echo
  echo "  $2"
  echo
  if [ ! -t 0 ]; then
    echo "(not on a terminal: nothing run)"
    return 1
  fi
  read -r -p "Run it now? It uses sudo (your password). [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) bash -c "$2" ;;
    *) echo "Nothing changed."; return 1 ;;
  esac
}

desktop_env() {  # the robot's desktop (labwc) for Android's window, also when run over ssh
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    local sock
    sock=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-[0-9]' -printf '%f\n' 2>/dev/null | sort | head -1) || true
    export WAYLAND_DISPLAY="${sock:-wayland-0}"
  fi
}

session_running() {  # (the output is read whole first: grep -q stopping early would fail the pipe)
  local status
  status=$(waydroid status 2>/dev/null) || true
  grep -q "Session:.*RUNNING" <<<"$status"
}

cmd_check() {
  echo "Android (Waydroid) on LittleBot:"
  local ps
  ps=$(page_size)
  if [ "$ps" = 4096 ]; then ok "4K-page kernel ($(uname -r))"; else no "4K-page kernel: this one uses $ps-byte pages ($(uname -r)); step 1: $0 kernel"; fi
  if grep -qw binder /proc/filesystems 2>/dev/null; then ok "binder (Android's messaging) is in the kernel"; else no "binder in the kernel"; fi
  if [ -e /proc/pressure/memory ]; then ok "psi (memory pressure for Android)"; else no "psi=1 on the kernel command line (step 1 adds it)"; fi
  if have_cmd waydroid; then ok "waydroid ($(waydroid --version 2>/dev/null || echo installed))"; else no "waydroid; step 2: $0 install"; fi
  if [ -e /var/lib/waydroid/images/system.img ] || [ -e /usr/share/waydroid-extra/images/system.img ]; then
    ok "Android images"
  else
    no "Android images (step 2 downloads them)"
  fi
  desktop_env
  if [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then ok "the desktop ($WAYLAND_DISPLAY) for Android's window"; else no "a desktop session (Wayland) to show Android in"; fi
  local avail free_gb
  avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
  free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
  note "memory free now: ${avail} MB (Android takes about 1-1.5 GB while it runs; stop it after: $0 stop)"
  note "disk free: ${free_gb} GB (Android needs about 3 GB)"
  if have_cmd waydroid && session_running; then note "Android is running now"; fi
}

cmd_kernel() {
  if [ "$(page_size)" = 4096 ] && [ -e /proc/pressure/memory ]; then
    echo "Already on the 4K-page kernel with psi. Next: $0 install"
    return 0
  fi
  [ -e /boot/firmware/kernel8.img ] || { echo "No /boot/firmware/kernel8.img: update the Pi first (sudo apt full-upgrade)." >&2; exit 1; }
  local steps=()
  steps+=("{ [ -e $CONFIG$BACKUP_SUFFIX ] || sudo cp $CONFIG $CONFIG$BACKUP_SUFFIX; }")
  steps+=("{ [ -e $CMDLINE$BACKUP_SUFFIX ] || sudo cp $CMDLINE $CMDLINE$BACKUP_SUFFIX; }")
  if ! grep -qF "$MARK_START" "$CONFIG"; then
    steps+=("printf '\\n%s\\n[all]\\nkernel=kernel8.img\\n%s\\n' '$MARK_START' '$MARK_END' | sudo tee -a $CONFIG >/dev/null")
  fi
  if ! grep -qw "psi=1" "$CMDLINE"; then
    steps+=("sudo sed -i '1 s/\$/ psi=1/' $CMDLINE")
  fi
  local line
  line=$(printf ' && %s' "${steps[@]}")
  if ask_and_run "Step 1 switches the Pi to its 4K-page kernel (kernel8.img, the one the Pi 4 uses: Android's images need 4K pages) and turns on psi; config.txt and cmdline.txt are backed up first (*$BACKUP_SUFFIX). Undo: $0 undo" "${line:4}"; then
    echo
    echo "Done. Restart the robot for it to take effect:  sudo reboot"
    echo "Then:  $0 check   (should say 4K-page kernel and psi ok)"
  fi
}

cmd_install() {
  if [ "$(page_size)" != 4096 ]; then
    echo "This kernel uses $(page_size)-byte pages; Android's images need 4K. First: $0 kernel (then reboot)." >&2
    exit 1
  fi
  local steps=()
  if ! apt-cache policy waydroid 2>/dev/null | grep -q "Candidate: [0-9]"; then
    steps+=("echo '$BACKPORTS_LINE' | sudo tee $BACKPORTS_FILE >/dev/null && sudo apt update")
  fi
  have_cmd waydroid || steps+=("sudo apt install -y -t trixie-backports waydroid")
  # the system and vendor images from Waydroid's own download server (its default), with Google Play (GAPPS)
  steps+=("sudo waydroid init -s GAPPS")
  local line
  line=$(printf ' && %s' "${steps[@]}")
  if ask_and_run "Step 2 installs Waydroid and downloads Android (LineageOS with Google Play, about 1 GB, from Waydroid's own server)" "${line:4}"; then
    echo
    echo "Done. Next: $0 start"
  fi
}

cmd_start() {
  have_cmd waydroid || { echo "Waydroid isn't installed: $0 install" >&2; exit 1; }
  desktop_env
  if ! systemctl is-active --quiet waydroid-container; then
    ask_and_run "Android's container service isn't running; this starts it (not at every boot)" \
      "sudo systemctl start waydroid-container" || exit 1
  fi
  if ! session_running; then
    echo "Starting Android (the first time takes a minute or two) ..."
    nohup waydroid session start >"$XDG_RUNTIME_DIR/waydroid-session.log" 2>&1 &
    for _ in $(seq 1 90); do
      if session_running && [ "$(waydroid prop get sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then break; fi
      sleep 2
    done
    session_running || { echo "Android didn't start; see $XDG_RUNTIME_DIR/waydroid-session.log and: sudo waydroid log" >&2; exit 1; }
  fi
  nohup waydroid show-full-ui >/dev/null 2>&1 &
  echo "Android is on the screen. Sign in to Google Play there, then: $0 play-id"
  echo "Off again: $0 stop"
}

cmd_play_id() {
  session_running || { echo "Android isn't running: $0 start" >&2; exit 1; }
  echo "Android's ID for Google (the robot's Android only; this asks sudo for your password):"
  # https://docs.waydro.id/faq/google-play-certification
  sudo waydroid shell -- sh -c "sqlite3 /data/data/*/*/gservices.db 'select value from main where name = \"android_id\";'"
  echo
  echo "Register that number at https://www.google.com/android/uncertified (signed in with the same Google account),"
  echo "wait a few minutes, then restart Android ($0 stop, $0 start). Google Play then works."
}

cmd_aosu() {
  session_running || { echo "Android isn't running: $0 start" >&2; exit 1; }
  local apps
  apps=$(waydroid app list 2>/dev/null) || true
  if grep -q "$AOSU_APP" <<<"$apps"; then
    waydroid app launch "$AOSU_APP"
    echo "Aosu is open. Try a camera's live view; if it plays, Android on the robot works for the doorbell."
  else
    echo "The Aosu app isn't installed in Android yet: open Google Play there and install \"aosu\"."
    echo "(Its package should be $AOSU_APP; installed under another name, open it from Android's app list.)"
  fi
}

cmd_stop() {
  have_cmd waydroid || exit 0
  if session_running; then waydroid session stop; echo "Android stopped."; else echo "Android isn't running."; fi
}

cmd_undo() {
  local steps=()
  if have_cmd waydroid && session_running; then steps+=("waydroid session stop"); fi
  if grep -qF "$MARK_START" "$CONFIG"; then
    steps+=("sudo sed -i '\\|^$MARK_START|,\\|^$MARK_END|d' $CONFIG")
  fi
  # psi=1 comes off only if step 1 added it (the backup taken before then didn't have it)
  if grep -qw "psi=1" "$CMDLINE" && [ -e "$CMDLINE$BACKUP_SUFFIX" ] && ! grep -qw "psi=1" "$CMDLINE$BACKUP_SUFFIX"; then
    steps+=("sudo sed -i '1 s/ psi=1//' $CMDLINE")
  fi
  if [ "${2:-}" = "--remove" ] && have_cmd waydroid; then
    steps+=("sudo systemctl stop waydroid-container && sudo apt remove -y waydroid && sudo rm -rf /var/lib/waydroid ~/.local/share/waydroid")
  fi
  if [ "${#steps[@]}" -eq 0 ]; then
    echo "Nothing to undo."
    return 0
  fi
  local line
  line=$(printf ' && %s' "${steps[@]}")
  if ask_and_run "Undo: the usual 16K-page kernel back${2:+, Waydroid removed}" "${line:4}"; then
    echo
    echo "Done. Restart the robot for the kernel to change back:  sudo reboot"
  fi
}

case "${1:-check}" in
  check) cmd_check ;;
  kernel) cmd_kernel ;;
  install) cmd_install ;;
  start) cmd_start ;;
  play-id) cmd_play_id ;;
  aosu) cmd_aosu ;;
  stop) cmd_stop ;;
  undo) cmd_undo "$@" ;;
  -h|--help|help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown step: $1 (try --help)" >&2; exit 2 ;;
esac
