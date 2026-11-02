#!/usr/bin/env bash
# LittleBot - sets up the Nanit baby camera view (docs/NANIT.md): builds the unofficial community bridge
# indiefan/home_assistant_nanit, signs you in to Nanit, and starts it as a systemd user service.
#
#   bash ~/LittleBot/scripts/setup_nanit.sh                 # set up (or finish setting up)
#   bash ~/LittleBot/scripts/setup_nanit.sh --check         # say what is there and what is missing; change nothing
#   bash ~/LittleBot/scripts/setup_nanit.sh --login-again   # sign in to Nanit again, then restart the bridge
#   bash ~/LittleBot/scripts/setup_nanit.sh --remove        # stop and remove the bridge, Go, the sign-in, the service
#
# Run it on LittleBot (the Raspberry Pi), as the desktop user, in a terminal: you type your own Nanit email,
# password and the code Nanit emails you. No sudo, nothing outside your home folder:
#   ~/.local/nanit-go          Go (pinned below, checked against go.dev's SHA-256), only to build the bridge
#   ~/.local/nanit-bridge      the bridge's source (pinned commit) and the program, bin/nanit
#   ~/LittleBot/data/nanit     the sign-in (session.json, mode 600: it lets the bridge use your Nanit account),
#                              cameras.json (camera names only) and the bridge's own folders
#   ~/.config/systemd/user/littlebot-nanit.service   runs scripts/nanit_bridge.sh: restarts on failure,
#                              at most 200 MB of memory, IPv4 only, RTMP port 1935 on the home network
set -euo pipefail

# The bridge, pinned: the newest commit when this was written (2025-05-27, "Merge pull request #29").
BRIDGE_REPO=https://github.com/indiefan/home_assistant_nanit.git
BRIDGE_COMMIT=d01a3cb958f152816529326eff96a3b26b220a27
# Go for linux-arm64 (the bridge's go.mod asks for go 1.24 or newer), with go.dev's published SHA-256.
GO_VERSION=go1.27.3
GO_SHA256=a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49
GO_URL="https://go.dev/dl/${GO_VERSION}.linux-arm64.tar.gz"
PORT=1935

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
GO_DIR=${LB_NANIT_GO_DIR:-$HOME/.local/nanit-go}
BRIDGE_DIR=${LB_NANIT_BRIDGE_DIR:-$HOME/.local/nanit-bridge}
DATA_DIR=${LB_NANIT_DATA_DIR:-$REPO_DIR/data/nanit}
UNIT_DIR=${LB_NANIT_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
UNIT=littlebot-nanit.service
SESSION="$DATA_DIR/session.json"
BIN="$BRIDGE_DIR/bin/nanit"
SRC="$BRIDGE_DIR/src"
HELPER="$REPO_DIR/scripts/nanit_helper.py"
RUNNER="$REPO_DIR/scripts/nanit_bridge.sh"

MODE=setup
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  --login-again) MODE=login ;;
  --remove) MODE=remove ;;
  -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

have_cmd() { command -v "$1" >/dev/null 2>&1; }
say() { printf '  %-8s %s\n' "$1" "$2"; }
has_session() { [ -f "$SESSION" ] && have_cmd python3 && python3 "$HELPER" has-session "$SESSION"; }
file_mode() { stat -c '%a' "$1" 2>/dev/null || echo "?"; }
go_ok() { [ -x "$GO_DIR/go/bin/go" ] && [ -f "$GO_DIR/.version" ] && [ "$(cat "$GO_DIR/.version")" = "$GO_VERSION" ]; }
src_ok() { [ -d "$SRC/.git" ] && [ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null)" = "$BRIDGE_COMMIT" ]; }
bin_ok() { [ -x "$BIN" ] && [ -f "$BRIDGE_DIR/.commit" ] && [ "$(cat "$BRIDGE_DIR/.commit")" = "$BRIDGE_COMMIT" ]; }
unit_state() { if have_cmd systemctl; then systemctl --user is-active "$UNIT" 2>/dev/null || true; else echo "no systemctl"; fi; }
unit_enabled() { if have_cmd systemctl; then systemctl --user is-enabled "$UNIT" 2>/dev/null || true; else echo "?"; fi; }

# ---------------------------------------------------------------- --check
check() {
  local missing=0
  echo "The Nanit baby camera on LittleBot:"
  local arch; arch=$(uname -m)
  if [ "$arch" = aarch64 ] || [ "$arch" = arm64 ]; then say ok "this is a 64-bit ARM computer ($arch)"; else say problem "this is $arch, not the robot's aarch64: run this on LittleBot"; missing=1; fi
  for cmd in git curl tar sha256sum python3 ip systemctl ffplay; do
    if have_cmd "$cmd"; then say ok "$cmd"; else say missing "$cmd"; missing=1; fi
  done
  if go_ok; then say ok "Go $GO_VERSION ($GO_DIR)"; else say missing "Go $GO_VERSION (only to build the bridge)"; missing=1; fi
  if src_ok; then say ok "the bridge's source at ${BRIDGE_COMMIT:0:12}"; else say missing "the bridge's source at ${BRIDGE_COMMIT:0:12}"; missing=1; fi
  if bin_ok; then say ok "the bridge program ($BIN)"; else say missing "the bridge program"; missing=1; fi
  if has_session; then
    local m; m=$(file_mode "$SESSION")
    if [ "$m" = 600 ]; then say ok "signed in to Nanit (session.json, mode 600)"; else say problem "signed in, but session.json is mode $m, not 600"; missing=1; fi
  else
    say missing "the Nanit sign-in"; missing=1
  fi
  if [ -f "$UNIT_DIR/$UNIT" ]; then say ok "the service ($UNIT: $(unit_enabled), $(unit_state))"; else say missing "the service $UNIT"; missing=1; fi
  if [ -f "$DATA_DIR/cameras.json" ]; then say ok "cameras listed (cameras.json)"; else say missing "no camera listed yet (the bridge writes it a few seconds after it starts)"; fi
  echo
  if [ "$missing" = 0 ]; then
    echo "Everything is set up. Say \"Hey LittleBot, show the baby camera\", or use the dashboard (Assistant page)."
  else
    echo "To set it up, run (no sudo; it asks you to sign in to Nanit):"
    echo
    echo "  bash $REPO_DIR/scripts/setup_nanit.sh"
  fi
}

# ---------------------------------------------------------------- --remove
remove() {
  echo "Removing the Nanit bridge from LittleBot..."
  if have_cmd systemctl; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT_DIR/$UNIT"
  if have_cmd systemctl; then systemctl --user daemon-reload >/dev/null 2>&1 || true; fi
  for d in "$GO_DIR" "$BRIDGE_DIR"; do
    case "$d" in
      */nanit-go|*/nanit-bridge) if [ -e "$d" ]; then chmod -R u+w "$d" 2>/dev/null || true; rm -rf "$d"; fi ;;
    esac
  done
  case "$DATA_DIR" in */nanit) rm -rf "$DATA_DIR" ;; esac
  echo "Done: the service, the bridge, Go and the Nanit sign-in are gone. To also remove LittleBot from your"
  echo "Nanit account's signed-in devices, use the Nanit app."
}

# ---------------------------------------------------------------- the steps
need() {
  local bad=()
  for cmd in git curl tar sha256sum python3 ip systemctl; do have_cmd "$cmd" || bad+=("$cmd"); done
  if [ "${#bad[@]}" -gt 0 ]; then
    echo "Missing: ${bad[*]}. Install them first (sudo apt install ${bad[*]}), then run this again." >&2
    exit 1
  fi
  local arch; arch=$(uname -m)
  if [ "$arch" != aarch64 ] && [ "$arch" != arm64 ]; then
    echo "This is $arch: run it on LittleBot (a Raspberry Pi 5, aarch64)." >&2
    exit 1
  fi
}

install_go() {
  if go_ok; then echo "Go $GO_VERSION is there."; return; fi
  echo "Downloading Go $GO_VERSION from go.dev..."
  mkdir -p "$(dirname "$GO_DIR")"
  DOWNLOAD_DIR=$(mktemp -d "$(dirname "$GO_DIR")/.nanit-go-download.XXXXXX")
  trap 'rm -rf "$DOWNLOAD_DIR"' EXIT  # also when the download fails half-way
  local tmp=$DOWNLOAD_DIR
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/go.tar.gz" "$GO_URL"
  local got; got=$(sha256sum "$tmp/go.tar.gz" | cut -d' ' -f1)
  if [ "$got" != "$GO_SHA256" ]; then
    echo "The Go download's checksum is wrong (got $got, go.dev says $GO_SHA256). Nothing installed." >&2
    exit 1
  fi
  if [ -e "$GO_DIR" ]; then chmod -R u+w "$GO_DIR" 2>/dev/null || true; rm -rf "$GO_DIR"; fi
  mkdir -p "$GO_DIR"
  tar -C "$GO_DIR" -xzf "$tmp/go.tar.gz"
  rm -rf "$tmp"
  echo "$GO_VERSION" > "$GO_DIR/.version"
  echo "Go $GO_VERSION installed in $GO_DIR."
}

fetch_bridge() {
  if src_ok; then echo "The bridge's source is there (${BRIDGE_COMMIT:0:12})."; return; fi
  echo "Downloading the bridge's source (${BRIDGE_COMMIT:0:12})..."
  rm -rf "$SRC"
  mkdir -p "$SRC"
  git -C "$SRC" init -q
  git -C "$SRC" remote add origin "$BRIDGE_REPO"
  git -C "$SRC" fetch -q --depth 1 origin "$BRIDGE_COMMIT"
  git -C "$SRC" -c advice.detachedHead=false checkout -q FETCH_HEAD
  if [ "$(git -C "$SRC" rev-parse HEAD)" != "$BRIDGE_COMMIT" ]; then
    echo "The bridge's source isn't the pinned commit. Nothing built." >&2
    exit 1
  fi
}

build_bridge() {
  if bin_ok; then echo "The bridge program is built."; return; fi
  echo "Building the bridge (a minute or two)..."
  mkdir -p "$BRIDGE_DIR/bin"
  local build=(env PATH="$GO_DIR/go/bin:$PATH" GOTOOLCHAIN=local GOPATH="$BRIDGE_DIR/gopath"
               GOCACHE="$BRIDGE_DIR/cache" GOFLAGS=-modcacherw CGO_ENABLED=0 GOOS=linux GOARCH=arm64
               GOMAXPROCS=2 GOMEMLIMIT=800MiB
               go build -p 2 -trimpath -ldflags "-s -w -X main.GitCommit=${BRIDGE_COMMIT:0:7}" -o "$BIN" ./cmd/nanit)
  # A memory cap (the robot has 4 GB and other work running): in its own scope when systemd allows it.
  if (cd "$SRC" && systemd-run --user --scope --quiet -p MemoryMax=1200M -- true) >/dev/null 2>&1; then
    (cd "$SRC" && systemd-run --user --scope --quiet -p MemoryMax=1200M -- "${build[@]}")
  else
    (cd "$SRC" && "${build[@]}")
  fi
  echo "$BRIDGE_COMMIT" > "$BRIDGE_DIR/.commit"
  echo "Built $BIN."
}

sign_in() {
  mkdir -p "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  echo
  python3 "$HELPER" login "$SESSION"
  echo
}

write_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT" <<EOF
# Written by LittleBot's scripts/setup_nanit.sh (docs/NANIT.md). Remove with: setup_nanit.sh --remove
[Unit]
Description=LittleBot: Nanit baby camera bridge (unofficial, indiefan/home_assistant_nanit ${BRIDGE_COMMIT:0:7})
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
Environment=LB_NANIT_BIN=$BIN
Environment=LB_NANIT_DATA=$DATA_DIR
Environment=LB_NANIT_PORT=$PORT
ExecStart=/bin/bash $RUNNER
Restart=on-failure
RestartSec=30
MemoryHigh=160M
MemoryMax=200M
Nice=5
UMask=0077
NoNewPrivileges=yes
# IPv4 only (and local sockets): the RTMP port is never offered on a public IPv6 address.
RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK

[Install]
WantedBy=default.target
EOF
  chmod 644 "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null
  echo "The service $UNIT is written and enabled."
}

restart_unit() {
  systemctl --user restart "$UNIT" || true
  sleep 3
  local state; state=$(unit_state)
  echo "The bridge is $state."
  if [ "$state" != active ]; then
    echo "To see why: systemctl --user status ${UNIT%.service}; journalctl --user -u ${UNIT%.service} -n 50"
  fi
}

case "$MODE" in
  check) check; exit 0 ;;
  remove) remove; exit 0 ;;
esac

if [ ! -t 0 ] && [ "${LB_NANIT_ASSUME_TERMINAL:-}" != 1 ]; then  # (the tests set that)
  echo "Run this in a terminal on LittleBot: it asks you to sign in to Nanit." >&2
  exit 1
fi
need

if [ "$MODE" = login ]; then
  sign_in
  has_session || { echo "Not signed in; nothing changed." >&2; exit 1; }
  if [ -f "$UNIT_DIR/$UNIT" ]; then restart_unit; else echo "Now run this script without --login-again to finish."; fi
  exit 0
fi

echo "Setting up the Nanit baby camera on LittleBot. It's unofficial: a community bridge, not an app from Nanit."
echo
install_go
fetch_bridge
build_bridge
if has_session; then
  echo "Already signed in to Nanit (to sign in again: --login-again)."
else
  sign_in
  has_session || { echo "Not signed in. Run this again to try once more." >&2; exit 1; }
fi
chmod 700 "$DATA_DIR"
chmod 600 "$SESSION"
write_unit
restart_unit
echo
echo "Done. In a few seconds the dashboard's Baby camera card lists your cameras. Say \"Hey LittleBot, show the"
echo "baby camera\". The Nanit app may switch to its cloud stream while LittleBot shows the picture."
