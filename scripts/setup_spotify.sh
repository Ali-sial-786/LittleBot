#!/usr/bin/env bash
# LittleBot - Spotify Connect on the robot: a speaker called "LittleBot" in every Spotify app, which LittleBot
# itself plays music on ("play some jazz"). Spotify only allows this with Spotify Premium.
#
#   scripts/setup_spotify.sh              # set it up (or bring it up to date); safe to run again
#   scripts/setup_spotify.sh --dry-run    # print what it would do and the files it would write, change nothing
#   scripts/setup_spotify.sh --check      # is it working? (PASS / WARN / FAIL per item, changes nothing)
#   scripts/setup_spotify.sh --uninstall  # undo everything it did
#
# UNTESTED ON A REAL RASPBERRY PI: written from the raspotify and librespot documentation and tested only with
# fake commands (tests/test_spotify_script.py). Run --check on the robot.
#
# Run it as the desktop user (no sudo: it asks for your password where it needs it). What it does, step by step
# (each step also says so as it runs):
#   1. Installs librespot, the open-source Spotify Connect receiver, with raspotify's official install script
#      (https://dtcooper.github.io/raspotify/install.sh: it adds raspotify's apt repository and installs the
#      raspotify package, which contains /usr/bin/librespot). Skipped when librespot is already there.
#   2. Switches off raspotify's own system service (raspotify.service: stopped, disabled and masked, so an
#      upgrade doesn't start it again). It would be a second receiver, running as a system user outside your
#      sound session, straight to the sound card: no echo cancelling, and two "speakers" fighting over it.
#   3. Writes ~/.local/bin/littlebot-spotify (a small launcher) and ~/.config/systemd/user/littlebot-spotify.service.
#      librespot runs as YOUR user, so it plays through your PipeWire session into the default speaker: the
#      echo-cancelled littlebot_ec_speaker (scripts/setup_echo_cancel.sh), whose sound the microphone hears
#      taken out, so music never sets off the wake word. The launcher uses PipeWire's PulseAudio server
#      (pipewire-pulse) and falls back to ALSA when that isn't there. librespot's settings: --name LittleBot,
#      --device-type speaker, --bitrate 160, --initial-volume 60, --disable-audio-cache (no music is stored),
#      autoplay off (nothing plays by itself after what was asked for). The receiver keeps only its sign-in
#      (credentials) in ~/.cache/littlebot-spotify.
#   4. Enables the service (it starts at login, like LittleBot) and (re)starts it, then runs --check.
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
NAME="LittleBot"
BITRATE=160
VOLUME=60

UNIT_NAME="littlebot-spotify.service"
INSTALL_URL="${LITTLEBOT_SPOTIFY_INSTALL_URL:-https://dtcooper.github.io/raspotify/install.sh}"
EC_SINK="littlebot_ec_speaker"

usage() {
    cat <<'EOF'
Usage: scripts/setup_spotify.sh [options]

Makes the robot a Spotify Connect speaker called "LittleBot" (librespot, from raspotify), running as your user
so it plays through PipeWire into the echo-cancelled speaker. Needs Spotify Premium. Run it as the desktop user
(no sudo; it asks for your password to install the package). Untested on a real Raspberry Pi: run --check
afterwards. docs/SPOTIFY.md explains the rest (the developer app, connecting on the dashboard).

Options:
  --dry-run          print every step and the files it would write, change nothing
  --check            report whether it works (changes nothing; exit 1 on a FAIL)
  --uninstall        undo it: stop and remove the service, the launcher and the receiver's sign-in; raspotify's
                     own service is unmasked (and the package removed when this script installed it)
  --name NAME        the speaker's name in Spotify (default LittleBot; LittleBot looks for it by this name:
                     skills.spotify_device_name)
  --bitrate KBPS     96, 160 or 320 (default 160)
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
        --name) shift; NAME="${1:?--name needs a name}" ;;
        --name=*) NAME="${1#*=}" ;;
        --bitrate) shift; BITRATE="${1:?--bitrate needs 96, 160 or 320}" ;;
        --bitrate=*) BITRATE="${1#*=}" ;;
        --volume) shift; VOLUME="${1:?--volume needs a percent}" ;;
        --volume=*) VOLUME="${1#*=}" ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if ((CHECK + UNINSTALL > 1)); then
    echo "Choose one of --check and --uninstall." >&2
    exit 2
fi
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9\ _-]{0,29}$ ]] || { echo "--name: letters, digits, spaces, - and _ (30 at most)" >&2; exit 2; }
[[ "$BITRATE" =~ ^(96|160|320)$ ]] || { echo "--bitrate must be 96, 160 or 320" >&2; exit 2; }
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
LAUNCHER="$HOME/.local/bin/littlebot-spotify"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/littlebot-spotify"
STATE_DIR="$CONF_HOME/littlebot-spotify"  # what this script changed, so --uninstall undoes only that
TOKEN_FILE="$REPO_DIR/data/spotify_token.json"

LIBRESPOT="${LITTLEBOT_SPOTIFY_LIBRESPOT:-}"
find_librespot() {
    if [[ -n "$LIBRESPOT" && -x "$LIBRESPOT" ]]; then
        return 0
    fi
    LIBRESPOT=""
    if have librespot; then
        LIBRESPOT="$(command -v librespot)"
    elif [[ -x /usr/bin/librespot ]]; then
        LIBRESPOT=/usr/bin/librespot
    fi
}
librespot_version() {
    "$LIBRESPOT" --version 2>/dev/null | head -1 || true
}
# librespot 0.5+ takes `--autoplay on|off` (its help shows "--autoplay OVERRIDE"); older ones have a plain
# --autoplay flag (off unless given).
autoplay_option() {
    if "$LIBRESPOT" --help 2>&1 | grep -Eq -- '--autoplay [A-Z<]'; then
        printf '%s' "--autoplay off"
    fi
}
supports_pulse() {
    "$LIBRESPOT" --backend '?' 2>&1 | grep -qi pulseaudio
}
pulse_running() {
    have pactl && pactl info >/dev/null 2>&1
}
alsa_device() {
    if have aplay && aplay -L 2>/dev/null | grep -qx "$EC_SINK"; then
        printf '%s' "$EC_SINK"
    else
        printf 'default'
    fi
}
default_sink() {
    if have pactl; then
        pactl get-default-sink 2>/dev/null || true
    fi
}
raspotify_known() {
    have systemctl && systemctl list-unit-files raspotify.service --no-legend 2>/dev/null | grep -q raspotify
}
raspotify_state() {  # enabled | disabled | masked | ... ("" when it isn't installed)
    if raspotify_known; then
        systemctl is-enabled raspotify.service 2>/dev/null || true
    fi
}

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
    local autoplay="$1" alsa="$2" bin="$3"
    cat <<EOF
#!/bin/sh
# LittleBot's Spotify Connect receiver: librespot as "$NAME" (written by scripts/setup_spotify.sh, started by
# $UNIT_NAME; run the script again to change it). It plays through PipeWire's PulseAudio server into the
# default speaker (the echo-cancelled $EC_SINK), or through ALSA ($alsa) when that server isn't there.
# No music is stored: only the receiver's sign-in, in $CACHE_DIR.
set -eu
LIBRESPOT="$bin"
RUNTIME="\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}"
if [ -S "\$RUNTIME/pulse/native" ] && "\$LIBRESPOT" --backend '?' 2>&1 | grep -qi pulseaudio; then
    set -- --backend pulseaudio
    echo "littlebot-spotify: playing through PipeWire (its PulseAudio server)" >&2
else
    set -- --backend alsa --device "$alsa"
    echo "littlebot-spotify: no PulseAudio server (pipewire-pulse): playing through ALSA ($alsa)" >&2
fi
mkdir -p "$CACHE_DIR"
exec "\$LIBRESPOT" --name "$NAME" --device-type speaker --bitrate $BITRATE --initial-volume $VOLUME \\
    --cache "$CACHE_DIR" --disable-audio-cache ${autoplay:+$autoplay }"\$@"
EOF
}

unit_text() {
    cat <<EOF
# LittleBot - the robot's Spotify Connect receiver (librespot as "$NAME"), a systemd USER service.
# Written by scripts/setup_spotify.sh (run it again to change it; --uninstall removes it). docs/SPOTIFY.md
#   systemctl --user status littlebot-spotify      journalctl --user-unit littlebot-spotify -f
#   systemctl --user restart littlebot-spotify
# Needs Spotify Premium. It runs in your session (like LittleBot), so its sound goes through PipeWire.

[Unit]
Description=LittleBot's Spotify Connect receiver (librespot as "$NAME")
Documentation=file://$REPO_DIR/docs/SPOTIFY.md
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire-pulse.service
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStart=$LAUNCHER
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
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
    step "Checking LittleBot's Spotify receiver"
    find_librespot
    if [[ -z "$LIBRESPOT" ]]; then
        fail "librespot is not installed: run scripts/setup_spotify.sh"
        printf '\nSpotify on LittleBot is not set up.\n'
        return 1
    fi
    pass "librespot is installed: $LIBRESPOT ($(librespot_version))"
    case "$(raspotify_state)" in
        masked | "") pass "raspotify's own system service is off" ;;
        disabled) warn_item "raspotify's own system service is disabled but not masked (an upgrade could start it)" ;;
        *)
            if systemctl is-active --quiet raspotify.service 2>/dev/null; then
                fail "raspotify's own system service is running: a second receiver (run scripts/setup_spotify.sh)"
            else
                warn_item "raspotify's own system service is enabled (run scripts/setup_spotify.sh)"
            fi
            ;;
    esac
    if [[ -f "$UNIT_FILE" && -x "$LAUNCHER" ]]; then
        pass "the service and its launcher are installed ($UNIT_FILE)"
    else
        fail "the service isn't installed: run scripts/setup_spotify.sh"
    fi
    if systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        pass "it starts at login (enabled)"
    else
        fail "it isn't enabled: systemctl --user enable --now $UNIT_NAME"
    fi
    if systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        pass "it is running"
    else
        fail "it isn't running: systemctl --user restart $UNIT_NAME, then journalctl --user-unit littlebot-spotify -n 30"
    fi
    if supports_pulse; then
        if pulse_running; then
            pass "PipeWire's PulseAudio server is running: it plays through PipeWire"
        else
            warn_item "PipeWire's PulseAudio server (pipewire-pulse) isn't running: it plays through ALSA ($(alsa_device))"
        fi
    else
        warn_item "this librespot has no PulseAudio backend: it plays through ALSA ($(alsa_device))"
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
    if [[ -n "$(ls -A "$CACHE_DIR" 2>/dev/null | grep -i credentials || true)" ]]; then
        pass "the receiver is signed in to Spotify (it signs in by itself at every start)"
    else
        warn_item "the receiver isn't signed in yet: on a phone on the same Wi-Fi, open Spotify, tap the speaker icon and pick \"$NAME\" once"
    fi
    if have journalctl && journalctl --user-unit littlebot-spotify -n 200 --no-pager 2>/dev/null | grep -qi premium; then
        warn_item "its log mentions Spotify Premium: LittleBot can only play on a Premium account"
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
# --uninstall
# ---------------------------------------------------------------------------
if ((UNINSTALL)); then
    step "Removing LittleBot's Spotify receiver"
    if [[ -f "$UNIT_FILE" ]] || systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        info "Stopping the service and stopping it from starting at login."
        run systemctl --user disable --now "$UNIT_NAME" || warn "couldn't stop $UNIT_NAME (already gone?)"
    else
        info "The service isn't installed."
    fi
    for f in "$UNIT_FILE" "$LAUNCHER"; do
        if [[ -e "$f" ]]; then
            run rm -f "$f"
        fi
    done
    run systemctl --user daemon-reload || true
    if [[ -d "$CACHE_DIR" ]]; then
        info "Forgetting the receiver's sign-in (no music was stored)."
        run rm -rf "$CACHE_DIR"
    fi
    if [[ "$(raspotify_state)" == "masked" ]]; then
        info "raspotify's own system service: unmasked (back as raspotify left it)."
        run sudo systemctl unmask raspotify.service
        if [[ -f "$STATE_DIR/raspotify-was-enabled" ]]; then
            run sudo systemctl enable --now raspotify.service
        fi
    fi
    if [[ -f "$STATE_DIR/installed-raspotify" ]]; then
        info "This script installed raspotify: removing the package (sudo apt-get remove raspotify)."
        run sudo apt-get remove -y raspotify
    else
        info "raspotify's package stays (this script didn't install it). To remove it: sudo apt-get remove raspotify"
    fi
    if [[ -d "$STATE_DIR" ]]; then
        run rm -rf "$STATE_DIR"
    fi
    info "LittleBot's own sign-in (data/spotify_token.json) stays: Disconnect Spotify on the dashboard to forget it."
    printf '\nDone: LittleBot is no longer a Spotify speaker. Music plays from YouTube again.\n'
    exit 0
fi

# ---------------------------------------------------------------------------
# Set up
# ---------------------------------------------------------------------------
printf 'Spotify on LittleBot (needs Spotify Premium)%s\n' "$( ((DRY_RUN)) && printf ' - dry run: nothing is changed' || true)"

step "1/4 librespot, the Spotify Connect receiver (from raspotify)"
find_librespot
INSTALLED_NOW=0
if [[ -n "$LIBRESPOT" ]]; then
    info "Already installed: $LIBRESPOT ($(librespot_version))."
else
    have curl || die "curl is needed to fetch raspotify's install script: sudo apt install curl"
    info "Installing raspotify with its official install script: it adds raspotify's apt repository and installs"
    info "the raspotify package (librespot). It asks for your password (sudo)."
    if ((DRY_RUN)); then
        printf '    [dry-run] curl -sSfL %s | sh\n' "$INSTALL_URL"
    else
        printf '    $ curl -sSfL %s | sh\n' "$INSTALL_URL"
        curl -sSfL "$INSTALL_URL" | sh
        find_librespot
        [[ -n "$LIBRESPOT" ]] || die "librespot still isn't installed (see the messages above)."
        info "Installed: $LIBRESPOT ($(librespot_version))."
        INSTALLED_NOW=1
    fi
fi

step "2/4 raspotify's own system service: off"
info "raspotify comes with its own receiver as a system service; LittleBot runs librespot as your user instead"
info "(through your sound session and the echo canceller), so raspotify's is stopped, disabled and masked."
STATE="$(raspotify_state)"
WAS_ENABLED=0
case "$STATE" in
    masked) info "Already off (masked)." ;;
    "")
        if ((DRY_RUN)) && [[ -z "$LIBRESPOT" ]]; then
            run sudo systemctl disable --now raspotify.service
            run sudo systemctl mask raspotify.service
        else
            info "raspotify's service isn't installed: nothing to switch off."
        fi
        ;;
    *)
        if [[ "$STATE" == "enabled" ]]; then
            WAS_ENABLED=1
        fi
        run sudo systemctl disable --now raspotify.service
        run sudo systemctl mask raspotify.service
        ;;
esac

step "3/4 The receiver as your user service ($UNIT_NAME)"
if [[ -n "$LIBRESPOT" ]]; then
    AUTOPLAY="$(autoplay_option)"
    BIN="$LIBRESPOT"
else
    AUTOPLAY="--autoplay off"  # (a dry run before raspotify is installed: its librespot takes on/off)
    BIN=/usr/bin/librespot
fi
ALSA_DEVICE="$(alsa_device)"
info "librespot as \"$NAME\": a speaker, $BITRATE kbit/s, starting at volume $VOLUME%, no audio cache, autoplay off;"
info "through PipeWire's PulseAudio server, else ALSA ($ALSA_DEVICE)."
run mkdir -p "$UNIT_DIR" "$(dirname "$LAUNCHER")"
write_file "$LAUNCHER" 755 < <(launcher_text "$AUTOPLAY" "$ALSA_DEVICE" "$BIN") || true
write_file "$UNIT_FILE" 644 < <(unit_text) || true
if ((!DRY_RUN)); then
    mkdir -p "$STATE_DIR"
    if ((INSTALLED_NOW)); then
        : >"$STATE_DIR/installed-raspotify"
    fi
    if ((WAS_ENABLED)); then
        : >"$STATE_DIR/raspotify-was-enabled"
    fi
fi

step "4/4 Start it (and at every login)"
if ! { run systemctl --user daemon-reload && run systemctl --user enable "$UNIT_NAME"; }; then
    warn "Could not reach your systemd user manager (no login session?). Log in on the desktop or via SSH and run:"
    warn "  systemctl --user daemon-reload && systemctl --user enable --now $UNIT_NAME"
    exit 1
fi
if ((CHANGED)) || ! systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null || ((DRY_RUN)); then
    run systemctl --user restart "$UNIT_NAME"
else
    info "Running and up to date: no restart needed."
fi

if ((DRY_RUN)); then
    printf '\nDry run: nothing was changed.\n'
    exit 0
fi
do_check || true
cat <<EOF

Next (docs/SPOTIFY.md):
  1. Once: on a phone on the same Wi-Fi, open Spotify, tap the speaker icon and pick "$NAME".
  2. On the dashboard's Assistant page: enter your Spotify app's Client ID and press Connect Spotify.
Then say "Hey LittleBot, play some jazz".
EOF
