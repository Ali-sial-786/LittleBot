#!/usr/bin/env bash
# LittleBot - Raspberry Pi installer (idempotent and non-destructive).
#
# Run it as your normal user from the repository; it uses sudo only for apt and for the
# optional system-level steps you ask for. Safe to re-run at any time.
#
# It never: overwrites config/local.yaml or .env, deletes or recreates an existing .venv,
# enables physical servo motion, enables I2C unless you pass --enable-i2c, or edits
# config.txt / cmdline.txt (display settings are manual - docs/DISPLAY_SETUP.md).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
CURRENT_USER="$(id -un)"
CURRENT_UID="$(id -u)"

DRY_RUN=0
ASSUME_YES=0
WITH_SERVICE=0
HEADLESS_SERVICE=0
ENABLE_I2C=0
SYSTEM_OPENCV=0
SKIP_APT=0
NO_CHROMIUM=0
WITH_DEV=0
ALLOW_NON_PI=0
NO_MODELS=0
NO_LIVE=0
NO_WAKE=0
NO_AUDIO_DEFAULT=0

usage() {
    cat <<'EOF'
Usage: scripts/install_pi.sh [options]

Installs system packages, creates .venv, installs LittleBot with the vision/audio/pi
extras, the live extra (GPT-Live voice) and the wake word ("Hey LittleBot": Vosk + a small
offline English model), creates data/ and config/local.yaml (from
config/hardware.example.yaml) if absent.

Options:
  --dry-run            print every action, change nothing
  --yes, -y            do not ask for confirmation
  --with-service       start LittleBot automatically on desktop login
                       (systemd user service + XDG autostart entry; recommended)
  --headless-service   install the HEADLESS system service instead (dashboard, no face)
  --enable-i2c         enable the I2C bus for the future PCA9685 servo board
                       (servos still stay disabled: motion.physical_enabled=false)
  --system-opencv      use apt python3-opencv (venv with --system-site-packages)
                       instead of the opencv-python-headless pip wheel
  --skip-apt           do not install system packages
  --no-chromium        do not install Chromium (YouTube player)
  --dev                also install test tools (pytest) to run the test-suite on the Pi
  --no-models          do not download models (scripts/download_models.py): the YuNet face
                       detector and the wake-word speech model
  --no-live            do not install live voice (the "live" extra: aiortc WebRTC, ~40 MB of
                       prebuilt wheels; docs/CHATGPT.md). Sign in with ChatGPT text works without it
  --no-wake-word       do not install the wake word ("Hey LittleBot" starts live voice): the
                       "local-stt" extra (Vosk, offline) and its 40 MB English model
  --no-audio-default   do not make the USB speaker / USB microphone the default sound devices
                       (scripts/set_default_speaker.sh; otherwise sound may go to the HDMI screen)
  --allow-non-pi       allow running on a non-Raspberry-Pi Debian system (testing)
  -h, --help           show this help
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes | -y) ASSUME_YES=1 ;;
        --with-service) WITH_SERVICE=1 ;;
        --headless-service) HEADLESS_SERVICE=1 ;;
        --enable-i2c) ENABLE_I2C=1 ;;
        --system-opencv) SYSTEM_OPENCV=1 ;;
        --skip-apt) SKIP_APT=1 ;;
        --no-chromium) NO_CHROMIUM=1 ;;
        --dev) WITH_DEV=1 ;;
        --no-models) NO_MODELS=1 ;;
        --no-live) NO_LIVE=1 ;;
        --no-wake-word) NO_WAKE=1 ;;
        --no-audio-default) NO_AUDIO_DEFAULT=1 ;;
        --allow-non-pi) ALLOW_NON_PI=1 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ((WITH_SERVICE && HEADLESS_SERVICE)); then
    echo "Choose either --with-service (face on screen) or --headless-service, not both." >&2
    exit 2
fi

if [[ -t 1 ]]; then
    C_BLUE=$'\033[1;34m'; C_YEL=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_OFF=$'\033[0m'
else
    C_BLUE=""; C_YEL=""; C_RED=""; C_OFF=""
fi
step() { printf '\n%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die() { printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

# Run a command, or only print it in --dry-run mode.
run() {
    if ((DRY_RUN)); then
        printf '    [dry-run] %s\n' "$*"
    else
        printf '    $ %s\n' "$*"
        "$@"
    fi
}

# Write stdin to a file (as the user, or via sudo for system paths), honouring --dry-run.
# An existing file with different content is backed up first (<file>.bak-<timestamp>).
write_file() {
    local dest="$1" use_sudo="${2:-}" content backup
    content="$(cat)"
    if ((DRY_RUN)); then
        printf '    [dry-run] write %s\n' "$dest"
        return 0
    fi
    if [[ -f "$dest" ]]; then
        if [[ "$(cat "$dest")" == "$content" ]]; then
            printf '    %s is up to date\n' "$dest"
            return 0
        fi
        backup="$dest.bak-$(date +%Y%m%d-%H%M%S)"
        printf '    back up %s -> %s\n' "$dest" "$backup"
        if [[ "$use_sudo" == "sudo" ]]; then sudo cp -p "$dest" "$backup"; else cp -p "$dest" "$backup"; fi
    fi
    printf '    write %s\n' "$dest"
    if [[ "$use_sudo" == "sudo" ]]; then
        printf '%s\n' "$content" | sudo tee "$dest" >/dev/null
    else
        printf '%s\n' "$content" >"$dest"
    fi
}

# Fill the @REPO_DIR@ / @USER@ / @UID@ placeholders of a template.
render() {
    local esc_repo
    esc_repo="$(printf '%s' "$REPO_DIR" | sed 's/[&|\\]/\\&/g')"
    sed -e "s|@REPO_DIR@|$esc_repo|g" -e "s|@USER@|$CURRENT_USER|g" -e "s|@UID@|$CURRENT_UID|g" "$1"
}

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
pkg_available() {
    local cand
    cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    [[ -n "$cand" && "$cand" != "(none)" ]]
}

# ---------------------------------------------------------------------------
# 1. Pre-flight checks
# ---------------------------------------------------------------------------
step "Checking this system"

if [[ "$(id -u)" -eq 0 && $DRY_RUN -eq 0 ]]; then
    die "Run this as your normal user (not root / not with sudo). It calls sudo itself when needed."
fi

OS_ID="" OS_LIKE="" OS_CODENAME="" OS_PRETTY=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    OS_ID="$(. /etc/os-release && echo "${ID:-}")"
    # shellcheck disable=SC1091
    OS_LIKE="$(. /etc/os-release && echo "${ID_LIKE:-}")"
    # shellcheck disable=SC1091
    OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
    # shellcheck disable=SC1091
    OS_PRETTY="$(. /etc/os-release && echo "${PRETTY_NAME:-}")"
fi
if [[ "$OS_ID" != "debian" && "$OS_ID" != "raspbian" && "$OS_LIKE" != *debian* ]]; then
    die "This installer is for Raspberry Pi OS / Debian (found: ${OS_PRETTY:-unknown})."
fi
info "OS: $OS_PRETTY"
case "$OS_CODENAME" in
    bookworm) info "Debian 12 'bookworm' based (Python 3.11, labwc or wayfire)." ;;
    trixie) info "Debian 13 'trixie' based (Python 3.13, labwc)." ;;
    *) warn "Untested release '$OS_CODENAME' - continuing; Bookworm and Trixie are the tested targets." ;;
esac

PI_MODEL=""
if [[ -r /proc/device-tree/model ]]; then
    PI_MODEL="$(tr -d '\0' </proc/device-tree/model)"
fi
if [[ "$PI_MODEL" == *"Raspberry Pi"* ]]; then
    info "Board: $PI_MODEL"
    [[ "$PI_MODEL" == *"Raspberry Pi 5"* ]] || warn "LittleBot is developed for the Raspberry Pi 5."
elif ((ALLOW_NON_PI)); then
    warn "Not a Raspberry Pi - continuing because of --allow-non-pi."
else
    die "This does not look like a Raspberry Pi. Use --allow-non-pi to install on another Debian system."
fi

ARCH="$(uname -m)"
BITS="$(getconf LONG_BIT 2>/dev/null || echo 64)"
if [[ "$BITS" != "64" ]]; then
    die "32-bit userland detected ($ARCH). Flash the 64-bit Raspberry Pi OS (pip wheels for pygame-ce/OpenCV are aarch64)."
fi
info "Architecture: $ARCH ($BITS-bit)"

PY_SYS="$(command -v python3 || true)"
[[ -n "$PY_SYS" ]] || die "python3 not found."
if ! "$PY_SYS" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    die "Python >= 3.11 is required (found $("$PY_SYS" -V 2>&1))."
fi
info "System Python: $("$PY_SYS" -V 2>&1)"

if ((WITH_SERVICE || HEADLESS_SERVICE)) && [[ "$REPO_DIR" =~ [[:space:]] ]]; then
    die "The repository path contains spaces ('$REPO_DIR'); systemd units cannot use it. Move it, e.g. to ~/LittleBot."
fi

FREE_KB="$(df -Pk "$REPO_DIR" | awk 'NR==2 {print $4}')"
if [[ -n "$FREE_KB" ]] && ((FREE_KB < 2000000)); then
    warn "Less than 2 GB free on the SD card ($((FREE_KB / 1024)) MB). Installation needs ~1.5 GB with Chromium."
fi

# Chromium: Bookworm ships Raspberry Pi's build as 'chromium-browser'; Trixie / Debian use
# 'chromium'. Prefer the one matching the release, fall back to whichever apt offers.
CHROMIUM_PKG=""
CHROMIUM_NOTE=""
if ((NO_CHROMIUM)); then
    CHROMIUM_NOTE="skipped (--no-chromium)"
elif command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
    CHROMIUM_NOTE="already installed ($(command -v chromium || command -v chromium-browser))"
else
    if [[ "$OS_CODENAME" == "bookworm" ]]; then candidates=(chromium-browser chromium); else candidates=(chromium chromium-browser); fi
    for pkg in "${candidates[@]}"; do
        if pkg_available "$pkg"; then CHROMIUM_PKG="$pkg"; break; fi
    done
    CHROMIUM_NOTE="${CHROMIUM_PKG:-not available from apt - YouTube player disabled}"
fi

APT_WANTED=(
    python3-venv python3-dev python3-pip   # virtualenv + headers for any source build
    libportaudio2                          # sounddevice (microphone / speaker)
    espeak-ng                              # offline text-to-speech voice
    v4l-utils                              # v4l2-ctl: camera inspection
    alsa-utils                             # aplay / arecord / alsamixer
    pulseaudio-utils                       # pactl (talks to PipeWire's pulse server)
    i2c-tools                              # i2cdetect (servo board, later)
    wlr-randr                              # Wayland screen mode / rotation tool
    fonts-dejavu-core                      # readable fonts for the dashboard / kiosk
)
[[ -n "$CHROMIUM_PKG" ]] && APT_WANTED+=("$CHROMIUM_PKG")
((SYSTEM_OPENCV)) && APT_WANTED+=(python3-opencv opencv-data)

APT_INSTALL=()
APT_MISSING=()
if ((SKIP_APT == 0)); then
    for pkg in "${APT_WANTED[@]}"; do
        if pkg_installed "$pkg"; then
            continue
        elif pkg_available "$pkg"; then
            APT_INSTALL+=("$pkg")
        else
            APT_MISSING+=("$pkg")
        fi
    done
fi

if ((SYSTEM_OPENCV)); then
    EXTRAS="audio,pi"
else
    EXTRAS="vision,audio,pi"
fi
((WITH_DEV)) && EXTRAS="$EXTRAS,dev"

# ---------------------------------------------------------------------------
# 2. Plan
# ---------------------------------------------------------------------------
step "Plan"
if ((SKIP_APT)); then
    info "apt: skipped (--skip-apt)"
elif ((${#APT_INSTALL[@]})); then
    info "apt install: ${APT_INSTALL[*]}"
else
    info "apt: all packages already installed"
fi
((${#APT_MISSING[@]})) && warn "not available from apt (skipped): ${APT_MISSING[*]}"
info "chromium: $CHROMIUM_NOTE"
if [[ -x "$VENV_DIR/bin/python" ]]; then
    info "venv: keep existing $VENV_DIR"
else
    info "venv: create $VENV_DIR$( ((SYSTEM_OPENCV)) && echo ' (--system-site-packages for apt OpenCV)')"
fi
info "pip: install -e \".[$EXTRAS]\" (binary wheels only for numpy / pygame-ce / OpenCV)"
info "files: data/, data/logs/; config/local.yaml and .env only if missing (never overwritten)"
if ((NO_LIVE)); then
    info "live voice: skipped (--no-live)"
else
    info "live voice: pip install -e \".[live]\" (aiortc, prebuilt wheels ~40 MB; a failure only warns)"
fi
if ((NO_MODELS == 0)) && [[ -f "$REPO_DIR/scripts/download_models.py" ]]; then
    info "models: YuNet face detector via scripts/download_models.py (skipped if already verified)"
fi
if ((NO_WAKE)); then
    info "wake word: skipped (--no-wake-word)"
else
    info "wake word: pip install -e \".[local-stt]\" (Vosk, offline, ~3 MB)$( ((NO_MODELS)) || echo ' + its English model (40 MB)'); a failure only warns"
fi
((ENABLE_I2C)) && info "i2c: enable the I2C bus with raspi-config (servos remain disabled in config)"
if ((NO_AUDIO_DEFAULT)); then
    info "audio default: skipped (--no-audio-default)"
else
    info "audio default: USB speaker + USB microphone before HDMI, for $CURRENT_USER (WirePlumber rule in"
    info "               ~/.config/wireplumber, scripts/set_default_speaker.sh; a failure only warns)"
fi
((WITH_SERVICE)) && info "autostart: systemd user service + ~/.config/autostart/littlebot.desktop"
((HEADLESS_SERVICE)) && info "autostart: HEADLESS system service /etc/systemd/system/littlebot.service"
if ((DRY_RUN)); then
    info "(dry run: nothing will be changed)"
elif ((ASSUME_YES == 0)); then
    if [[ -t 0 ]]; then
        read -r -p "Continue? [y/N] " answer
        [[ "$answer" == [yY]* ]] || { echo "Aborted."; exit 1; }
    else
        die "Not interactive: re-run with --yes to confirm."
    fi
fi

if ((DRY_RUN == 0)) && ! command -v sudo >/dev/null 2>&1 && ((${#APT_INSTALL[@]} || ENABLE_I2C || HEADLESS_SERVICE)); then
    die "sudo is required for the system steps."
fi

# ---------------------------------------------------------------------------
# 3. System packages
# ---------------------------------------------------------------------------
if ((SKIP_APT == 0 && ${#APT_INSTALL[@]})); then
    step "Installing system packages"
    run sudo apt-get update
    run sudo apt-get install -y "${APT_INSTALL[@]}"
fi

# Group membership for camera / audio / touch input (normally already set on Pi OS).
step "Checking user groups"
GROUPS_WANTED=(video audio input render)
((ENABLE_I2C)) && GROUPS_WANTED+=(i2c)
GROUPS_ADDED=0
for g in "${GROUPS_WANTED[@]}"; do
    if ! getent group "$g" >/dev/null; then
        continue
    fi
    if id -nG "$CURRENT_USER" | tr ' ' '\n' | grep -qx "$g"; then
        info "$CURRENT_USER is in '$g'"
    else
        run sudo usermod -aG "$g" "$CURRENT_USER"
        GROUPS_ADDED=1
    fi
done
((GROUPS_ADDED)) && warn "Group membership changed: log out and back in (or reboot) before running LittleBot."

# ---------------------------------------------------------------------------
# 4. Python virtual environment + LittleBot
# ---------------------------------------------------------------------------
step "Python environment"
if [[ -x "$VENV_DIR/bin/python" ]]; then
    if ! "$VENV_DIR/bin/python" -c 'import sys' 2>/dev/null; then
        die "$VENV_DIR exists but its python does not run (copied from another machine or Python upgraded). Move it aside: mv .venv .venv.old - then re-run."
    fi
    VENV_PY_VER="$("$VENV_DIR/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    SYS_PY_VER="$("$PY_SYS" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [[ "$VENV_PY_VER" == "$SYS_PY_VER" ]] || warn ".venv uses Python $VENV_PY_VER but the system has $SYS_PY_VER."
    if ((SYSTEM_OPENCV)) && ! grep -qi 'include-system-site-packages *= *true' "$VENV_DIR/pyvenv.cfg"; then
        warn "--system-opencv needs a venv with --system-site-packages; the existing .venv has none. Move .venv aside and re-run to switch."
    fi
    info "keeping existing $VENV_DIR (Python $VENV_PY_VER)"
elif [[ -e "$VENV_DIR" ]]; then
    die "$VENV_DIR exists but is not a working venv. Move it aside (mv .venv .venv.old) and re-run."
else
    if ((SYSTEM_OPENCV)); then
        run "$PY_SYS" -m venv --system-site-packages "$VENV_DIR"
    else
        run "$PY_SYS" -m venv "$VENV_DIR"
    fi
fi

PIP=("$VENV_DIR/bin/python" -m pip)
run "${PIP[@]}" install --upgrade pip
# --only-binary: fail fast instead of compiling numpy/OpenCV/pygame for an hour on 2 GB RAM.
run "${PIP[@]}" install --prefer-binary --only-binary=numpy,pygame-ce,opencv-python-headless -e "${REPO_DIR}[${EXTRAS}]"
if ((SYSTEM_OPENCV)) && ((DRY_RUN == 0)); then
    "$VENV_DIR/bin/python" -c 'import cv2; print("    OpenCV (apt):", cv2.__version__)' \
        || warn "cv2 is not importable from the venv - is python3-opencv installed and the venv using system site-packages?"
fi

# Live voice (GPT-Live over WebRTC, docs/CHATGPT.md) is optional, so its own step: a failure
# only warns and LittleBot's AI page then says "Not installed on this robot yet". aiortc is pure
# Python; --only-binary makes pip fail fast instead of compiling PyAV / SRTP / cryptography.
if ((NO_LIVE == 0)); then
    step "Live voice (aiortc)"
    run "${PIP[@]}" install --prefer-binary --only-binary=av,pylibsrtp,cryptography,google-crc32c -e "${REPO_DIR}[live]" \
        || warn "Live voice could not be installed (no internet?). Later: .venv/bin/python -m pip install -e '.[live]'"
fi

# The OpenCV 5 pip wheel has no Haar cascade support, so the vision module uses YuNet,
# whose small ONNX model is downloaded by the vision module's own script (idempotent).
if ((NO_MODELS == 0)) && [[ -f "$REPO_DIR/scripts/download_models.py" ]]; then
    step "Face-detection and hand models"
    # hands: "how many fingers?" is counted on the robot (~0.1 s) instead of by ChatGPT (~2 s).
    run "$VENV_DIR/bin/python" "$REPO_DIR/scripts/download_models.py" yunet hands \
        || warn "Model download failed (no internet?). Later: .venv/bin/python scripts/download_models.py yunet hands"
fi

# Wake word ("Hey LittleBot" starts live voice, docs/CHATGPT.md): Vosk, an offline speech
# recogniser (one prebuilt aarch64 wheel for every Python 3; its small helper "srt" is pure
# Python) and its small English model, unpacked into models/vosk-model-small-en-us-0.15. Optional,
# so its own step: a failure only warns and the AI page then says what is missing.
if ((NO_WAKE == 0)); then
    step "Wake word (offline speech recogniser)"
    run "${PIP[@]}" install --prefer-binary --only-binary=vosk,cffi -e "${REPO_DIR}[local-stt]" \
        || warn "The wake word could not be installed (no internet?). Later: .venv/bin/python -m pip install -e '.[local-stt]'"
    if ((NO_MODELS == 0)) && [[ -f "$REPO_DIR/scripts/download_models.py" ]]; then
        run "$VENV_DIR/bin/python" "$REPO_DIR/scripts/download_models.py" vosk-en \
            || warn "Wake-word model download failed (no internet?). Later: .venv/bin/python scripts/download_models.py vosk-en"
    fi
fi

# ---------------------------------------------------------------------------
# 5. Data directory and per-machine config (never overwritten)
# ---------------------------------------------------------------------------
step "Data and configuration"
run mkdir -p "$REPO_DIR/data/logs"
if [[ -f "$REPO_DIR/config/local.yaml" ]]; then
    info "config/local.yaml exists - kept unchanged"
else
    run cp "$REPO_DIR/config/hardware.example.yaml" "$REPO_DIR/config/local.yaml"
fi
if [[ -f "$REPO_DIR/.env" ]]; then
    info ".env exists - kept unchanged"
elif [[ -f "$REPO_DIR/.env.example" ]]; then
    run cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
fi
if [[ -f "$REPO_DIR/.env" || $DRY_RUN -eq 1 ]]; then
    run chmod 600 "$REPO_DIR/.env"
fi

# ---------------------------------------------------------------------------
# 6. Optional: I2C (for the future servo board) - never on by default
# ---------------------------------------------------------------------------
if ((ENABLE_I2C)); then
    step "Enabling I2C"
    if [[ -e /dev/i2c-1 ]]; then
        info "I2C already enabled (/dev/i2c-1 exists)"
    elif command -v raspi-config >/dev/null 2>&1; then
        run sudo raspi-config nonint do_i2c 0
        info "I2C enabled - a reboot may be needed. Servos stay disabled (motion.physical_enabled: false)."
    else
        warn "raspi-config not found: add 'dtparam=i2c_arm=on' to /boot/firmware/config.txt yourself."
    fi
fi

# ---------------------------------------------------------------------------
# 7. Optional: autostart
# ---------------------------------------------------------------------------
if ((WITH_SERVICE)); then
    step "Installing the autostart (systemd user service + desktop entry)"
    if systemctl is-enabled littlebot.service >/dev/null 2>&1; then
        warn "The headless system service is enabled too. Disable it: sudo systemctl disable --now littlebot"
    fi
    USER_UNIT_DIR="$HOME/.config/systemd/user"
    run mkdir -p "$USER_UNIT_DIR" "$HOME/.config/autostart" "$HOME/.config/littlebot"
    for f in scripts/systemd/littlebot-launch.sh scripts/autostart/littlebot-session-start.sh; do
        run chmod +x "$REPO_DIR/$f"
    done
    render "$REPO_DIR/scripts/systemd/littlebot.service" | write_file "$USER_UNIT_DIR/littlebot.service"
    render "$REPO_DIR/scripts/autostart/littlebot.desktop" | write_file "$HOME/.config/autostart/littlebot.desktop"
    if [[ ! -f "$HOME/.config/littlebot/littlebot.env" ]]; then
        printf '%s\n' "# LittleBot service overrides (KEY=value). Examples:" "#SDL_VIDEODRIVER=x11" \
            "#LITTLEBOT_ARGS=--debug" "#LITTLEBOT__LOGGING__LEVEL=DEBUG" \
            | write_file "$HOME/.config/littlebot/littlebot.env"
    fi
    if ! { run systemctl --user daemon-reload && run systemctl --user enable littlebot.service; }; then
        warn "Could not reach your systemd user manager (no login session?). Log in on the desktop or via SSH"
        warn "as $CURRENT_USER and run: systemctl --user daemon-reload && systemctl --user enable littlebot"
    fi
    AUTOLOGIN="$(grep -hsE '^\s*autologin-user\s*=' /etc/lightdm/lightdm.conf /etc/lightdm/lightdm.conf.d/*.conf 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ' || true)"
    if [[ "$AUTOLOGIN" == "$CURRENT_USER" ]]; then
        info "Desktop autologin is enabled for $CURRENT_USER."
    else
        warn "Desktop autologin does not seem to be enabled for $CURRENT_USER. Enable it so LittleBot starts at boot:"
        warn "  sudo raspi-config  ->  System Options  ->  Boot / Auto Login  ->  Desktop Autologin"
    fi
    info "Start now: systemctl --user start littlebot   Logs: journalctl --user -u littlebot -f"
fi

if ((HEADLESS_SERVICE)); then
    step "Installing the headless system service"
    if systemctl --user is-enabled littlebot.service >/dev/null 2>&1; then
        warn "The user service is enabled too. Disable it: systemctl --user disable --now littlebot"
    fi
    render "$REPO_DIR/scripts/littlebot.service" | write_file /etc/systemd/system/littlebot.service sudo
    run sudo systemctl daemon-reload
    run sudo systemctl enable littlebot.service
    info "Start now: sudo systemctl start littlebot   Logs: journalctl -u littlebot -f"
fi

# ---------------------------------------------------------------------------
# 8. Default sound devices: USB speaker / microphone instead of the HDMI screen
# ---------------------------------------------------------------------------
# User-level only (a WirePlumber rule in ~/.config plus `wpctl set-default`), run as this user
# like the `systemctl --user` steps above. Needs PipeWire running for this user: on the desktop,
# or over SSH (the script finds /run/user/<uid> itself). A failure only warns.
if ((NO_AUDIO_DEFAULT == 0)); then
    step "Default sound devices (USB speaker and microphone, not HDMI)"
    run bash "$REPO_DIR/scripts/set_default_speaker.sh" \
        || warn "The USB speaker is not the default sound output yet (plugged in? desktop session running?). Later: scripts/set_default_speaker.sh"
fi

# ---------------------------------------------------------------------------
# 9. Next steps
# ---------------------------------------------------------------------------
step "Done$( ((DRY_RUN)) && echo ' (dry run - nothing was changed)')"
cat <<EOF
    Next steps (docs/PI_SETUP.md):
      1. .venv/bin/python scripts/diagnose.py            # everything green / explained?
      2. .venv/bin/python scripts/check_audio.py --help  # pick microphone + speaker
         .venv/bin/python scripts/check_camera.py --help # confirm the camera
      3. nano config/local.yaml                           # device names, dashboard options
      4. .venv/bin/python -m littlebot --mode pi          # first run (Esc / Ctrl+C to quit)
    Servos stay disabled (motion.physical_enabled: false) until docs/MOTION_SAFETY.md is done.
EOF
