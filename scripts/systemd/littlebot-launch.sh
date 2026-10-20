#!/usr/bin/env bash
# LittleBot launcher used by the systemd user service and the desktop autostart entries.
#
# pygame needs the graphical session. A systemd --user service can start before the
# desktop (labwc) is up and may not inherit WAYLAND_DISPLAY / DISPLAY, so this script
# waits for the compositor's socket and exports the variables itself, then *execs* the
# app so that systemd's SIGTERM reaches Python directly (the app de-energises servos and
# shuts down cleanly on SIGTERM).
#
# Environment knobs (put them in ~/.config/littlebot/littlebot.env for the service):
#   LITTLEBOT_MODE=pi                 mode passed to --mode (default: pi)
#   LITTLEBOT_ARGS="--debug"          extra command-line arguments
#   LITTLEBOT_HEADLESS=1              do not wait for a display; run with --headless
#   LITTLEBOT_DISPLAY_WAIT=90         seconds to wait for the desktop (default 90)
#   SDL_VIDEODRIVER=wayland|x11       force an SDL video driver (default: SDL decides;
#                                     under labwc it usually uses Xwayland via DISPLAY)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${LITTLEBOT_PYTHON:-$REPO_DIR/.venv/bin/python}"
MODE="${LITTLEBOT_MODE:-pi}"
WAIT_SECONDS="${LITTLEBOT_DISPLAY_WAIT:-90}"

if [[ ! -x "$PYTHON" ]]; then
    echo "littlebot-launch: $PYTHON not found - run scripts/install_pi.sh first" >&2
    exit 1
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PYTHONUNBUFFERED=1

find_wayland_socket() {
    local sock
    if [[ -n "${WAYLAND_DISPLAY:-}" && -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]]; then
        return 0
    fi
    for sock in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
        [[ "$sock" == *.lock ]] && continue
        if [[ -S "$sock" ]]; then
            WAYLAND_DISPLAY="$(basename "$sock")"
            export WAYLAND_DISPLAY
            return 0
        fi
    done
    return 1
}

find_x_display() {
    if [[ -n "${DISPLAY:-}" ]]; then
        return 0
    fi
    if [[ -S /tmp/.X11-unix/X0 ]]; then
        export DISPLAY=:0
        return 0
    fi
    return 1
}

args=(-m littlebot --mode "$MODE")
if [[ "${LITTLEBOT_HEADLESS:-0}" == "1" ]]; then
    args+=(--headless)
else
    waited=0
    until find_wayland_socket || find_x_display; do
        if (( waited >= WAIT_SECONDS )); then
            echo "littlebot-launch: no desktop session after ${WAIT_SECONDS}s (no Wayland socket in $XDG_RUNTIME_DIR, no X display)." >&2
            echo "littlebot-launch: enable Desktop Autologin (raspi-config > System > Boot/Auto Login) or set LITTLEBOT_HEADLESS=1." >&2
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    # Xwayland is usually available as :0 under labwc; give SDL both options.
    find_x_display || true
    echo "littlebot-launch: display ready after ${waited}s (WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} DISPLAY=${DISPLAY:-} SDL_VIDEODRIVER=${SDL_VIDEODRIVER:-auto})"
fi

if [[ -n "${LITTLEBOT_ARGS:-}" ]]; then
    read -r -a extra <<<"$LITTLEBOT_ARGS"
    args+=("${extra[@]}")
fi

cd "$REPO_DIR"
exec "$PYTHON" "${args[@]}"
