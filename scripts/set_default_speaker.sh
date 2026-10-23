#!/usr/bin/env bash
# LittleBot - make the USB speaker (and the USB microphone) the default sound devices.
#
#   scripts/set_default_speaker.sh                  # USB before HDMI, now and after every reboot
#   scripts/set_default_speaker.sh --dry-run        # show what would happen, change nothing
#   scripts/set_default_speaker.sh --speaker UACDemo --mic PnP   # choose devices by name
#   scripts/set_default_speaker.sh --undo           # remove LittleBot's WirePlumber rule again
#
# Run it as the desktop user (no sudo). It only changes files in your home directory:
#   1. A WirePlumber rule (drop-in) that gives USB sound devices a high priority and HDMI
#      outputs a low one, so the USB speaker wins whenever it is plugged in:
#        WirePlumber 0.5 (Trixie):   ~/.config/wireplumber/wireplumber.conf.d/51-littlebot-usb-audio.conf
#        WirePlumber 0.4 (Bookworm): ~/.config/wireplumber/main.lua.d/51-littlebot-usb-audio.lua
#      Unplugged speaker: HDMI is still there as a fallback, so sound never breaks.
#   2. `systemctl --user restart wireplumber` (only when the rule is new or not active yet).
#   3. `wpctl set-default <id>` for the USB speaker and microphone. WirePlumber remembers
#      that choice (~/.local/state/wireplumber/default-nodes) and it outranks any priority,
#      so this also replaces an earlier HDMI choice made in the volume menu.
#   4. Shows the result (`wpctl inspect @DEFAULT_AUDIO_SINK@`) and warns if it is still HDMI.
# Everything that uses the system default follows: YouTube in Chromium, aplay/speaker-test
# without -D, and LittleBot when audio.speaker.device is not set.
#
# How WirePlumber chooses the default (verified in the WirePlumber docs and sources, 2026-10):
#  - Debian 13 Trixie / Raspberry Pi OS Trixie ship WirePlumber 0.5.8 (PipeWire 1.4.2);
#    Debian 12 Bookworm ships 0.4.13 (packages.debian.org, archive.raspberrypi.com).
#  - 0.5 reads SPA-JSON fragments from $XDG_CONFIG_HOME/wireplumber/wireplumber.conf.d/*.conf
#    after the system ones; arrays such as monitor.alsa.rules are concatenated, so these rules
#    run last ("Locations of WirePlumber's files", "The configuration file":
#    https://pipewire.pages.freedesktop.org/wireplumber/daemon/locations.html, .../conf_file.html).
#  - Rules are PipeWire match rules: properties in one `matches` object are ANDed, objects are
#    ORed, and a value starting with "~" is a POSIX extended regular expression, unanchored
#    (pipewire.conf(5) "MATCH RULES"; regexec() in pipewire src/pipewire/conf.c, used by
#    wp_json_utils_match_rules). Hence "^" and "[.]" (a literal dot, no backslash escaping).
#  - monitors/alsa.lua (0.5.8) computes a node's priority.session first and applies
#    monitor.alsa.rules afterwards, so update-props wins; later matching rules override earlier.
#  - Default node = the user's configured choice (`wpctl set-default`, saved in the
#    default-nodes state file together with earlier choices) if that node exists, else the
#    node with the highest priority.session (scripts/default-nodes/*.lua in 0.5.8; "Node
#    priorities": .../daemon/configuration/priorities.html). priority.driver does not decide
#    the default (it is only a fallback when priority.session is unset, 0.5.2+).
#  - Defaults: sources ~1600-2000, sinks ~600-1000; do not raise a sink above 1500, or its
#    monitor can become the default source (ALSA configuration docs, 0.4.13 and 0.5.8).
#    So: USB sinks 1500, USB sources 2100, HDMI sinks 100.
#  - 0.4 reads ~/.config/wireplumber/main.lua.d/*.lua merged with the system files by file
#    name; 51-* runs after 50-alsa-config.lua defines alsa_monitor.rules and before
#    90-enable-all.lua loads the monitor. "matches" there is a glob (GPatternSpec: * and ?,
#    whole string) - lib/wp/object-interest.c and docs/rst/configuration/config_lua.rst, 0.4.13.
#  - wpctl set-default takes a numeric node id; `wpctl clear-default 0` forgets the saved
#    Audio/Sink choices (wpctl(1); src/tools/wpctl.c in 0.4.13 and 0.5.8).
set -euo pipefail

DRY_RUN=0
UNDO=0
SPEAKER_HINT=""
MIC_HINT=""
WAIT=10

# Priorities written into the drop-in (see above for why these numbers).
PRIO_USB_SINK=1500
PRIO_USB_SOURCE=2100
PRIO_HDMI_SINK=100

DROPIN_NAME="51-littlebot-usb-audio"

usage() {
    cat <<'EOF'
Usage: scripts/set_default_speaker.sh [options]

Makes the USB speaker and the USB microphone the default PipeWire sound devices, now and
after reboots, instead of the HDMI screen. Run it as the desktop user (no sudo).

Options:
  --dry-run          print every action, change nothing
  --speaker NAME     use the output whose name/description contains NAME (case-insensitive),
                     e.g. --speaker UACDemo (default: the USB speaker)
  --mic NAME         same for the microphone, e.g. --mic PnP (default: the USB microphone)
  --wait SECONDS     how long to wait for the devices after restarting WirePlumber (default 10)
  --undo             remove LittleBot's WirePlumber rule and restart WirePlumber
  -h, --help         show this help

One-off manual fix: `wpctl status`, then `wpctl set-default <ID>` with the number shown
in front of the USB speaker under Audio -> Sinks.
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --undo) UNDO=1 ;;
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

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Picks the nodes from `pw-dump` JSON (stdin). Prints KEY<TAB>VALUE lines.
# argv: speaker hint, mic hint, USB sink / USB source / HDMI sink priorities.
# ---------------------------------------------------------------------------
PICKER=$(cat <<'PY'
import json
import sys


def clean(value):
    return " ".join(str(value).split())


def prio(props):
    for key in ("priority.session", "priority.driver"):
        try:
            return int(str(props[key]))
        except (KeyError, ValueError):
            continue
    return 0


def card_of(props, name):
    if props.get("device.id") is not None:
        return str(props["device.id"])
    rest = name.split(".", 1)[1] if "." in name else name
    return rest.rsplit(".", 1)[0]  # alsa_output.<card>.<profile>; card names may contain dots


def main():
    spk_hint, mic_hint = sys.argv[1].lower(), sys.argv[2].lower()
    p_usb_sink, p_usb_src, p_hdmi = (int(x) for x in sys.argv[3:6])
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("ERROR\tpw-dump did not print JSON")
        return
    nodes, meta = [], {}
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        props = (obj.get("info") or {}).get("props") or obj.get("props") or {}
        if obj.get("type") == "PipeWire:Interface:Node":
            name = str(props.get("node.name", ""))
            text = " ".join(str(props.get(k, "")) for k in (
                "node.name", "node.description", "node.nick", "alsa.card_name", "alsa.long_card_name",
                "api.alsa.card.name", "api.alsa.card.longname")).lower()
            nodes.append({"id": obj.get("id"), "name": name, "cls": str(props.get("media.class", "")),
                          "desc": clean(props.get("node.description") or props.get("node.nick") or name),
                          "prio": prio(props), "card": card_of(props, name), "text": text})
        elif obj.get("type") == "PipeWire:Interface:Metadata" and props.get("metadata.name") == "default":
            for item in obj.get("metadata") or []:
                value = item.get("value")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = {"name": value}
                if isinstance(value, dict) and value.get("name"):
                    meta[str(item.get("key"))] = clean(value["name"])

    sinks = [n for n in nodes if n["cls"] == "Audio/Sink"]
    sources = [n for n in nodes if n["cls"] == "Audio/Source"]
    usb_sinks = [n for n in sinks if n["name"].startswith("alsa_output.usb-")]
    usb_sources = [n for n in sources if n["name"].startswith("alsa_input.usb-")]
    hdmi_sinks = [n for n in sinks if n["name"].startswith("alsa_output.") and "hdmi" in n["name"].lower()
                  and n not in usb_sinks]

    # Are the drop-in's priorities active on the nodes that exist right now?
    expected = [(n, p_usb_sink) for n in usb_sinks] + [(n, p_usb_src) for n in usb_sources] + \
               [(n, p_hdmi) for n in hdmi_sinks]
    applied = "none" if not expected else ("yes" if all(n["prio"] == p for n, p in expected) else "no")

    spk_cands = [n for n in sinks if spk_hint in n["text"]] if spk_hint else usb_sinks
    mic_cands = [n for n in sources if mic_hint in n["text"]] if mic_hint else usb_sources

    def pick(cands, avoid_card, what, opt):
        if len(cands) <= 1:
            return (cands[0] if cands else None), ""
        # A USB speaker often has a (useless) microphone and a USB microphone a headphone
        # output: prefer devices on different cards for the speaker and the microphone.
        pool = [c for c in cands if avoid_card is None or c["card"] != avoid_card] or cands
        pool = sorted(pool, key=lambda c: (-c["prio"], c["id"] if isinstance(c["id"], int) else 0))
        if len(pool) == 1:
            return pool[0], ""
        listing = ", ".join("%s [%s]" % (c["desc"], c["id"]) for c in pool)
        return pool[0], "several %s: %s - chose the first; pick another with %s NAME" % (what, listing, opt)

    if len(spk_cands) <= 1 or len(mic_cands) > 1:
        spk, spk_note = pick(spk_cands, None, "speakers", "--speaker")
        mic, mic_note = pick(mic_cands, spk["card"] if spk else None, "microphones", "--mic")
    else:
        mic, mic_note = pick(mic_cands, None, "microphones", "--mic")
        spk, spk_note = pick(spk_cands, mic["card"] if mic else None, "speakers", "--speaker")
    if spk_hint and not spk:
        spk_note = "no sound output matches --speaker '%s'" % spk_hint
    if mic_hint and not mic:
        mic_note = "no microphone matches --mic '%s'" % mic_hint

    out = {
        "APPLIED": applied,
        "CONFIGURED_SINK": meta.get("default.configured.audio.sink", ""),
        "SINKS": "; ".join("%s [%s]" % (n["desc"], n["id"]) for n in sinks),
        "SPEAKER_NOTE": spk_note,
        "MIC_NOTE": mic_note,
    }
    for prefix, node in (("SPEAKER", spk), ("MIC", mic)):
        out[prefix + "_ID"] = str(node["id"]) if node else ""
        out[prefix + "_NAME"] = node["name"] if node else ""
        out[prefix + "_DESC"] = node["desc"] if node else ""
    for key, value in out.items():
        print("%s\t%s" % (key, clean(value)))


main()
PY
)

# Read the PipeWire graph once (read-only) and set the P_* variables. Fails if unreadable.
discover() {
    local out key val
    P_APPLIED="" P_CONFIGURED_SINK="" P_SINKS="" P_ERROR=""
    P_SPEAKER_ID="" P_SPEAKER_NAME="" P_SPEAKER_DESC="" P_SPEAKER_NOTE=""
    P_MIC_ID="" P_MIC_NAME="" P_MIC_DESC="" P_MIC_NOTE=""
    out="$(timeout 10 pw-dump 2>/dev/null | python3 -c "$PICKER" "$SPEAKER_HINT" "$MIC_HINT" \
        "$PRIO_USB_SINK" "$PRIO_USB_SOURCE" "$PRIO_HDMI_SINK")" || return 1
    while IFS=$'\t' read -r key val; do
        case "$key" in
            APPLIED | CONFIGURED_SINK | SINKS | ERROR | SPEAKER_ID | SPEAKER_NAME | \
                SPEAKER_DESC | SPEAKER_NOTE | MIC_ID | MIC_NAME | MIC_DESC | MIC_NOTE)
                printf -v "P_$key" '%s' "$val" ;;
        esac
    done <<<"$out"
    [[ -z "$P_ERROR" && -n "$P_APPLIED" ]]
}

# Print "<node.name><TAB><node.description>" of the current default (@DEFAULT_AUDIO_SINK@ ...).
default_node() {
    local out name desc
    out="$(timeout 10 wpctl inspect "$1" 2>/dev/null)" || return 1
    name="$(sed -n 's/^[[:space:]*]*node\.name = "\(.*\)"$/\1/p;T;q' <<<"$out")"
    desc="$(sed -n 's/^[[:space:]*]*node\.description = "\(.*\)"$/\1/p;T;q' <<<"$out")"
    [[ -n "$name" ]] || return 1
    printf '%s\t%s\n' "$name" "${desc:-$name}"
}

# Write stdin to $1 unless it already has that content. Returns 0 if (it would be) changed.
write_dropin() {
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

dropin_05() {
    cat <<EOF
# LittleBot: use the USB speaker and USB microphone instead of HDMI (the screen).
# Written by scripts/set_default_speaker.sh - remove it with: scripts/set_default_speaker.sh --undo
#
# WirePlumber 0.5 SPA-JSON fragment. "~" = POSIX extended regex (unanchored, hence "^";
# "[.]" is a literal dot). priority.session decides the default device unless one was
# chosen by hand (wpctl set-default / volume menu), so USB wins whenever it is plugged in.
# Without the USB speaker, HDMI is still used as a fallback, so sound never breaks.
# Sinks stay <= 1500: a higher sink priority can make its monitor the default source.
monitor.alsa.rules = [
  {
    matches = [
      { node.name = "~^alsa_output[.].*hdmi" }
    ]
    actions = {
      update-props = {
        priority.session = $PRIO_HDMI_SINK
      }
    }
  }
  {
    matches = [
      { node.name = "~^alsa_output[.]usb-" }
    ]
    actions = {
      update-props = {
        priority.session = $PRIO_USB_SINK
      }
    }
  }
  {
    matches = [
      { node.name = "~^alsa_input[.]usb-" }
    ]
    actions = {
      update-props = {
        priority.session = $PRIO_USB_SOURCE
      }
    }
  }
]
EOF
}

dropin_04() {
    cat <<EOF
-- LittleBot: use the USB speaker and USB microphone instead of HDMI (the screen).
-- Written by scripts/set_default_speaker.sh - remove it with: scripts/set_default_speaker.sh --undo
--
-- WirePlumber 0.4 Lua fragment, loaded after 50-alsa-config.lua. "matches" is a glob
-- (* and ?, whole name); later rules override earlier ones. priority.session decides the
-- default device unless one was chosen by hand (wpctl set-default / volume menu).
-- Without the USB speaker, HDMI is still used as a fallback, so sound never breaks.
-- Sinks stay <= 1500: a higher sink priority can make its monitor the default source.
table.insert(alsa_monitor.rules, {
  matches = { { { "node.name", "matches", "alsa_output.*hdmi*" } } },
  apply_properties = { ["priority.session"] = $PRIO_HDMI_SINK },
})
table.insert(alsa_monitor.rules, {
  matches = { { { "node.name", "matches", "alsa_output.usb-*" } } },
  apply_properties = { ["priority.session"] = $PRIO_USB_SINK },
})
table.insert(alsa_monitor.rules, {
  matches = { { { "node.name", "matches", "alsa_input.usb-*" } } },
  apply_properties = { ["priority.session"] = $PRIO_USB_SOURCE },
})
EOF
}

restart_wireplumber() {
    if run systemctl --user restart wireplumber; then
        return 0
    fi
    warn "Could not restart WirePlumber. Log out and in again (or reboot) to load the new rule."
    return 1
}

# ---------------------------------------------------------------------------
# 1. Checks
# ---------------------------------------------------------------------------
step "Checking PipeWire / WirePlumber"
((DRY_RUN)) && info "(dry run: nothing will be changed)"

if [[ "$(id -u)" -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" ]]; then
        die "Do not use sudo: sound settings belong to the desktop user. Run it as $SUDO_USER: scripts/set_default_speaker.sh"
    fi
    warn "Running as root: this changes root's own sound settings, not the desktop user's."
fi

# Over SSH the runtime directory (PipeWire / systemd --user sockets) may not be set.
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export XDG_RUNTIME_DIR
    info "XDG_RUNTIME_DIR was not set - using $XDG_RUNTIME_DIR"
fi

CONF_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DROPIN_05="$CONF_HOME/wireplumber/wireplumber.conf.d/$DROPIN_NAME.conf"
DROPIN_04="$CONF_HOME/wireplumber/main.lua.d/$DROPIN_NAME.lua"

WP_VERSION=""
if have wireplumber; then
    WP_VERSION="$(wireplumber --version 2>/dev/null |
        sed -n 's/^Linked with libwireplumber \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')" || WP_VERSION=""
fi
if [[ -z "$WP_VERSION" ]]; then  # the system config layout tells the series apart too
    if [[ -d /usr/share/wireplumber/main.lua.d ]]; then
        WP_VERSION="0.4"
    elif [[ -f /usr/share/wireplumber/wireplumber.conf ]]; then
        WP_VERSION="0.5"
    fi
fi
case "$WP_VERSION" in
    0.4) DROPIN="$DROPIN_04"; info "WirePlumber 0.4 (Lua configuration, Bookworm)" ;;
    0.[0-3]) die "WirePlumber $WP_VERSION is too old for this script; pick the speaker in the desktop volume menu." ;;
    "") DROPIN="$DROPIN_05"; warn "Could not tell the WirePlumber version - assuming 0.5 (Trixie)." ;;
    *) DROPIN="$DROPIN_05"; info "WirePlumber $WP_VERSION (SPA-JSON configuration)" ;;
esac

# Without PipeWire only --dry-run (to show the plan) and --undo (to remove the file) go on.
PW_OK=1
PW_MSG=""
MISSING=()
for tool in wpctl pw-dump python3 timeout; do
    have "$tool" || MISSING+=("$tool")
done
if ((${#MISSING[@]})); then
    PW_OK=0
    PW_MSG="Not found: ${MISSING[*]}. PipeWire tools come with Raspberry Pi OS (packages wireplumber, pipewire-bin, python3)."
elif ! timeout 10 wpctl status >/dev/null 2>&1; then
    PW_OK=0
    PW_MSG="Cannot reach PipeWire for user $(id -un) (XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset}). Run this in the robot's desktop session or via SSH as the desktop user; check: systemctl --user status pipewire wireplumber"
else
    info "PipeWire is running for $(id -un)"
fi
if ((PW_OK == 0)); then
    if ((DRY_RUN || UNDO)); then warn "$PW_MSG"; else die "$PW_MSG"; fi
fi

# ---------------------------------------------------------------------------
# --undo
# ---------------------------------------------------------------------------
if ((UNDO)); then
    step "Removing LittleBot's WirePlumber rule"
    REMOVED=0
    for f in "$DROPIN_05" "$DROPIN_04"; do
        if [[ -e "$f" ]]; then
            run rm -f "$f"
            REMOVED=1
        fi
    done
    if ((REMOVED)); then
        restart_wireplumber || true
    else
        info "nothing to remove (no $DROPIN_NAME rule installed)"
    fi
    info "The device saved with wpctl set-default stays the default. To let WirePlumber choose"
    info "again: wpctl clear-default"
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. The rule (drop-in)
# ---------------------------------------------------------------------------
step "WirePlumber rule: USB sound devices before HDMI"
CHANGED=0
if [[ "$DROPIN" == "$DROPIN_04" ]]; then
    if dropin_04 | write_dropin "$DROPIN"; then CHANGED=1; fi
else
    if dropin_05 | write_dropin "$DROPIN"; then CHANGED=1; fi
fi

if ((PW_OK == 0)); then
    # Only reachable in --dry-run: show the remaining steps without device ids.
    run systemctl --user restart wireplumber
    run wpctl set-default "<id of the USB speaker>"
    run wpctl set-default "<id of the USB microphone>"
    step "Done (dry run - nothing was changed)"
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Restart WirePlumber if the rule is new or not active yet
# ---------------------------------------------------------------------------
discover || P_APPLIED=""
if ((CHANGED)) || [[ "$P_APPLIED" != yes ]]; then
    step "Restarting WirePlumber"
    ((CHANGED)) || info "the rule is installed but not active yet"
    if restart_wireplumber && ((DRY_RUN == 0)); then
        info "waiting up to ${WAIT}s for the sound devices"
        start=$SECONDS
        while :; do
            # Ready: the speaker is back with the new priorities, and the microphone too
            # (or it has had 3 s to appear - there may be none).
            if discover && [[ "$P_APPLIED" != no && -n "$P_SPEAKER_ID" ]]; then
                if [[ -n "$P_MIC_ID" ]] || ((SECONDS - start >= 3)); then
                    break
                fi
            fi
            if ((SECONDS - start >= WAIT)); then
                break
            fi
            sleep 0.5
        done
        discover || die "Cannot read the PipeWire graph (pw-dump) after restarting WirePlumber."
    fi
else
    info "the rule is active (USB priority $PRIO_USB_SINK, HDMI $PRIO_HDMI_SINK) - no restart needed"
fi

# ---------------------------------------------------------------------------
# 4. Make the USB devices the saved default
# ---------------------------------------------------------------------------
step "Default speaker and microphone"
STATUS=0
[[ -n "$P_SPEAKER_NOTE" ]] && warn "$P_SPEAKER_NOTE"
[[ -n "$P_MIC_NOTE" ]] && warn "$P_MIC_NOTE"

if [[ -n "$P_SPEAKER_ID" ]]; then
    info "speaker: $P_SPEAKER_DESC ($P_SPEAKER_NAME)"
    run wpctl set-default "$P_SPEAKER_ID" || { warn "wpctl set-default $P_SPEAKER_ID failed"; STATUS=1; }
else
    STATUS=1
    warn "No USB speaker found${SPEAKER_HINT:+ matching \"$SPEAKER_HINT\"}. Outputs: ${P_SINKS:-none}"
    warn "Plug the speaker in (check: aplay -l) and run scripts/set_default_speaker.sh again."
    warn "The rule is installed: once plugged in, the USB speaker is preferred over HDMI."
    if [[ "$P_CONFIGURED_SINK" == *hdmi* ]]; then
        info "HDMI was saved as the chosen output; forgetting that so the USB speaker can win:"
        run wpctl clear-default 0 || warn "wpctl clear-default 0 failed"
    fi
fi

if [[ -n "$P_MIC_ID" ]]; then
    info "microphone: $P_MIC_DESC ($P_MIC_NAME)"
    run wpctl set-default "$P_MIC_ID" || warn "wpctl set-default $P_MIC_ID failed"
else
    warn "No USB microphone found${MIC_HINT:+ matching \"$MIC_HINT\"} - the default microphone is unchanged."
fi

# ---------------------------------------------------------------------------
# 5. Verify
# ---------------------------------------------------------------------------
if ((DRY_RUN)); then
    step "Done (dry run - nothing was changed)"
    exit 0
fi

step "Result"
SINK_NOW="" SOURCE_NOW=""
start=$SECONDS
while :; do  # set-default is applied asynchronously by WirePlumber
    SINK_NOW="$(default_node @DEFAULT_AUDIO_SINK@ || true)"
    if [[ -z "$P_SPEAKER_NAME" || "${SINK_NOW%%$'\t'*}" == "$P_SPEAKER_NAME" ]] || ((SECONDS - start >= 5)); then
        break
    fi
    sleep 0.5
done
SOURCE_NOW="$(default_node @DEFAULT_AUDIO_SOURCE@ || true)"

if [[ -z "$SINK_NOW" ]]; then
    warn "There is no default speaker at all (wpctl inspect @DEFAULT_AUDIO_SINK@ failed)."
    STATUS=1
else
    info "default speaker:    ${SINK_NOW#*$'\t'} (${SINK_NOW%%$'\t'*})"
    if [[ "${SINK_NOW%%$'\t'*}" == *hdmi* ]]; then
        warn "Sound still goes to HDMI (the screen). Check: wpctl status - then wpctl set-default <ID of the USB speaker>"
        STATUS=1
    elif [[ -n "$P_SPEAKER_NAME" && "${SINK_NOW%%$'\t'*}" != "$P_SPEAKER_NAME" ]]; then
        warn "The default speaker is not $P_SPEAKER_DESC. Check: wpctl status"
        STATUS=1
    fi
fi
[[ -n "$SOURCE_NOW" ]] && info "default microphone: ${SOURCE_NOW#*$'\t'} (${SOURCE_NOW%%$'\t'*})"

if ((STATUS == 0)); then
    info "Test it: speaker-test -t wav -c 2 -l 1   (start with a low volume)"
    info "If one app still plays on HDMI it was moved there by hand: restart it, or move it back"
    info "in the desktop volume menu."
fi
exit "$STATUS"
