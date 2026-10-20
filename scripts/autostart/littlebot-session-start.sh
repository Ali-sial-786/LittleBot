#!/usr/bin/env bash
# Run from the desktop session (XDG autostart or labwc autostart) when the desktop is up.
#
# 1. Hands the session's display variables to the systemd user manager, so the
#    littlebot user service (and a Chromium kiosk it starts) can open windows.
# 2. Starts littlebot.service if it is installed; otherwise runs the launcher directly
#    (logging to the journal with the tag "littlebot"), so this also works on its own.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VARS=(WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR XDG_SESSION_TYPE XDG_CURRENT_DESKTOP)

if command -v systemctl >/dev/null 2>&1 && systemctl --user cat littlebot.service >/dev/null 2>&1; then
    systemctl --user import-environment "${VARS[@]}" 2>/dev/null || true
    if command -v dbus-update-activation-environment >/dev/null 2>&1; then
        dbus-update-activation-environment --systemd "${VARS[@]}" 2>/dev/null || true
    fi
    exec systemctl --user start littlebot.service
fi

# No user service installed: run directly (no automatic restart in this mode).
if command -v systemd-cat >/dev/null 2>&1; then
    exec systemd-cat -t littlebot "$REPO_DIR/scripts/systemd/littlebot-launch.sh"
fi
exec "$REPO_DIR/scripts/systemd/littlebot-launch.sh"
