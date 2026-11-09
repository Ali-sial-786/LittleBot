#!/usr/bin/env bash
# LittleBot - what phone mirroring needs (docs/MIRRORING.md), and the one command that installs it.
#
#   scripts/setup_mirroring.sh            # check, print the install command, ask y/N before running it
#   scripts/setup_mirroring.sh --check    # only check and print (never asks, never installs)
#
# Run it as the desktop user. It changes nothing by itself: it only runs `sudo ...` after you answer y on
# the terminal (sudo then asks for your password itself).
#
# What it checks (Debian 13 trixie / Raspberry Pi OS trixie):
#   iPhone (AirPlay):  uxplay (trixie: 1.71) and the GStreamer plugins it uses for software H.264 decoding
#                      and the Wayland video window: gstreamer1.0-libav, -plugins-bad, -plugins-good.
#                      The Pi 5 has no H.264 decoder chip; LittleBot starts uxplay with -avdec.
#                      avahi-daemon must run, so iPhones can find "LittleBot" on the Wi-Fi.
#   Android:           adb (trixie: 34) and scrcpy. scrcpy is not in trixie itself, only in
#                      trixie-backports (contrib): this script adds that source if it is missing.
set -euo pipefail

CHECK_ONLY=0
case "${1:-}" in
  --check) CHECK_ONLY=1 ;;
  -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

BACKPORTS_FILE=/etc/apt/sources.list.d/trixie-backports.list
BACKPORTS_LINE="deb http://deb.debian.org/debian trixie-backports main contrib"

have_cmd() { command -v "$1" >/dev/null 2>&1; }
have_pkg() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
in_apt() { apt-cache policy "$1" 2>/dev/null | grep -q "Candidate: [0-9]"; }

missing=()
echo "Phone mirroring on LittleBot:"
for cmd in uxplay adb scrcpy; do
  if have_cmd "$cmd"; then echo "  ok       $cmd ($(command -v "$cmd"))"; else echo "  missing  $cmd"; missing+=("$cmd"); fi
done
for pkg in gstreamer1.0-libav gstreamer1.0-plugins-bad gstreamer1.0-plugins-good; do
  if have_pkg "$pkg"; then echo "  ok       $pkg"; else echo "  missing  $pkg"; missing+=("$pkg"); fi
done

avahi_ok=1
if have_cmd systemctl && systemctl is-active --quiet avahi-daemon; then
  echo "  ok       avahi-daemon is running (iPhones can find LittleBot)"
else
  echo "  missing  avahi-daemon is not running (iPhones won't see LittleBot)"
  avahi_ok=0
fi

apt_pkgs=()
need_scrcpy=0
for m in "${missing[@]+"${missing[@]}"}"; do
  if [ "$m" = scrcpy ]; then need_scrcpy=1; else apt_pkgs+=("$m"); fi
done

steps=()
if [ "${#apt_pkgs[@]}" -gt 0 ]; then
  steps+=("sudo apt install -y ${apt_pkgs[*]}")
fi
if [ "$need_scrcpy" = 1 ]; then
  if in_apt scrcpy && apt-cache policy scrcpy 2>/dev/null | grep -q backports; then
    steps+=("sudo apt install -y -t trixie-backports scrcpy")
  elif in_apt scrcpy; then
    steps+=("sudo apt install -y scrcpy")
  else
    steps+=("echo '$BACKPORTS_LINE' | sudo tee $BACKPORTS_FILE >/dev/null && sudo apt update && sudo apt install -y -t trixie-backports scrcpy")
  fi
fi
if [ "$avahi_ok" = 0 ]; then
  steps+=("sudo apt install -y avahi-daemon && sudo systemctl enable --now avahi-daemon")
fi

if [ "${#steps[@]}" -eq 0 ]; then
  echo "Everything is installed. Say \"Hey LittleBot, mirror my iPhone\", or use the dashboard (Assistant page)."
  exit 0
fi

command_line=$(printf ' && %s' "${steps[@]}")
command_line=${command_line:4}
echo
echo "To install what is missing, run:"
echo
echo "  $command_line"
echo
if [ "$CHECK_ONLY" = 1 ] || [ ! -t 0 ]; then
  exit 0
fi
read -r -p "Run it now? It uses sudo (your password). [y/N] " answer
case "$answer" in
  y|Y|yes|YES)
    bash -c "$command_line"
    echo "Done. LittleBot sees the new programs at once (the dashboard's Phone mirroring card says so)."
    ;;
  *) echo "Nothing installed." ;;
esac
