#!/usr/bin/env bash
# LittleBot - Spotify Connect on the robot: a speaker called "LittleBot" in every Spotify app, which LittleBot
# itself plays music on ("play some jazz"). Spotify only allows this with Spotify Premium.
#
#   scripts/setup_spotify.sh              # set it up (or bring it up to date); safe to run again
#   scripts/setup_spotify.sh --dry-run    # print what it would do and the files it would write, change nothing
#   scripts/setup_spotify.sh --check      # is it working? (PASS / WARN / FAIL per item, changes nothing)
#   scripts/setup_spotify.sh --update     # fetch a newer Soloist build (they stop working 90 days after they're built)
#   scripts/setup_spotify.sh --pair       # sign the receiver in again (pick LittleBot in the Spotify app)
#   scripts/setup_spotify.sh --uninstall  # undo everything it did
#
# The receiver is Spotify Soloist, Spotify's own headless Spotify Connect client for Linux
# (https://developer.spotify.com/documentation/soloist). It replaced librespot (from raspotify) on 2026-11-02:
# Spotify refused librespot's audio keys for this account ("error audio key 0 1", librespot issue #1649), so
# every song was skipped.
#
# UNTESTED ON A REAL RASPBERRY PI: written from Spotify's Soloist documentation and tested only with fake
# commands (tests/test_spotify_script.py). Run --check on the robot.
#
# Run it as the desktop user. It needs no sudo; the only things that do (removing the old raspotify package,
# switching off raspotify's own service, pausing avahi-daemon for a pairing) are asked y/N first. Step by step
# (each step also says so as it runs):
#   1. Downloads Soloist from Spotify (https://soloist-builds.spotifycdn.com/soloist_release_<arch>.tar.gz) and
#      installs it as ~/.local/bin/soloist. Spotify publishes only "the newest build" (no versions to pin, no
#      SHA-256): the download is checked against the ETag (MD5) Spotify's server sends, must run
#      `soloist --version`, and its SHA-256 is recorded (set LITTLEBOT_SOLOIST_SHA256 to insist on one).
#   2. Retires the old receiver: librespot's sign-in (~/.cache/littlebot-spotify) is deleted, and it offers to
#      remove the raspotify package (or at least switch off its system service), asking y/N before sudo.
#   3. Asks for your Soloist API key (from developer.spotify.com/dashboard/soloist, made by a Premium account)
#      and keeps it in ~/.config/littlebot-spotify/api-key.env (chmod 600). It is never printed.
#   4. Writes ~/.local/bin/littlebot-spotify (a small launcher), the user service littlebot-spotify.service and a
#      weekly timer (littlebot-spotify-update.timer) that runs --update. Soloist runs as YOUR user, so it plays
#      through your PipeWire session into the default speaker: the echo-cancelled littlebot_ec_speaker
#      (scripts/setup_echo_cancel.sh), whose sound the microphone hears taken out, so music never sets off the
#      wake word. Settings: --device-name LittleBot, --initial-volume 60, its local WebSocket API on 127.0.0.1
#      only (for --check and LittleBot's status). Its sign-in lives in ~/.local/share/soloist.
#   5. Enables the service and the timer (they start at login, like LittleBot), (re)starts it, then runs --check.
#
# Afterwards (docs/SPOTIFY.md):
#   - Once: on a phone on the same Wi-Fi, open Spotify, tap the speaker icon and pick "LittleBot". That signs the
#     receiver in (it remembers it); from then on it signs in by itself at every start, and LittleBot can find it.
#   - Connect Spotify on the dashboard (Assistant page) with your app's Client ID from developer.spotify.com.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=0
CHECK=0
UNINSTALL=0
UPDATE=0
PAIR=0
NEW_KEY=0
NAME="LittleBot"
VOLUME=60
LIFETIME_DAYS=90  # Spotify: "builds expire 90 days after their build date" (they exit with code 10)
RENEW_DAYS=14     # setup fetches a new build when the one installed has this few days left

UNIT_NAME="littlebot-spotify.service"
UPDATE_UNIT="littlebot-spotify-update.service"
TIMER_UNIT="littlebot-spotify-update.timer"
EC_SINK="littlebot_ec_speaker"
KEY_PAGE="https://developer.spotify.com/dashboard/soloist"

usage() {
    cat <<'EOF'
Usage: scripts/setup_spotify.sh [options]

Makes the robot a Spotify Connect speaker called "LittleBot" with Spotify Soloist (Spotify's own receiver),
running as your user so it plays through PipeWire into the echo-cancelled speaker. Needs Spotify Premium and a
Soloist API key (developer.spotify.com/dashboard/soloist). Run it as the desktop user; it asks y/N before
anything that needs sudo. Untested on a real Raspberry Pi: run --check afterwards. docs/SPOTIFY.md explains the
rest (the developer app, connecting on the dashboard).

Options:
  --dry-run          print every step and the files it would write, change nothing
  --check            report whether it works (changes nothing; exit 1 on a FAIL)
  --update           fetch Spotify's newest Soloist build if it is newer, and restart the receiver when nothing
                     plays (the weekly timer runs this; builds stop working 90 days after they're built)
  --pair             sign the receiver in again: pick it in the Spotify app on the same Wi-Fi
  --new-key          ask for the Soloist API key again (a new or replaced key)
  --uninstall        undo it: stop and remove the service, the timer, the launcher, Soloist, its sign-in and the
                     API key
  --name NAME        the speaker's name in Spotify (default LittleBot; LittleBot looks for it by this name:
                     skills.spotify_device_name)
  --volume PERCENT   the volume it starts at (default 60)
  -h, --help         show this help

Check by hand:  systemctl --user status littlebot-spotify     journalctl --user-unit littlebot-spotify -f
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --check) CHECK=1 ;;
        --uninstall | --remove) UNINSTALL=1 ;;
        --update) UPDATE=1 ;;
        --pair) PAIR=1 ;;
        --new-key) NEW_KEY=1 ;;
        --name) shift; NAME="${1:?--name needs a name}" ;;
        --name=*) NAME="${1#*=}" ;;
        --volume) shift; VOLUME="${1:?--volume needs a percent}" ;;
        --volume=*) VOLUME="${1#*=}" ;;
        --bitrate | --bitrate=*)
            echo "--bitrate is gone: Soloist picks the quality itself (the Spotify app's settings)." >&2
            exit 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if ((CHECK + UNINSTALL + UPDATE + PAIR > 1)); then
    echo "Choose one of --check, --update, --pair and --uninstall." >&2
    exit 2
fi
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9\ _-]{0,29}$ ]] || { echo "--name: letters, digits, spaces, - and _ (30 at most)" >&2; exit 2; }
[[ "$VOLUME" =~ ^[0-9]{1,3}$ ]] && ((VOLUME <= 100)) || { echo "--volume must be 0 to 100" >&2; exit 2; }

if [[ -t 1 ]]; then
    C_BLUE=$'\033[1;34m'; C_YEL=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_GRN=$'\033[1;32m'; C_OFF=$'\033[0m'
else
    C_BLUE=""; C_YEL=""; C_RED=""; C_GRN=""; C_OFF=""
fi
step() { printf '\n%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die() { printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Run a command, or only print it in --dry-run mode.
run() {
    if ((DRY_RUN)); then
        printf '    [dry-run] %s\n' "$*"
    else
        printf '    $ %s\n' "$*"
        "$@"
    fi
}

# A question on the terminal (y/N): only a person can say yes. Not a terminal, or a dry run: no.
# (LITTLEBOT_SPOTIFY_INTERACTIVE=1 reads the answers from stdin anyway: the tests.)
interactive() { [[ -t 0 || "${LITTLEBOT_SPOTIFY_INTERACTIVE:-}" == 1 ]]; }
ask() {
    local answer=""
    if ((DRY_RUN)); then
        info "[dry-run] would ask: $1 [y/N]"
        return 1
    fi
    if ! interactive; then
        info "(not asked: no terminal) $1"
        return 1
    fi
    printf '    %s [y/N] ' "$1"
    read -r answer || answer=""
    printf '\n'
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

if [[ "$(id -u)" -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" ]]; then
        die "Do not use sudo: the receiver runs as the desktop user. Run it as $SUDO_USER: scripts/setup_spotify.sh"
    fi
    warn "Running as root: this sets up a receiver for root, outside the desktop user's sound session."
fi
# Over SSH the runtime directory (PipeWire / systemd --user sockets) may not be set.
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export XDG_RUNTIME_DIR
fi

CONF_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONF_HOME/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
UPDATE_FILE="$UNIT_DIR/$UPDATE_UNIT"
TIMER_FILE="$UNIT_DIR/$TIMER_UNIT"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/littlebot-spotify"
SOLOIST_HOME_BIN="$BIN_DIR/soloist"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/soloist"  # Soloist's own default: `soloist ctl` finds it there
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/soloist"
OLD_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/littlebot-spotify"  # librespot's sign-in (before Soloist)
STATE_DIR="$CONF_HOME/littlebot-spotify"  # what this script did, and the API key
KEY_FILE="$STATE_DIR/api-key.env"
TOKEN_FILE="$REPO_DIR/data/spotify_token.json"

case "${LITTLEBOT_SPOTIFY_ARCH:-$(uname -m)}" in
    aarch64 | arm64) ARCH=arm64 ;;
    armv7l | armv8l | armhf) ARCH=arm32 ;;
    x86_64 | amd64) ARCH=x86_64 ;;
    *) ARCH="" ;;
esac
SOLOIST_URL="${LITTLEBOT_SOLOIST_URL:-https://soloist-builds.spotifycdn.com/soloist_release_${ARCH:-unknown}.tar.gz}"
WANT_SHA256="${LITTLEBOT_SOLOIST_SHA256:-}"

SOLOIST="${LITTLEBOT_SPOTIFY_SOLOIST:-}"
find_soloist() {
    if [[ -n "$SOLOIST" && -x "$SOLOIST" ]]; then
        return 0
    fi
    SOLOIST=""
    if [[ -x "$SOLOIST_HOME_BIN" ]]; then
        SOLOIST="$SOLOIST_HOME_BIN"
    elif have soloist; then
        SOLOIST="$(command -v soloist)"
    fi
}
soloist_version() {  # e.g. "soloist 1.3.10.31 build 20261030 (linux/aarch64)"
    "${1:-$SOLOIST}" --version 2>/dev/null | head -1 || true
}
# The build's date (Unix time) from its --version: "build 20261030" (a date), "build 1786982514" (a Unix time)
# or a date like 2026-10-30 anywhere in it. Empty when it can't be read.
build_epoch() {
    local text="$1" b
    b="$(printf '%s\n' "$text" | grep -Eio 'build[ :=]*[0-9]{8,10}' | grep -Eo '[0-9]{8,10}' | head -1 || true)"
    if [[ ${#b} -eq 8 ]]; then
        date -u -d "$b" +%s 2>/dev/null || true
    elif [[ ${#b} -eq 10 ]]; then
        printf '%s' "$b"
    else
        b="$(printf '%s\n' "$text" | grep -Eo '20[0-9]{2}-[01][0-9]-[0-3][0-9]' | head -1 || true)"
        if [[ -n "$b" ]]; then
            date -u -d "$b" +%s 2>/dev/null || true
        fi
    fi
}
# Days until the build stops working (negative: it has), counted in whole (UTC) days; empty when unknown.
days_left() {
    local built
    built="$(build_epoch "$1")"
    [[ -n "$built" ]] || return 0
    printf '%s' $((built / 86400 + LIFETIME_DAYS - $(date +%s) / 86400))
}
expiry_text() {  # "62 days left" / "expired 3 days ago" / "" (unknown)
    local d
    d="$(days_left "$1")"
    if [[ -z "$d" ]]; then
        return 0
    elif ((d < 0)); then
        printf 'expired %s day(s) ago' "${d#-}"
    else
        printf '%s day(s) left' "$d"
    fi
}
key_saved() {  # the key file has a key (never printed)
    [[ -r "$KEY_FILE" ]] && grep -Eq '^SOLOIST_API_KEY=.+' "$KEY_FILE"
}
signed_in_rc() {  # soloist ctl's exit code for "now": 0 signed in, 3 not signed in (or no answer), 2 can't reach it
    local rc=0
    [[ -n "$SOLOIST" ]] || { printf '2'; return 0; }
    timeout 10 "$SOLOIST" ctl --data-dir "$DATA_DIR" now --json >/dev/null 2>&1 || rc=$?
    printf '%s' "$rc"
}
playing_now() {
    [[ -n "$SOLOIST" ]] || return 1
    timeout 10 "$SOLOIST" ctl --data-dir "$DATA_DIR" now --json 2>/dev/null | grep -Eq '"status" *: *"playing"'
}
pulse_running() {
    have pactl && pactl info >/dev/null 2>&1
}
default_sink() {
    if have pactl; then
        pactl get-default-sink 2>/dev/null || true
    fi
}
raspotify_installed() {
    have dpkg-query && dpkg-query -W -f='${Status}' raspotify 2>/dev/null | grep -q 'install ok installed'
}
raspotify_known() {
    have systemctl && systemctl list-unit-files raspotify.service --no-legend 2>/dev/null | grep -q raspotify
}
raspotify_state() {  # enabled | disabled | masked | ... ("" when it isn't installed)
    if raspotify_known; then
        systemctl is-enabled raspotify.service 2>/dev/null || true
    fi
}
librespot_running() {
    have pgrep && pgrep -x librespot >/dev/null 2>&1
}
avahi_running() {
    have systemctl && systemctl is-active --quiet avahi-daemon.service 2>/dev/null
}
soloist_on_mdns() {  # Soloist has the mDNS port (5353), so Spotify apps on the Wi-Fi can find it
    have ss && ss -H -ulpn 'sport = :5353' 2>/dev/null | grep -q '"soloist"'
}
runs_librespot() {  # the launcher is still the librespot one (before 2026-11-02)
    [[ -f "$LAUNCHER" ]] && grep -Eq 'LIBRESPOT=|/librespot([" ]|$)' "$LAUNCHER"
}
unit_known() { [[ -f "$UNIT_FILE" ]] || systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; }

# Write stdin to a file only when it changed (dry-run: print it). Returns 0 when it wrote (or would write).
CHANGED=0
write_file() {
    local path="$1" mode="${2:-644}" content
    content="$(cat)"
    if ((DRY_RUN)); then
        printf '    [dry-run] write %s:\n' "$path"
        printf '%s\n' "$content" | sed 's/^/      | /'
        return 0
    fi
    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
        info "unchanged: $path"
        return 1
    fi
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" >"$path"
    chmod "$mode" "$path"
    info "wrote $path"
    CHANGED=1
    return 0
}

launcher_text() {
    local bin="$1"
    cat <<EOF
#!/bin/sh
# LittleBot's Spotify Connect receiver: Spotify Soloist as "$NAME" (written by scripts/setup_spotify.sh, started
# by $UNIT_NAME; run the script again to change it). It plays through PipeWire into the default speaker (the
# echo-cancelled $EC_SINK). Its sign-in lives in $DATA_DIR.
# The API key comes from $KEY_FILE (chmod 600). Soloist only takes it on its command line.
set -eu
SOLOIST="$bin"
KEY_FILE="$KEY_FILE"
if [ -z "\${SOLOIST_API_KEY:-}" ] && [ -r "\$KEY_FILE" ]; then
    SOLOIST_API_KEY="\$(sed -n 's/^SOLOIST_API_KEY=//p' "\$KEY_FILE" | head -n 1)"
fi
if [ -z "\${SOLOIST_API_KEY:-}" ]; then
    echo "littlebot-spotify: no Soloist API key in \$KEY_FILE: run scripts/setup_spotify.sh --new-key" >&2
    exit 3
fi
mkdir -p "$DATA_DIR" "$CACHE_DIR"
exec "\$SOLOIST" --device-name "$NAME" --api-key "\$SOLOIST_API_KEY" --data-dir "$DATA_DIR" \\
    --cache-dir "$CACHE_DIR" --cache-size 100 --initial-volume $VOLUME --ws 127.0.0.1:0 "\$@"
EOF
}

unit_text() {
    cat <<EOF
# LittleBot - the robot's Spotify Connect receiver (Spotify Soloist as "$NAME"), a systemd USER service.
# Written by scripts/setup_spotify.sh (run it again to change it; --uninstall removes it). docs/SPOTIFY.md
#   systemctl --user status littlebot-spotify      journalctl --user-unit littlebot-spotify -f
#   systemctl --user restart littlebot-spotify
# Needs Spotify Premium. It runs in your session (like LittleBot), so its sound goes through PipeWire.
# Exit 10: the Soloist build expired (scripts/setup_spotify.sh --update); exit 3: no API key.

[Unit]
Description=LittleBot's Spotify Connect receiver (Spotify Soloist as "$NAME")
Documentation=file://$REPO_DIR/docs/SPOTIFY.md
After=pipewire.service pipewire-pulse.service wireplumber.service network-online.target
Wants=pipewire.service
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStart=$LAUNCHER
Restart=always
RestartSec=5
RestartPreventExitStatus=3 10
Nice=5

[Install]
WantedBy=default.target
EOF
}

update_unit_text() {
    cat <<EOF
# LittleBot - fetches Spotify's newest Soloist build (builds stop working 90 days after they're built).
# Written by scripts/setup_spotify.sh; started by $TIMER_UNIT. It needs no sudo.
#   journalctl --user-unit $UPDATE_UNIT -n 30

[Unit]
Description=Fetch a newer Spotify Soloist build for LittleBot's Spotify receiver
Documentation=file://$REPO_DIR/docs/SPOTIFY.md
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $REPO_DIR/scripts/setup_spotify.sh --update
Nice=10
MemoryMax=300M
TimeoutStartSec=15min
EOF
}

timer_text() {
    cat <<EOF
# LittleBot - once a week, look for a newer Spotify Soloist build ($UPDATE_UNIT).
# Written by scripts/setup_spotify.sh (--uninstall removes it).   systemctl --user list-timers

[Unit]
Description=Weekly check for a newer Spotify Soloist build (LittleBot's Spotify receiver)

[Timer]
OnCalendar=Mon *-*-* 04:20
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
FAILS=0
WARNS=0
pass() { printf '  %sPASS%s  %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn_item() { printf '  %sWARN%s  %s\n' "$C_YEL" "$C_OFF" "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  %sFAIL%s  %s\n' "$C_RED" "$C_OFF" "$*"; FAILS=$((FAILS + 1)); }

do_check() {
    FAILS=0
    WARNS=0
    step "Checking LittleBot's Spotify receiver (Spotify Soloist)"
    find_soloist
    if [[ -z "$SOLOIST" ]]; then
        fail "Soloist is not installed: run scripts/setup_spotify.sh"
        printf '\nSpotify on LittleBot is not set up.\n'
        return 1
    fi
    local version left
    version="$(soloist_version)"
    pass "Soloist is installed: $SOLOIST (${version:-its --version said nothing})"
    left="$(days_left "$version")"
    if [[ -z "$left" ]]; then
        warn_item "couldn't read the build's date from its --version: builds stop working 90 days after they're built (scripts/setup_spotify.sh --update)"
    elif ((left < 0)); then
        fail "this Soloist build expired ${left#-} day(s) ago: scripts/setup_spotify.sh --update"
    elif ((left <= RENEW_DAYS)); then
        warn_item "this Soloist build stops working in $left day(s): scripts/setup_spotify.sh --update"
    else
        pass "the build works for $left more day(s) (the weekly timer fetches newer ones)"
    fi
    if systemctl --user is-enabled --quiet "$TIMER_UNIT" 2>/dev/null; then
        pass "the weekly update timer is on ($TIMER_UNIT)"
    else
        warn_item "the weekly update timer is off: systemctl --user enable --now $TIMER_UNIT (or run scripts/setup_spotify.sh)"
    fi
    if key_saved; then
        pass "the Soloist API key is saved ($KEY_FILE)"
    else
        fail "no Soloist API key: run scripts/setup_spotify.sh --new-key (make one on $KEY_PAGE)"
    fi
    if [[ -f "$UNIT_FILE" && -x "$LAUNCHER" ]]; then
        if runs_librespot; then
            fail "the service still runs librespot: run scripts/setup_spotify.sh"
        else
            pass "the service and its launcher are installed ($UNIT_FILE)"
        fi
    else
        fail "the service isn't installed: run scripts/setup_spotify.sh"
    fi
    if systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        pass "it starts at login (enabled)"
    else
        fail "it isn't enabled: systemctl --user enable --now $UNIT_NAME"
    fi
    local running=0
    if systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        running=1
        pass "it is running"
    else
        fail "it isn't running: systemctl --user restart $UNIT_NAME, then journalctl --user-unit littlebot-spotify -n 30"
    fi
    case "$(raspotify_state)" in
        masked | "") ;;
        disabled) warn_item "raspotify's own system service is disabled but not masked (an upgrade could start it): scripts/setup_spotify.sh offers to remove raspotify" ;;
        *)
            if systemctl is-active --quiet raspotify.service 2>/dev/null; then
                fail "raspotify's own system service is running: a second \"$NAME\" (scripts/setup_spotify.sh offers to remove it)"
            else
                warn_item "raspotify's own system service is enabled: scripts/setup_spotify.sh offers to remove it"
            fi
            ;;
    esac
    if librespot_running; then
        fail "librespot (the old receiver) is still running: a second \"$NAME\" (scripts/setup_spotify.sh)"
    else
        pass "the old receiver (librespot) isn't running"
    fi
    if ((running)); then
        case "$(signed_in_rc)" in
            0) pass "the receiver is signed in to Spotify (it signs in by itself at every start)" ;;
            3)
                warn_item "the receiver isn't signed in yet: on a phone on the same Wi-Fi, open Spotify, tap the speaker icon and pick \"$NAME\" once (or scripts/setup_spotify.sh --pair)"
                if avahi_running && ! soloist_on_mdns; then
                    warn_item "Soloist doesn't have the mDNS port (avahi-daemon has it), so the Spotify app may not list \"$NAME\": scripts/setup_spotify.sh --pair can pause avahi-daemon while you pick it"
                fi
                ;;
            *) warn_item "couldn't ask the receiver whether it is signed in (its local API: soloist ctl status)" ;;
        esac
    fi
    if pulse_running; then
        pass "PipeWire is running: it plays through PipeWire"
    else
        warn_item "couldn't reach PipeWire (pactl info): Soloist needs PipeWire or PulseAudio running for the desktop user"
    fi
    local sink
    sink="$(default_sink)"
    if [[ "$sink" == "$EC_SINK" ]]; then
        pass "the default speaker is $EC_SINK (echo cancelled: music won't set off the wake word)"
    elif [[ -n "$sink" ]]; then
        warn_item "the default speaker is $sink, not $EC_SINK: music may set off the wake word (scripts/setup_echo_cancel.sh)"
    else
        warn_item "couldn't read the default speaker (pactl): check scripts/setup_echo_cancel.sh --check"
    fi
    if have journalctl; then
        local logtext
        logtext="$(journalctl --user-unit littlebot-spotify -n 200 --no-pager 2>/dev/null || true)"
        if grep -qi premium <<<"$logtext"; then
            warn_item "its log mentions Spotify Premium: LittleBot can only play on a Premium account"
        fi
        if grep -Eqi 'api.?key' <<<"$logtext" && grep -Eqi 'invalid|rejected|unauthori|login failed' <<<"$logtext"; then
            warn_item "its log mentions the API key being refused: scripts/setup_spotify.sh --new-key"
        fi
    fi
    if [[ -f "$TOKEN_FILE" ]]; then
        pass "LittleBot is connected to Spotify (the dashboard's Assistant page)"
    else
        warn_item "LittleBot isn't connected to Spotify yet: the dashboard's Assistant page, Connect Spotify (docs/SPOTIFY.md)"
    fi
    if ((FAILS)); then
        printf '\n%s problem(s) above: Spotify on LittleBot does not work yet.\n' "$FAILS"
        return 1
    fi
    printf '\nSpotify on LittleBot looks set up%s.\n' "$( ((WARNS)) && printf ' (%s warning(s) above)' "$WARNS" || true)"
    return 0
}

if ((CHECK)); then
    do_check
    exit $?
fi

# ---------------------------------------------------------------------------
# Fetching Soloist (setup and --update)
# ---------------------------------------------------------------------------
header_value() {  # header_value FILE NAME: the last response's value for that header
    tr -d '\r' <"$1" | grep -i "^$2:" | tail -1 | cut -d: -f2- | sed 's/^ *//; s/"//g' || true
}
restart_if_quiet() {  # restart the receiver for a new build, unless music is playing on it right now
    if ! systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null && \
        ! systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        rm -f "$STATE_DIR/restart-pending"
        return 0
    fi
    find_soloist
    if playing_now; then
        mkdir -p "$STATE_DIR"
        : >"$STATE_DIR/restart-pending"
        info "Music is playing on it: the new build starts at the next --update when nothing plays (or a restart)."
        return 0
    fi
    run systemctl --user restart "$UNIT_NAME"
    rm -f "$STATE_DIR/restart-pending"
}

# fetch_soloist RESTART(0|1): install Spotify's newest build to ~/.local/bin/soloist when it differs from the one
# there. Sets FETCHED=1 when it installed one. Dies on a bad download (the old build stays).
FETCHED=0
fetch_soloist() {
    local restart="$1"
    [[ -n "$ARCH" ]] || die "Soloist has no build for this computer ($(uname -m)); it needs aarch64, armv7l or x86_64."
    if ((DRY_RUN)); then
        printf '    [dry-run] curl --fail --location -o soloist.tar.gz %s\n' "$SOLOIST_URL"
        info "[dry-run] check it against the server's ETag (MD5)${WANT_SHA256:+ and SHA-256 $WANT_SHA256}, extract it, run soloist --version"
        printf '    [dry-run] install -m 755 soloist %s\n' "$SOLOIST_HOME_BIN"
        return 0
    fi
    local tool
    for tool in curl tar sha256sum md5sum; do
        have "$tool" || die "$tool is needed to fetch Soloist: sudo apt install curl tar coreutils"
    done
    mkdir -p "$STATE_DIR" "$BIN_DIR"
    local tmp
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/littlebot-soloist.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT
    local etag="" old_etag=""
    if curl --fail --silent --show-error --location --head --max-time 30 -o /dev/null -D "$tmp/head" "$SOLOIST_URL"; then
        etag="$(header_value "$tmp/head" etag)"
    else
        warn "couldn't ask Spotify's download server about the newest build (offline?)."
    fi
    old_etag="$(cat "$STATE_DIR/soloist-etag" 2>/dev/null || true)"
    if [[ -n "$etag" && "$etag" == "$old_etag" && -x "$SOLOIST_HOME_BIN" && -z "$WANT_SHA256" ]] && \
        [[ -n "$(soloist_version "$SOLOIST_HOME_BIN")" ]]; then
        info "Up to date: Spotify's newest build is the one installed ($(soloist_version "$SOLOIST_HOME_BIN"))."
        return 0
    fi
    info "Downloading Spotify's newest Soloist build for $ARCH ..."
    printf '    $ curl --fail --location -o soloist.tar.gz %s\n' "$SOLOIST_URL"
    curl --fail --silent --show-error --location --max-time 600 --retry 2 -D "$tmp/head" -o "$tmp/soloist.tar.gz" \
        "$SOLOIST_URL" || die "the download failed (the build installed stays)."
    etag="$(header_value "$tmp/head" etag)"
    local md5 sha
    md5="$(md5sum "$tmp/soloist.tar.gz" | cut -d' ' -f1)"
    sha="$(sha256sum "$tmp/soloist.tar.gz" | cut -d' ' -f1)"
    if [[ "$etag" =~ ^[0-9a-fA-F]{32}$ ]]; then
        [[ "${etag,,}" == "$md5" ]] || die "the download is damaged (its MD5 doesn't match the server's ETag); try again."
        info "The download matches the server's checksum (MD5 $md5)."
    else
        info "Spotify's server sent no plain checksum for it (ETag ${etag:-none}): it is checked by running it."
    fi
    if [[ -n "$WANT_SHA256" && "${WANT_SHA256,,}" != "$sha" ]]; then
        die "the download's SHA-256 is $sha, not $WANT_SHA256 (LITTLEBOT_SOLOIST_SHA256): not installed."
    fi
    info "SHA-256 of the archive: $sha"
    if tar -tzf "$tmp/soloist.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
        die "the archive has files outside its own folder: not installed."
    fi
    mkdir "$tmp/x"
    tar -xzf "$tmp/soloist.tar.gz" --no-same-owner -C "$tmp/x" || die "couldn't unpack the download."
    local new
    new="$(find "$tmp/x" -maxdepth 3 -type f -name soloist | head -1)"
    [[ -n "$new" ]] || die "the download has no soloist program in it."
    chmod 755 "$new"
    local version left
    version="$(soloist_version "$new")"
    [[ -n "$version" ]] || die "the downloaded soloist doesn't run on this computer (soloist --version said nothing)."
    left="$(days_left "$version")"
    if [[ -n "$left" ]] && ((left < 0)); then
        die "Spotify's newest build ($version) has already expired: try again in a day or two."
    fi
    if [[ -x "$SOLOIST_HOME_BIN" ]] && cmp -s "$new" "$SOLOIST_HOME_BIN"; then
        info "Up to date: $version is already installed."
        printf '%s\n' "$etag" >"$STATE_DIR/soloist-etag"
        return 0
    fi
    if [[ -x "$SOLOIST_HOME_BIN" ]]; then
        cp -f "$SOLOIST_HOME_BIN" "$SOLOIST_HOME_BIN.old"
    fi
    install -m 755 "$new" "$SOLOIST_HOME_BIN.new"
    mv -f "$SOLOIST_HOME_BIN.new" "$SOLOIST_HOME_BIN"
    printf '%s\n' "$etag" >"$STATE_DIR/soloist-etag"
    printf '%s  soloist_release_%s.tar.gz\n' "$sha" "$ARCH" >"$STATE_DIR/soloist-sha256"
    : >"$STATE_DIR/installed-soloist"
    info "Installed $SOLOIST_HOME_BIN: $version${left:+ ($left day(s) left)}."
    SOLOIST="$SOLOIST_HOME_BIN"
    FETCHED=1
    if ((restart)); then
        restart_if_quiet
    fi
}

if ((UPDATE)); then
    step "Looking for a newer Spotify Soloist build"
    fetch_soloist 1
    if ((!DRY_RUN && !FETCHED)) && [[ -f "$STATE_DIR/restart-pending" ]]; then
        info "A new build from an earlier run is waiting for a restart."
        restart_if_quiet
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# --pair
# ---------------------------------------------------------------------------
pair_once() {  # one pairing run: the receiver waits up to 3 minutes to be picked in the Spotify app
    local rc=0
    info "Now: on a phone on the same Wi-Fi, open Spotify, play anything, tap the speaker icon and pick \"$NAME\"."
    info "(It waits up to 3 minutes; Ctrl+C to give up.)"
    timeout 180 "$LAUNCHER" --pair || rc=$?
    return "$rc"
}

if ((PAIR)); then
    step "Signing the receiver in to Spotify again"
    find_soloist
    [[ -n "$SOLOIST" && -x "$LAUNCHER" ]] || die "Spotify on LittleBot isn't set up yet: run scripts/setup_spotify.sh"
    key_saved || die "no Soloist API key yet: run scripts/setup_spotify.sh --new-key"
    if ((DRY_RUN)); then
        run systemctl --user stop "$UNIT_NAME"
        run timeout 180 "$LAUNCHER" --pair
        run systemctl --user start "$UNIT_NAME"
        printf '\nDry run: nothing was changed.\n'
        exit 0
    fi
    interactive || die "pairing needs you at the terminal (ssh -t ...): run it again in a terminal."
    AVAHI_PAUSED=0
    # Ctrl+C: the receiver (and avahi-daemon, if you let it be paused) come back
    trap 'systemctl --user start "$UNIT_NAME" >/dev/null 2>&1 || true; if ((AVAHI_PAUSED)); then sudo systemctl start avahi-daemon.socket avahi-daemon.service || true; fi' EXIT
    run systemctl --user stop "$UNIT_NAME" || true
    PAIRED=0
    if pair_once; then
        PAIRED=1
    elif avahi_running && ask "Didn't work. avahi-daemon may have the port Spotify apps use to find speakers. Pause avahi-daemon during the pairing (needs sudo; littlebot.local stops answering for a moment)?"; then
        AVAHI_PAUSED=1
        run sudo systemctl stop avahi-daemon.socket avahi-daemon.service || true
        if pair_once; then
            PAIRED=1
        fi
        run sudo systemctl start avahi-daemon.socket avahi-daemon.service || warn "start it again: sudo systemctl start avahi-daemon"
        AVAHI_PAUSED=0
    fi
    run systemctl --user start "$UNIT_NAME"
    if ((PAIRED)); then
        printf '\nSigned in: "%s" now signs in by itself at every start.\n' "$NAME"
        exit 0
    fi
    printf '\nNot signed in. Check the API key (--new-key) and that the phone is on the same Wi-Fi (docs/SPOTIFY.md).\n'
    exit 1
fi

# ---------------------------------------------------------------------------
# --uninstall
# ---------------------------------------------------------------------------
if ((UNINSTALL)); then
    step "Removing LittleBot's Spotify receiver"
    if unit_known; then
        info "Stopping the service and stopping it from starting at login."
        run systemctl --user disable --now "$UNIT_NAME" || warn "couldn't stop $UNIT_NAME (already gone?)"
    else
        info "The service isn't installed."
    fi
    if [[ -f "$TIMER_FILE" ]] || systemctl --user is-enabled --quiet "$TIMER_UNIT" 2>/dev/null; then
        run systemctl --user disable --now "$TIMER_UNIT" || warn "couldn't stop $TIMER_UNIT (already gone?)"
    fi
    for f in "$UNIT_FILE" "$UPDATE_FILE" "$TIMER_FILE" "$LAUNCHER"; do
        if [[ -e "$f" ]]; then
            run rm -f "$f"
        fi
    done
    run systemctl --user daemon-reload || true
    if [[ -f "$STATE_DIR/installed-soloist" ]]; then
        info "Removing Soloist (this script installed it)."
        run rm -f "$SOLOIST_HOME_BIN" "$SOLOIST_HOME_BIN.old"
    fi
    for d in "$DATA_DIR" "$CACHE_DIR" "$OLD_CACHE_DIR"; do
        if [[ -d "$d" ]]; then
            info "Forgetting the receiver's sign-in and cache: $d"
            run rm -rf "$d"
        fi
    done
    if [[ -d "$STATE_DIR" ]]; then
        info "Deleting the Soloist API key and this script's notes ($STATE_DIR)."
        run rm -rf "$STATE_DIR"
    fi
    if raspotify_installed; then
        info "raspotify (the old librespot receiver) is still installed. To remove it: sudo apt-get remove raspotify"
    fi
    info "LittleBot's own sign-in (data/spotify_token.json) stays: Disconnect Spotify on the dashboard to forget it."
    printf '\nDone: LittleBot is no longer a Spotify speaker. Music plays from YouTube again.\n'
    exit 0
fi

# ---------------------------------------------------------------------------
# Set up
# ---------------------------------------------------------------------------
printf 'Spotify on LittleBot with Spotify Soloist (needs Spotify Premium)%s\n' "$( ((DRY_RUN)) && printf ' - dry run: nothing is changed' || true)"

step "1/5 Spotify Soloist, the Spotify Connect receiver"
find_soloist
if [[ -n "$SOLOIST" ]]; then
    VERSION="$(soloist_version)"
    LEFT="$(days_left "$VERSION")"
    info "Already installed: $SOLOIST (${VERSION:-?}$( [[ -n "$LEFT" ]] && printf ', %s' "$(expiry_text "$VERSION")" || true))."
    if [[ -n "$LEFT" ]] && ((LEFT <= RENEW_DAYS)); then
        info "It stops working soon: fetching Spotify's newest build."
        fetch_soloist 0
    fi
else
    info "Downloading it from Spotify (no sudo: it goes in $BIN_DIR)."
    fetch_soloist 0
    find_soloist
fi

step "2/5 The old receiver (librespot, from raspotify): off"
if runs_librespot; then
    info "The service ran librespot: from now on it runs Soloist (the same service, so never two \"$NAME\"s)."
fi
if [[ -d "$OLD_CACHE_DIR" ]]; then
    info "Deleting librespot's sign-in (it isn't used any more)."
    run rm -rf "$OLD_CACHE_DIR"
fi
REMOVED_RASPOTIFY=0
if raspotify_installed; then
    info "raspotify (librespot) is still installed. Removing it needs sudo (your password)."
    if ask "Remove the raspotify package now (sudo apt-get remove -y raspotify)?"; then
        run sudo apt-get remove -y raspotify && REMOVED_RASPOTIFY=1 || warn "couldn't remove raspotify (see above)."
    else
        info "Left installed. To remove it later: sudo apt-get remove raspotify"
    fi
fi
STATE="$(raspotify_state)"
if ((!REMOVED_RASPOTIFY)) && [[ -n "$STATE" && "$STATE" != masked ]]; then
    info "raspotify's own system service is $STATE: it would be a second \"$NAME\", outside the echo canceller."
    if ask "Switch it off (sudo systemctl disable --now raspotify, then mask it)?"; then
        run sudo systemctl disable --now raspotify.service || true
        run sudo systemctl mask raspotify.service || true
    else
        info "Left as it is. To switch it off: sudo systemctl disable --now raspotify && sudo systemctl mask raspotify"
    fi
else
    info "raspotify's own system service is off."
fi

step "3/5 Your Soloist API key"
if key_saved && ((!NEW_KEY)); then
    info "Saved already ($KEY_FILE). To use another: scripts/setup_spotify.sh --new-key"
elif ((DRY_RUN)); then
    info "[dry-run] would ask for your Soloist API key (it isn't shown as you type) and save it to $KEY_FILE (chmod 600)"
else
    KEY="${SOLOIST_API_KEY:-}"
    if [[ -z "$KEY" ]]; then
        interactive || die "the Soloist API key is needed: run this in a terminal (ssh -t ...) so it can ask for it."
        info "Soloist needs an API key from a Spotify Premium account:"
        info "  1. Open $KEY_PAGE and log in with your Spotify account."
        info "  2. Accept the terms if it asks, and press the button to make a key. Copy it."
        info "It is kept only on the robot, in $KEY_FILE (only you can read it), and never shown."
        printf '    Paste the Soloist API key here (nothing shows as you type), then press Enter: '
        read -rs KEY || KEY=""
        printf '\n'
    else
        info "Using the key in SOLOIST_API_KEY."
    fi
    KEY="${KEY#"${KEY%%[![:space:]]*}"}"
    KEY="${KEY%"${KEY##*[![:space:]]}"}"
    [[ "$KEY" =~ ^[A-Za-z0-9._~+/=:-]{16,512}$ ]] || die "that doesn't look like a Soloist API key (nothing was saved). Run it again."
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    (umask 077 && printf 'SOLOIST_API_KEY=%s\n' "$KEY" >"$KEY_FILE.new" && mv -f "$KEY_FILE.new" "$KEY_FILE")
    chmod 600 "$KEY_FILE"
    unset KEY
    info "Saved to $KEY_FILE (chmod 600)."
    CHANGED=1
fi

step "4/5 The receiver as your user service ($UNIT_NAME), and a weekly update"
BIN="${SOLOIST:-$SOLOIST_HOME_BIN}"
info "Soloist as \"$NAME\", starting at volume $VOLUME%, through PipeWire into the default speaker; its local"
info "API on 127.0.0.1 only. $TIMER_UNIT looks for a newer build every Monday morning."
run mkdir -p "$UNIT_DIR" "$BIN_DIR"
write_file "$LAUNCHER" 755 < <(launcher_text "$BIN") || true
write_file "$UNIT_FILE" 644 < <(unit_text) || true
write_file "$UPDATE_FILE" 644 < <(update_unit_text) || true
write_file "$TIMER_FILE" 644 < <(timer_text) || true

step "5/5 Start it (and at every login)"
if ! { run systemctl --user daemon-reload && run systemctl --user enable "$UNIT_NAME" "$TIMER_UNIT"; }; then
    warn "Could not reach your systemd user manager (no login session?). Log in on the desktop or via SSH and run:"
    warn "  systemctl --user daemon-reload && systemctl --user enable --now $UNIT_NAME $TIMER_UNIT"
    exit 1
fi
if ((DRY_RUN)) || ! systemctl --user is-active --quiet "$TIMER_UNIT" 2>/dev/null; then
    run systemctl --user start "$TIMER_UNIT" || true
fi
if ((CHANGED || FETCHED)) || ! systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null || ((DRY_RUN)); then
    run systemctl --user restart "$UNIT_NAME"
    rm -f "$STATE_DIR/restart-pending" 2>/dev/null || true
else
    info "Running and up to date: no restart needed."
fi

if ((DRY_RUN)); then
    printf '\nDry run: nothing was changed.\n'
    exit 0
fi
sleep "${LITTLEBOT_SPOTIFY_SETTLE_S:-3}"  # let it start before asking it whether it is signed in
do_check || true
cat <<EOF

Next (docs/SPOTIFY.md):
  1. Once: on a phone on the same Wi-Fi, open Spotify, tap the speaker icon and pick "$NAME".
     (Not in the list? scripts/setup_spotify.sh --pair)
  2. On the dashboard's Assistant page: enter your Spotify app's Client ID and press Connect Spotify
     (already connected from before? Nothing to do).
Then say "Hey LittleBot, play some jazz".
EOF
