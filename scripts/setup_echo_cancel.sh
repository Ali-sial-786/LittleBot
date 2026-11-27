#!/usr/bin/env bash
# LittleBot - echo cancelling for the USB microphone and speaker, with PipeWire's own echo canceller.
#
#   scripts/setup_echo_cancel.sh              # set it up (or bring it up to date); safe to run again
#   scripts/setup_echo_cancel.sh --dry-run    # print what it would do and the files it would write
#   scripts/setup_echo_cancel.sh --check      # is it working? (PASS / WARN / FAIL per item, changes nothing)
#   scripts/setup_echo_cancel.sh --remove     # undo everything it did
#
# UNTESTED ON A REAL RASPBERRY PI: written from the PipeWire / WirePlumber / ALSA documentation and
# tested only against a fake PipeWire (tests/test_echo_cancel_script.py). Run --check on the robot.
#
# Run it as the desktop user (no sudo). It only changes files in your home directory:
#   1. ~/.config/pipewire/pipewire.conf.d/60-littlebot-echo-cancel.conf loads PipeWire's
#      libpipewire-module-echo-cancel with the WebRTC canceller (aec/libspa-aec-webrtc) between the
#      USB microphone and the USB speaker. It makes two devices with stable names:
#        littlebot_ec_speaker  (Audio/Sink)   plays on the USB speaker; the canceller knows what it played
#        littlebot_ec_mic      (Audio/Source) the USB microphone with that sound taken out
#   2. A marked block in ~/.config/alsa/asoundrc (ALSA reads it as well as ~/.asoundrc; on the robot
#      something deleted ~/.asoundrc at boot, so the block lives here and is taken out of ~/.asoundrc,
#      where earlier versions put it): one ALSA PCM per device, through the pipewire ALSA plugin
#      (pipewire-alsa: `type pipewire` with `capture_node` / `playback_node`), with a hint so that
#      `arecord -L` / `aplay -L` list them. PortAudio (LittleBot uses it through sounddevice) lists
#      every pcm.<name> of the ALSA configuration under that name, so LittleBot finds them
#      (littlebot/audio/devices.py, audio.echo_cancel: auto) and uses them instead of the hw: devices.
#   3. `systemctl --user restart pipewire pipewire-pulse wireplumber` (only when something changed),
#      then `wpctl set-default` for both: YouTube in Chromium (and anything else using the default)
#      then plays through littlebot_ec_speaker too, so its sound is cancelled from what the wake word
#      and live voice hear, and every program shares the USB speaker through PipeWire (the exclusive
#      hw: devices used to give "Device unavailable" after a video).
#   4. Restarts LittleBot's user service if it is running (it picks devices when it starts).
#
# What the files rely on (PipeWire 0.3.65 / WirePlumber 0.4.13 on Bookworm, 1.4 / 0.5 on Trixie):
#  - module-echo-cancel args: library.name, aec.args, capture.props, source.props, sink.props,
#    playback.props (docs: https://docs.pipewire.org/page_module_echo_cancel.html). The WebRTC
#    canceller ships in libspa-0.2-modules (spa-0.2/aec/libspa-aec-webrtc.so), the module in
#    libpipewire-0.3-modules; Raspberry Pi OS installs both with PipeWire.
#  - target.object (PipeWire >= 0.3.64) pins the canceller's own streams to the USB devices, by
#    node.name, so they never loop back into littlebot_ec_speaker once it is the default.
#  - node.passive on those streams lets the USB devices suspend while nobody uses the new ones.
#  - The ALSA plugin's playback_node / capture_node take a node name (50-pipewire.conf, pipewire-alsa).
#  - priority.session 1600 / 2200 puts the new devices above the USB rule of
#    scripts/set_default_speaker.sh (1500 / 2100); wpctl set-default saves the choice as well.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=0
REMOVE=0
CHECK=0
SET_DEFAULT=1
RESTART_LITTLEBOT=1
SPEAKER_HINT=""
MIC_HINT=""
WAIT=10

EC_SINK="littlebot_ec_speaker"
EC_SOURCE="littlebot_ec_mic"
EC_CAPTURE="littlebot_ec_capture"
EC_PLAYBACK="littlebot_ec_playback"
PRIO_EC_SINK=1600
PRIO_EC_SOURCE=2200
CONF_NAME="60-littlebot-echo-cancel.conf"
BLOCK_BEGIN="# >>> LittleBot echo cancelling (scripts/setup_echo_cancel.sh) >>>"
BLOCK_END="# <<< LittleBot echo cancelling <<<"

usage() {
    cat <<'EOF'
Usage: scripts/setup_echo_cancel.sh [options]

Sets up PipeWire's echo canceller (WebRTC) for the USB microphone and speaker: an echo-cancelled
microphone "littlebot_ec_mic" and speaker "littlebot_ec_speaker", reachable from ALSA / PortAudio
(LittleBot) through ~/.config/alsa/asoundrc, and makes them the default sound devices. Run it as the desktop
user (no sudo). Untested on a real Raspberry Pi: run --check afterwards.

Options:
  --dry-run          print every action and the files it would write, change nothing
  --check            report whether echo cancelling works (changes nothing; exit 1 on a FAIL)
  --remove           undo it: remove the files, restart PipeWire, USB devices as defaults again
  --speaker NAME     the USB speaker whose name/description contains NAME (default: the USB speaker)
  --mic NAME         same for the microphone (default: the USB microphone)
  --no-default       don't make the echo-cancelled devices the default sound devices
  --no-restart-littlebot  don't restart LittleBot's user service afterwards
  --wait SECONDS     how long to wait for the new devices after restarting PipeWire (default 10)
  -h, --help         show this help

Check by hand:  wpctl status                 (Sinks / Sources: "LittleBot ... (echo cancelled)")
                pw-cli ls Node | grep littlebot_ec
                arecord -L | grep -A1 littlebot_ec    aplay -L | grep -A1 littlebot_ec
LittleBot's log then says "audio: echo cancelling on (PipeWire ...)".
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --remove | --undo) REMOVE=1 ;;
        --check) CHECK=1 ;;
        --no-default) SET_DEFAULT=0 ;;
        --no-restart-littlebot) RESTART_LITTLEBOT=0 ;;
        --speaker) shift; SPEAKER_HINT="${1:?--speaker needs a name}" ;;
        --speaker=*) SPEAKER_HINT="${1#*=}" ;;
        --mic) shift; MIC_HINT="${1:?--mic needs a name}" ;;
        --mic=*) MIC_HINT="${1#*=}" ;;
        --wait) shift; WAIT="${1:?--wait needs seconds}" ;;
        --wait=*) WAIT="${1#*=}" ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ "$WAIT" =~ ^[0-9]+$ ]] || { echo "--wait needs a whole number of seconds" >&2; exit 2; }
if ((REMOVE + CHECK > 1)); then
    echo "Choose one of --remove and --check." >&2
    exit 2
fi

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
        die "Do not use sudo: sound settings belong to the desktop user. Run it as $SUDO_USER: scripts/setup_echo_cancel.sh"
    fi
    warn "Running as root: this changes root's own sound settings, not the desktop user's."
fi
# Over SSH the runtime directory (PipeWire / systemd --user sockets) may not be set.
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export XDG_RUNTIME_DIR
fi

CONF_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
PW_CONF="$CONF_HOME/pipewire/pipewire.conf.d/$CONF_NAME"
ASOUNDRC="$CONF_HOME/alsa/asoundrc"  # LittleBot's block (ALSA's alsa.conf loads it after ~/.asoundrc)
OLD_ASOUNDRC="$HOME/.asoundrc"  # where earlier versions put it

# ---------------------------------------------------------------------------
# Reading the PipeWire graph: `pw-dump` JSON (stdin) -> KEY<TAB>VALUE lines.
# argv: speaker hint, mic hint, the echo-cancelled sink and source names.
# ---------------------------------------------------------------------------
PICKER=$(cat <<'PY'
import json
import sys


def clean(value):
    return " ".join(str(value).split())


def card_of(props, name):
    if props.get("device.id") is not None:
        return str(props["device.id"])
    rest = name.split(".", 1)[1] if "." in name else name
    return rest.rsplit(".", 1)[0]


def main():
    spk_hint, mic_hint, ec_sink, ec_source = sys.argv[1].lower(), sys.argv[2].lower(), sys.argv[3], sys.argv[4]
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("ERROR\tpw-dump did not print JSON")
        return
    nodes = []
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        props = (obj.get("info") or {}).get("props") or obj.get("props") or {}
        if obj.get("type") == "PipeWire:Interface:Node":
            name = str(props.get("node.name", ""))
            text = " ".join(str(props.get(k, "")) for k in (
                "node.name", "node.description", "node.nick", "alsa.card_name", "api.alsa.card.name")).lower()
            nodes.append({"id": obj.get("id"), "name": name, "cls": str(props.get("media.class", "")),
                          "desc": clean(props.get("node.description") or props.get("node.nick") or name),
                          "card": card_of(props, name), "text": text})

    ours = [n for n in nodes if n["name"].startswith("littlebot_ec")]
    sinks = [n for n in nodes if n["cls"] == "Audio/Sink" and n not in ours]
    sources = [n for n in nodes if n["cls"] == "Audio/Source" and n not in ours]
    usb_sinks = [n for n in sinks if n["name"].startswith("alsa_output.usb-")]
    usb_sources = [n for n in sources if n["name"].startswith("alsa_input.usb-")]
    spk_cands = [n for n in sinks if spk_hint in n["text"]] if spk_hint else usb_sinks
    mic_cands = [n for n in sources if mic_hint in n["text"]] if mic_hint else usb_sources

    def pick(cands, avoid_card):
        # A USB speaker often has a (useless) microphone and a USB microphone a headphone output:
        # prefer devices on different cards for the speaker and the microphone.
        pool = [c for c in cands if avoid_card is None or c["card"] != avoid_card] or cands
        return pool[0] if pool else None

    if len(spk_cands) <= 1 or len(mic_cands) > 1:
        spk = pick(spk_cands, None)
        mic = pick(mic_cands, spk["card"] if spk else None)
    else:
        mic = pick(mic_cands, None)
        spk = pick(spk_cands, mic["card"] if mic else None)
    ec_sink_node = next((n for n in ours if n["name"] == ec_sink and n["cls"] == "Audio/Sink"), None)
    ec_source_node = next((n for n in ours if n["name"] == ec_source and n["cls"] == "Audio/Source"), None)
    out = {
        "SPEAKER_NAME": spk["name"] if spk else "", "SPEAKER_DESC": spk["desc"] if spk else "",
        "MIC_NAME": mic["name"] if mic else "", "MIC_DESC": mic["desc"] if mic else "",
        "EC_SINK_ID": str(ec_sink_node["id"]) if ec_sink_node else "",
        "EC_SOURCE_ID": str(ec_source_node["id"]) if ec_source_node else "",
        "SINKS": "; ".join("%s [%s]" % (n["desc"], n["name"]) for n in sinks),
        "SOURCES": "; ".join("%s [%s]" % (n["desc"], n["name"]) for n in sources),
    }
    for key, value in out.items():
        print("%s\t%s" % (key, clean(value)))


main()
PY
)

discover() {
    local out key val
    P_SPEAKER_NAME="" P_SPEAKER_DESC="" P_MIC_NAME="" P_MIC_DESC="" P_EC_SINK_ID="" P_EC_SOURCE_ID=""
    P_SINKS="" P_SOURCES="" P_ERROR=""
    out="$(timeout 10 pw-dump 2>/dev/null | python3 -c "$PICKER" "$SPEAKER_HINT" "$MIC_HINT" "$EC_SINK" "$EC_SOURCE")" \
        || return 1
    while IFS=$'\t' read -r key val; do
        case "$key" in
            SPEAKER_NAME | SPEAKER_DESC | MIC_NAME | MIC_DESC | EC_SINK_ID | EC_SOURCE_ID | SINKS | SOURCES | ERROR)
                printf -v "P_$key" '%s' "$val" ;;
        esac
    done <<<"$out"
    [[ -z "$P_ERROR" ]]
}

# The node.name of the current default (@DEFAULT_AUDIO_SINK@ / @DEFAULT_AUDIO_SOURCE@), or nothing.
default_name() {
    local out
    out="$(timeout 10 wpctl inspect "$1" 2>/dev/null)" || return 0
    sed -n 's/^[[:space:]*]*node\.name = "\(.*\)"$/\1/p' <<<"$out" | head -n 1
}

# Where the canceller's sound really goes (pw-link -l), not just what the drop-in asks for: WirePlumber links it
# to another output when the speaker isn't there yet as PipeWire starts (on the robot: the ReSpeaker Lite's own
# headphone socket, so nothing was heard while every other check passed). littlebot/audio/ec_route.py.
check_route() {
    local py out rc=0
    py="${LITTLEBOT_EC_PYTHON:-$REPO_DIR/.venv/bin/python}"
    [[ -x "$py" ]] && have pw-link || return 0
    out="$("$py" -c "import sys; sys.path.insert(0, sys.argv[1]); from littlebot.audio import ec_route; \
raise SystemExit(ec_route.main(sys.argv[2:]))" "$REPO_DIR" 2>/dev/null)" || rc=$?
    out="${out%%$'\n'*}"
    [[ -z "$out" ]] && return 0
    case "$rc" in
        0) pass "${out%.}" ;;
        2) fail "${out%.}: nobody hears it. Move it: .venv/bin/python -m littlebot.audio.ec_route --fix (LittleBot also does when it starts and at every call)" ;;
        *) cwarn "${out%.}" ;;
    esac
}

# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------
# A target.object line for a node, or a comment saying the default device is followed.
target_line() {
    if [[ -n "$1" ]]; then
        printf '                target.object = "%s"\n' "$1"
    else
        printf '                # no USB %s found when this was written: the default one is used\n' "$2"
    fi
}

pipewire_conf() {
    cat <<EOF
# LittleBot: echo cancelling for the USB microphone and speaker (PipeWire's WebRTC echo canceller).
# Written by scripts/setup_echo_cancel.sh - remove it with: scripts/setup_echo_cancel.sh --remove
#
#   $EC_SINK  (Audio/Sink)   plays on the USB speaker; the canceller knows what was played
#   $EC_SOURCE      (Audio/Source) the USB microphone, with that sound taken out
# Everything played through $EC_SINK (LittleBot, YouTube in Chromium once it is the
# default) is removed from what $EC_SOURCE hears. Mono (the microphone is mono).
# Gain control is off: LittleBot measures levels itself (barge-in, wake word).
context.modules = [
    {   name = libpipewire-module-echo-cancel
        args = {
            library.name = aec/libspa-aec-webrtc
            audio.channels = 1
            audio.position = [ MONO ]
            aec.args = {
                webrtc.extended_filter = true
                webrtc.delay_agnostic = true
                webrtc.high_pass_filter = true
                webrtc.noise_suppression = true
                webrtc.gain_control = false
            }
            capture.props = {
                node.name = "$EC_CAPTURE"
                node.description = "LittleBot echo canceller (from the USB microphone)"
$(target_line "$P_MIC_NAME" microphone)
                node.passive = true
            }
            source.props = {
                node.name = "$EC_SOURCE"
                node.description = "LittleBot microphone (echo cancelled)"
                priority.session = $PRIO_EC_SOURCE
            }
            sink.props = {
                node.name = "$EC_SINK"
                node.description = "LittleBot speaker (echo cancelled)"
                priority.session = $PRIO_EC_SINK
            }
            playback.props = {
                node.name = "$EC_PLAYBACK"
                node.description = "LittleBot echo canceller (to the USB speaker)"
$(target_line "$P_SPEAKER_NAME" speaker)
                node.passive = true
            }
        }
    }
]
EOF
}

asound_block() {
    cat <<EOF
$BLOCK_BEGIN
# PipeWire's echo-cancelled microphone and speaker as ALSA devices (pipewire-alsa), for LittleBot
# (PortAudio lists them by these names) and arecord / aplay. Remove with: scripts/setup_echo_cancel.sh --remove
pcm.$EC_SOURCE {
    type pipewire
    capture_node "$EC_SOURCE"
    hint {
        show on
        description "LittleBot microphone (echo cancelled, PipeWire)"
    }
}
pcm.$EC_SINK {
    type pipewire
    playback_node "$EC_SINK"
    hint {
        show on
        description "LittleBot speaker (echo cancelled, PipeWire)"
    }
}
$BLOCK_END
EOF
}

# An ALSA file ($1, default $ASOUNDRC) without LittleBot's block (the rest is kept as it is).
asound_without_block() {
    local f="${1:-$ASOUNDRC}"
    [[ -f "$f" ]] || return 0
    awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$f"
}

asound_has_block() { local f="${1:-$ASOUNDRC}"; [[ -f "$f" ]] && grep -qxF "$BLOCK_BEGIN" "$f"; }

# Take LittleBot's block out of $1 (the file goes when nothing else is in it). Returns 1 if it had none.
strip_block() {
    local f="$1" rest
    asound_has_block "$f" || return 1
    rest="$(asound_without_block "$f")"
    if [[ -z "${rest//[[:space:]]/}" ]]; then
        run rm -f "$f"
    elif ((DRY_RUN)); then
        info "[dry-run] remove LittleBot's block from $f (the rest stays)"
    else
        printf '%s\n' "$rest" >"$f"
        info "removed LittleBot's block from $f (the rest stays)"
    fi
}

# Write stdin to $1 unless it already has that content. Returns 0 if (it would be) changed.
write_if_changed() {
    local dest="$1" content
    content="$(cat)"
    if [[ -f "$dest" && "$(cat "$dest")" == "$content" ]]; then
        info "$dest is up to date"
        return 1
    fi
    if ((DRY_RUN)); then
        printf '    [dry-run] write %s:\n' "$dest"
        printf '%s\n' "$content" | sed 's/^/      | /'
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    printf '%s\n' "$content" >"$dest"
    info "wrote $dest"
}

restart_pipewire() {
    run systemctl --user restart pipewire pipewire-pulse wireplumber \
        || { warn "Could not restart PipeWire. Log out and in again (or reboot) to load the change."; return 1; }
}

# Waits until the echo-cancelled devices exist (up to WAIT s); returns 1 if they never appear.
wait_for_ec() {
    local start=$SECONDS
    while :; do
        if discover && [[ -n "$P_EC_SINK_ID" && -n "$P_EC_SOURCE_ID" ]]; then
            return 0
        fi
        ((SECONDS - start >= WAIT)) && return 1
        sleep 0.5
    done
}

restart_littlebot() {
    ((RESTART_LITTLEBOT)) || return 0
    if have systemctl && systemctl --user is-active --quiet littlebot 2>/dev/null; then
        step "Restarting LittleBot (it chooses its sound devices when it starts)"
        run systemctl --user restart littlebot || warn "Could not restart LittleBot: systemctl --user restart littlebot"
    else
        info "LittleBot's service is not running: start LittleBot again to use the new devices."
    fi
}

# Where the PipeWire / ALSA plugins are looked for (LITTLEBOT_EC_LIB_DIRS: for the tests).
read -r -a LIB_DIRS <<<"${LITTLEBOT_EC_LIB_DIRS:-/usr/lib /usr/lib64 /usr/local/lib}"
lib_present() {  # $1: a path under <lib dir>/<multiarch triplet>/ or <lib dir>/
    local d f
    for d in "${LIB_DIRS[@]}"; do
        for f in "$d"/*/"$1" "$d"/"$1"; do
            [[ -e "$f" ]] && return 0
        done
    done
    return 1
}

# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
FAILS=0
WARNS=0
pass() { printf '  %sPASS%s  %s\n' "$C_GRN" "$C_OFF" "$*"; }
cwarn() { printf '  %sWARN%s  %s\n' "$C_YEL" "$C_OFF" "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  %sFAIL%s  %s\n' "$C_RED" "$C_OFF" "$*"; FAILS=$((FAILS + 1)); }

do_check() {
    step "Echo cancelling: checking (nothing is changed)"
    local pw_ok=1 ver def py
    for tool in wpctl pw-dump python3 timeout; do
        have "$tool" || { fail "$tool not found (packages wireplumber, pipewire-bin, python3)"; pw_ok=0; }
    done
    if ((pw_ok)) && timeout 10 wpctl status >/dev/null 2>&1; then
        ver="$(pipewire --version 2>/dev/null | sed -n 's/.*libpipewire \([0-9.]*\).*/\1/p' | head -n 1)" || ver=""
        pass "PipeWire is running for $(id -un)${ver:+ (version $ver)}"
    elif ((pw_ok)); then
        fail "Cannot reach PipeWire for $(id -un): run this in the desktop session (or SSH as that user); systemctl --user status pipewire wireplumber"
        pw_ok=0
    fi
    if lib_present "pipewire-0.3/libpipewire-module-echo-cancel.so"; then
        pass "PipeWire's echo-cancel module is installed"
    else
        fail "libpipewire-module-echo-cancel.so not found: sudo apt install libpipewire-0.3-modules"
    fi
    if lib_present "spa-0.2/aec/libspa-aec-webrtc.so"; then
        pass "the WebRTC echo canceller is installed"
    else
        fail "spa-0.2/aec/libspa-aec-webrtc.so not found: sudo apt install libspa-0.2-modules"
    fi
    if lib_present "alsa-lib/libasound_module_pcm_pipewire.so"; then
        pass "the PipeWire ALSA plugin is installed (pipewire-alsa)"
    else
        fail "the PipeWire ALSA plugin is missing: sudo apt install pipewire-alsa"
    fi
    if [[ -f "$PW_CONF" ]]; then pass "config: $PW_CONF"; else fail "not set up: $PW_CONF is missing (run scripts/setup_echo_cancel.sh)"; fi
    if asound_has_block; then pass "ALSA devices: LittleBot's block in $ASOUNDRC"; else fail "no LittleBot block in $ASOUNDRC (run scripts/setup_echo_cancel.sh)"; fi
    if asound_has_block "$OLD_ASOUNDRC"; then cwarn "LittleBot's block is in $OLD_ASOUNDRC too (an earlier version put it there; run scripts/setup_echo_cancel.sh to move it)"; fi
    if ((pw_ok)) && discover; then
        if [[ -n "$P_EC_SINK_ID" && -n "$P_EC_SOURCE_ID" ]]; then
            pass "PipeWire has $EC_SINK (id $P_EC_SINK_ID) and $EC_SOURCE (id $P_EC_SOURCE_ID)"
        else
            fail "PipeWire has no $EC_SINK / $EC_SOURCE: journalctl --user -u pipewire -b | grep -i echo"
        fi
        if [[ -n "$P_SPEAKER_NAME" ]]; then pass "USB speaker: $P_SPEAKER_DESC ($P_SPEAKER_NAME)"
        else cwarn "no USB speaker found (sinks: ${P_SINKS:-none})"; fi
        if [[ -n "$P_MIC_NAME" ]]; then pass "USB microphone: $P_MIC_DESC ($P_MIC_NAME)"
        else cwarn "no USB microphone found (sources: ${P_SOURCES:-none})"; fi
        if [[ -f "$PW_CONF" ]]; then
            for want in "$P_SPEAKER_NAME" "$P_MIC_NAME"; do
                [[ -z "$want" ]] && continue
                grep -qF "\"$want\"" "$PW_CONF" || cwarn "$PW_CONF doesn't name $want (devices changed?): run scripts/setup_echo_cancel.sh again"
            done
        fi
        def="$(default_name @DEFAULT_AUDIO_SINK@)"
        if [[ "$def" == "$EC_SINK" ]]; then pass "default speaker: $EC_SINK (YouTube's sound is cancelled too)"
        else cwarn "default speaker is ${def:-unknown}, not $EC_SINK: a video's sound is not cancelled (wpctl set-default $P_EC_SINK_ID)"; fi
        def="$(default_name @DEFAULT_AUDIO_SOURCE@)"
        if [[ "$def" == "$EC_SOURCE" ]]; then pass "default microphone: $EC_SOURCE"
        else cwarn "default microphone is ${def:-unknown}, not $EC_SOURCE"; fi
        check_route
    fi
    if have arecord && have aplay; then
        if arecord -L 2>/dev/null | grep -qx "$EC_SOURCE"; then pass "arecord -L lists $EC_SOURCE"; else fail "arecord -L doesn't list $EC_SOURCE ($ASOUNDRC not read?)"; fi
        if aplay -L 2>/dev/null | grep -qx "$EC_SINK"; then pass "aplay -L lists $EC_SINK"; else fail "aplay -L doesn't list $EC_SINK"; fi
        # Opens the microphone for 1 s; the sound goes to /dev/null (nothing is kept).
        if timeout 5 arecord -q -D "$EC_SOURCE" -d 1 -f S16_LE -r 48000 -c 1 /dev/null 2>/dev/null; then
            pass "$EC_SOURCE opens through ALSA (1 s read, discarded)"
        else
            fail "$EC_SOURCE does not open through ALSA: arecord -D $EC_SOURCE -d 1 -f S16_LE -r 48000 /dev/null"
        fi
    else
        cwarn "arecord / aplay not found (sudo apt install alsa-utils): ALSA side not checked"
    fi
    py="${LITTLEBOT_EC_PYTHON:-$REPO_DIR/.venv/bin/python}"
    if [[ -x "$py" ]]; then
        # What LittleBot itself will choose (PortAudio's device list; nothing is opened).
        if "$py" -c "import sys; sys.path.insert(0, sys.argv[1]); from littlebot.audio import devices as d; \
p = d.echo_cancel_devices(); print(p[0]['name'] + ' / ' + p[1]['name'] if p else ''); \
raise SystemExit(0 if p else 1)" "$REPO_DIR" >/dev/null 2>&1; then
            pass "PortAudio (LittleBot) sees $EC_SOURCE and $EC_SINK"
        else
            fail "PortAudio (LittleBot) doesn't list both $EC_SOURCE and $EC_SINK: .venv/bin/python scripts/check_audio.py --list"
        fi
    else
        cwarn "no $py: PortAudio's device list not checked"
    fi
    echo
    if ((FAILS)); then
        printf '%s%d problem(s).%s See docs/PI_SETUP.md ("Echo cancelling") and docs/TROUBLESHOOTING.md.\n' "$C_RED" "$FAILS" "$C_OFF"
        return 1
    fi
    printf '%sEcho cancelling looks set up%s%s. LittleBot logs "audio: echo cancelling on (PipeWire ...)" when it uses it.\n' \
        "$C_GRN" "$C_OFF" "$( ((WARNS)) && printf ' (%d warning(s))' "$WARNS")"
    return 0
}

if ((CHECK)); then
    do_check
    exit $?
fi

# ---------------------------------------------------------------------------
# Checks before changing anything
# ---------------------------------------------------------------------------
step "Checking PipeWire"
((DRY_RUN)) && info "(dry run: nothing will be changed)"
PW_OK=1
PW_MSG=""
MISSING=()
for tool in wpctl pw-dump python3 timeout systemctl; do
    have "$tool" || MISSING+=("$tool")
done
if ((${#MISSING[@]})); then
    PW_OK=0
    PW_MSG="Not found: ${MISSING[*]}. PipeWire's tools come with Raspberry Pi OS (packages wireplumber, pipewire-bin, python3)."
elif ! timeout 10 wpctl status >/dev/null 2>&1; then
    PW_OK=0
    PW_MSG="Cannot reach PipeWire for user $(id -un) (XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset}). Run this in the robot's desktop session or via SSH as the desktop user; check: systemctl --user status pipewire wireplumber"
else
    info "PipeWire is running for $(id -un)"
fi
if ((PW_OK == 0)); then
    if ((DRY_RUN || REMOVE)); then warn "$PW_MSG"; else die "$PW_MSG"; fi
fi

# ---------------------------------------------------------------------------
# --remove
# ---------------------------------------------------------------------------
if ((REMOVE)); then
    step "Removing LittleBot's echo cancelling"
    CHANGED=0
    if [[ -e "$PW_CONF" ]]; then
        run rm -f "$PW_CONF"
        CHANGED=1
    else
        info "no $PW_CONF"
    fi
    for f in "$ASOUNDRC" "$OLD_ASOUNDRC"; do
        if strip_block "$f"; then CHANGED=1; else info "no LittleBot block in $f"; fi
    done
    if ((CHANGED)) && ((PW_OK)); then
        step "Restarting PipeWire"
        restart_pipewire || true
        step "The USB speaker and microphone are the default devices again"
        if ((DRY_RUN)); then
            run bash "$SCRIPT_DIR/set_default_speaker.sh"
        else
            bash "$SCRIPT_DIR/set_default_speaker.sh" \
                || warn "scripts/set_default_speaker.sh failed: pick the speaker with wpctl status, then wpctl set-default <ID>"
            restart_littlebot
        fi
    elif ((CHANGED)); then
        info "PipeWire is not reachable: log out and in again (or reboot) to finish."
    fi
    step "Done$( ((DRY_RUN)) && echo ' (dry run - nothing was changed)')"
    info "LittleBot uses the USB devices directly again (half-duplex live voice, no barge-in)."
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Which USB devices
# ---------------------------------------------------------------------------
step "USB microphone and speaker"
P_SPEAKER_NAME="" P_SPEAKER_DESC="" P_MIC_NAME="" P_MIC_DESC="" P_EC_SINK_ID="" P_EC_SOURCE_ID=""
P_SINKS="" P_SOURCES=""
if ((PW_OK)); then
    discover || die "Cannot read the PipeWire graph (pw-dump)."
    if [[ -n "$P_SPEAKER_NAME" ]]; then info "speaker:    $P_SPEAKER_DESC ($P_SPEAKER_NAME)"
    else warn "No USB speaker found${SPEAKER_HINT:+ matching \"$SPEAKER_HINT\"} (outputs: ${P_SINKS:-none}): the canceller plays on the default output. Plug it in and run this again."; fi
    if [[ -n "$P_MIC_NAME" ]]; then info "microphone: $P_MIC_DESC ($P_MIC_NAME)"
    else warn "No USB microphone found${MIC_HINT:+ matching \"$MIC_HINT\"} (inputs: ${P_SOURCES:-none}): the canceller records from the default input. Plug it in and run this again."; fi
else
    info "(PipeWire not reachable: the canceller would follow the default devices)"
fi
if ((PW_OK)) && ! lib_present "alsa-lib/libasound_module_pcm_pipewire.so"; then
    warn "The PipeWire ALSA plugin (pipewire-alsa) seems missing; LittleBot can't reach the new devices without it: sudo apt install pipewire-alsa"
fi
if ((PW_OK)) && ! lib_present "spa-0.2/aec/libspa-aec-webrtc.so"; then
    warn "The WebRTC echo canceller (spa-0.2/aec/libspa-aec-webrtc.so) seems missing: sudo apt install libspa-0.2-modules"
fi

# ---------------------------------------------------------------------------
# 2. The files
# ---------------------------------------------------------------------------
step "PipeWire echo canceller"
CHANGED=0
if pipewire_conf | write_if_changed "$PW_CONF"; then CHANGED=1; fi

step "ALSA devices for LittleBot ($ASOUNDRC)"
NEW_ASOUND="$(asound_without_block)"
if [[ -n "${NEW_ASOUND//[[:space:]]/}" ]]; then
    NEW_ASOUND="$NEW_ASOUND"$'\n\n'"$(asound_block)"
else
    NEW_ASOUND="$(asound_block)"
fi
if printf '%s\n' "$NEW_ASOUND" | write_if_changed "$ASOUNDRC"; then CHANGED=1; fi
if strip_block "$OLD_ASOUNDRC"; then CHANGED=1; fi  # moved from where earlier versions put it

if ((PW_OK == 0)); then  # only reachable with --dry-run
    run systemctl --user restart pipewire pipewire-pulse wireplumber
    ((SET_DEFAULT)) && run wpctl set-default "<id of $EC_SINK>" && run wpctl set-default "<id of $EC_SOURCE>"
    step "Done (dry run - nothing was changed)"
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Load it
# ---------------------------------------------------------------------------
discover || true
if ((CHANGED)) || [[ -z "$P_EC_SINK_ID" || -z "$P_EC_SOURCE_ID" ]]; then
    step "Restarting PipeWire (sound stops for a moment)"
    restart_pipewire || true
    if ((DRY_RUN == 0)); then
        info "waiting up to ${WAIT}s for $EC_SINK and $EC_SOURCE"
        wait_for_ec || die "PipeWire didn't make $EC_SINK / $EC_SOURCE. See: journalctl --user -u pipewire -b | grep -i echo ; then scripts/setup_echo_cancel.sh --check"
        info "PipeWire has $EC_SINK (id $P_EC_SINK_ID) and $EC_SOURCE (id $P_EC_SOURCE_ID)"
    fi
else
    info "already loaded: $EC_SINK (id $P_EC_SINK_ID), $EC_SOURCE (id $P_EC_SOURCE_ID) - no restart needed"
fi

# ---------------------------------------------------------------------------
# 4. Default devices
# ---------------------------------------------------------------------------
if ((SET_DEFAULT)); then
    step "Default speaker and microphone: the echo-cancelled ones"
    run wpctl set-default "${P_EC_SINK_ID:-<id of $EC_SINK>}" || warn "wpctl set-default ${P_EC_SINK_ID} failed"
    run wpctl set-default "${P_EC_SOURCE_ID:-<id of $EC_SOURCE>}" || warn "wpctl set-default ${P_EC_SOURCE_ID} failed"
else
    info "default devices unchanged (--no-default): a video's sound is not cancelled"
fi

if ((DRY_RUN)); then
    step "Done (dry run - nothing was changed)"
    exit 0
fi
restart_littlebot

step "Done"
info "Echo cancelling is set up. LittleBot uses it on its next start (its log: \"audio: echo cancelling on (PipeWire ...)\")"
info "and a grown-up can then talk over the live voice (chatgpt.live_barge_in: auto)."
info "Check it any time:   scripts/setup_echo_cancel.sh --check"
info "By hand:             wpctl status ; pw-cli ls Node | grep littlebot_ec ; arecord -L | grep littlebot_ec"
info "Undo:                scripts/setup_echo_cancel.sh --remove"
echo
do_check || true
exit 0
