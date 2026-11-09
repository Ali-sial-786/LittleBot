#!/usr/bin/env bash
# LittleBot - starts the Nanit bridge (docs/NANIT.md). Run by the systemd user unit littlebot-nanit.service, which
# scripts/setup_nanit.sh writes; not meant to be run by hand.
#
# At each start it works out the robot's address on the home network now (DHCP can change it): the camera sends its
# picture to rtmp://<that address>:<port>/local/<baby uid>. It refuses an address that isn't a home-network
# (private) one, so the stream is never offered on the internet. It tidies the sign-in file, then runs the bridge
# from the data folder (the bridge reads a .env file from where it starts: there is none there), and once the
# bridge has listed the cameras it writes cameras.json (names and baby uids only) for LittleBot.
#
#   LB_NANIT_BIN    the bridge program        LB_NANIT_DATA       the data folder (data/nanit)
#   LB_NANIT_PORT   the RTMP port (1935)      LB_NANIT_LOG_LEVEL  the bridge's log level (info)
set -euo pipefail

BIN=${LB_NANIT_BIN:?LB_NANIT_BIN is not set}
DATA=${LB_NANIT_DATA:?LB_NANIT_DATA is not set}
PORT=${LB_NANIT_PORT:-1935}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HELPER="$HERE/nanit_helper.py"
umask 077

lan_ip() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1; }
is_private() {
  case "$1" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    *) return 1 ;;
  esac
}

ip_addr=""
for _ in $(seq 1 30); do
  ip_addr=$(lan_ip || true)
  [ -n "$ip_addr" ] && break
  sleep 2
done
if [ -z "$ip_addr" ]; then
  echo "nanit: no network address yet; trying again later" >&2
  exit 1
fi
if ! is_private "$ip_addr"; then
  echo "nanit: $ip_addr is not a home-network address; not starting (the camera stream must stay on the home network)" >&2
  exit 1
fi

python3 "$HELPER" tidy "$DATA/session.json"

export NANIT_RTMP_ENABLED=true
export NANIT_RTMP_ADDR="$ip_addr:$PORT"
export NANIT_DATA_DIR="$DATA"
export NANIT_SESSION_FILE="$DATA/session.json"
export NANIT_LOG_LEVEL="${LB_NANIT_LOG_LEVEL:-info}"
export NANIT_MQTT_ENABLED=false
export NANIT_EVENTS_POLLING=false
unset NANIT_EMAIL NANIT_PASSWORD NANIT_REFRESH_TOKEN
echo "nanit: the camera sends to rtmp://$ip_addr:$PORT/local/<baby>; LittleBot plays it from 127.0.0.1"

# The camera list, once the bridge has fetched it (it saves it in the session file within a few seconds).
(
  for _ in $(seq 1 60); do
    sleep 3
    if python3 "$HELPER" cameras "$DATA/session.json" "$DATA/cameras.json"; then
      exit 0
    fi
  done
  echo "nanit: the bridge hasn't listed a camera after 3 minutes" >&2
) &

cd "$DATA"
exec "$BIN"
