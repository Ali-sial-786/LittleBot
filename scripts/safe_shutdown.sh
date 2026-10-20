#!/usr/bin/env bash
# LittleBot - safe shutdown.
#
#   scripts/safe_shutdown.sh             # stop LittleBot, then power the Pi off (asks first)
#   scripts/safe_shutdown.sh --yes       # no question
#   scripts/safe_shutdown.sh --reboot    # reboot instead of power off
#   scripts/safe_shutdown.sh --app-only  # stop LittleBot but keep the Pi running
#   scripts/safe_shutdown.sh --dry-run   # show what would happen
#
# Order matters:
#   1. Stop the app with SIGTERM (via systemd if it runs as a service). On SIGTERM the
#      app stops activities and media, puts the servos in a safe, de-energised state
#      and closes the camera/microphone and its database cleanly.
#   2. Verify it really exited. If it did not, DO NOT continue by default: a PCA9685
#      keeps driving the servos after the Pi stops talking to it, so switch the servo
#      supply off first (or re-run with --force; after a forced kill the script runs
#      `python -m littlebot.motion off` to switch every PCA9685 output off over I2C).
#   3. sync, then `sudo shutdown -h now`. Unplug only after the green activity LED has
#      stopped flashing (on the Pi 5 the power LED turns red when it is safe).
#      Never pull the power while the SD card is being written: it can corrupt the card.
set -euo pipefail

TIMEOUT=25
DRY_RUN=0
ASSUME_YES=0
ACTION=poweroff
FORCE=0

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes | -y) ASSUME_YES=1 ;;
        --reboot) ACTION=reboot ;;
        --app-only) ACTION=none ;;
        --force) FORCE=1 ;;
        --timeout) shift; TIMEOUT="${1:?--timeout needs seconds}" ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '==> %s\n' "$*"; }
run() {
    if ((DRY_RUN)); then
        printf '    [dry-run] %s\n' "$*"
    else
        printf '    $ %s\n' "$*"
        "$@"
    fi
}

# True if the given argv is the app: "python [opts] -m littlebot ...", the "littlebot"
# console script, or littlebot/app/main.py. Works on argv *elements*, so a shell whose
# command text merely mentions "-m littlebot" (bash -c "...", watch, tmux) never matches.
is_littlebot_argv() {
    local exe a
    (($#)) || return 1
    exe="${1##*/}"
    [[ "$exe" == littlebot ]] && return 0
    [[ "$exe" == python* ]] || return 1
    shift
    while (($#)); do
        a="$1"
        case "$a" in
            -m) [[ "${2:-}" == littlebot ]]; return ;;
            -m*) [[ "${a#-m}" == littlebot ]]; return ;;
            -c*) return 1 ;;
            -X | -W) (($# >= 2)) || return 1; shift 2 ;;
            -*) shift ;;
            *)
                [[ "${a##*/}" == littlebot || "$a" == littlebot/app/main.py || "$a" == */littlebot/app/main.py ||
                    "$a" == littlebot/__main__.py || "$a" == */littlebot/__main__.py ]]
                return
                ;;
        esac
    done
    return 1
}

# PIDs of running LittleBot app processes (read from /proc/<pid>/cmdline).
app_pids() {
    local d pid
    local -a argv
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        [[ "$pid" == "$$" || "$pid" == "$BASHPID" ]] && continue
        mapfile -d '' -t argv 2>/dev/null <"$d/cmdline" || continue
        ((${#argv[@]})) || continue
        if is_littlebot_argv "${argv[@]}"; then
            echo "$pid"
        fi
    done
}

has_systemctl() { command -v systemctl >/dev/null 2>&1; }

if ((ASSUME_YES == 0 && DRY_RUN == 0)) && [[ "$ACTION" != "none" ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Stop LittleBot and $ACTION the Raspberry Pi now? [y/N] " answer
        [[ "$answer" == [yY]* ]] || { echo "Cancelled."; exit 1; }
    else
        echo "Not interactive: re-run with --yes." >&2
        exit 1
    fi
fi

# 1. Stop the app ------------------------------------------------------------
say "Stopping LittleBot"
stopped_any=0
if has_systemctl && systemctl --user is-active --quiet littlebot.service 2>/dev/null; then
    run systemctl --user stop littlebot.service
    stopped_any=1
fi
if has_systemctl && systemctl is-active --quiet littlebot.service 2>/dev/null; then
    run sudo systemctl stop littlebot.service
    stopped_any=1
fi

mapfile -t pids < <(app_pids)
if ((${#pids[@]})); then
    say "Sending SIGTERM to LittleBot process(es): ${pids[*]}"
    run kill -TERM "${pids[@]}"
    stopped_any=1
fi
((stopped_any)) || say "LittleBot is not running."

# 2. Verify -------------------------------------------------------------------
killed=0
if ((DRY_RUN == 0)); then
    waited=0
    while :; do
        mapfile -t left < <(app_pids)
        ((${#left[@]} == 0)) && break
        if ((waited >= TIMEOUT)); then
            echo "LittleBot (PID ${left[*]}) did not exit within ${TIMEOUT}s." >&2
            if ((FORCE)); then
                echo "--force: killing it. The servos may still be powered: switch the SERVO SUPPLY OFF now." >&2
                kill -KILL "${left[@]}" 2>/dev/null || true
                killed=1
                break
            fi
            echo "Not shutting down. Switch the servo power supply off first, then re-run with --force." >&2
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if ((killed)); then
        say "LittleBot was killed: it could not make the servos safe itself."
        # Best effort: switch every PCA9685 output off directly over I2C (only ever writes "off").
        repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
        if [[ -x "$repo_dir/.venv/bin/python" ]]; then
            run "$repo_dir/.venv/bin/python" -m littlebot.motion off ||
                echo "Could not confirm the servos are off: switch the SERVO SUPPLY OFF now." >&2
        fi
    elif ((stopped_any)); then
        say "LittleBot stopped (servos de-energised by the app on exit)."
    fi
fi

# 3. Flush and power off ---------------------------------------------------------
say "Flushing file system buffers"
run sync

case "$ACTION" in
    none)
        say "Done - the Pi keeps running."
        ;;
    reboot)
        say "Rebooting."
        run sudo shutdown -r now
        ;;
    poweroff)
        say "Powering off. Wait until the green LED stops flashing (Pi 5: power LED turns red) before unplugging."
        run sudo shutdown -h now
        ;;
esac
