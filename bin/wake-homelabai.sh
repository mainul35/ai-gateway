#!/usr/bin/env bash
# Makes sure homelabai is awake and its gateway answering, waking it if needed. Exits 0 once the
# gateway is healthy. Used by the Jenkins pipeline before it deploys, and by hand.
#
#   bin/wake-homelabai.sh
#
# Two ways to wake it, tried in this order:
#   - through the wake front (WAKE_FRONT_URL): works from anywhere that can reach proxy-vm, including
#     a Jenkins container whose own broadcasts would never reach the LAN
#   - a Wake-on-LAN packet sent from here (WAKE_MAC): needs this machine on the LAN itself
#
# Settings (environment variables; values missing here are read from wake.env if it exists):
#   GATEWAY_URL     the gateway on the LAN       (default: WAKE_UPSTREAM from wake.env)
#   WAKE_FRONT_URL  the wake front               (e.g. http://192.168.50.81:9100)
#   WAKE_MAC        homelabai's NIC              (default: from wake.env)
#   WAKE_BROADCAST  the LAN broadcast address    (default: from wake.env, else 255.255.255.255)
#   WAKE_ENV        where wake.env lives         (default: ~/gateway-wake/wake.env)
#   WAKE_TIMEOUT    seconds to wait              (default: 300)
set -uo pipefail

WAKE_ENV=${WAKE_ENV:-$HOME/gateway-wake/wake.env}
if [ -r "$WAKE_ENV" ]; then
    from_env() { grep -E "^$1=" "$WAKE_ENV" | tail -1 | cut -d= -f2-; }
    GATEWAY_URL=${GATEWAY_URL:-$(from_env WAKE_UPSTREAM)}
    WAKE_MAC=${WAKE_MAC:-$(from_env WAKE_MAC)}
    WAKE_BROADCAST=${WAKE_BROADCAST:-$(from_env WAKE_BROADCAST)}
fi
WAKE_FRONT_URL=${WAKE_FRONT_URL:-}
WAKE_MAC=${WAKE_MAC:-}
WAKE_BROADCAST=${WAKE_BROADCAST:-255.255.255.255}
WAKE_TIMEOUT=${WAKE_TIMEOUT:-300}
: "${GATEWAY_URL:?set GATEWAY_URL (e.g. http://192.168.50.146:9000)}"
if [ -z "$WAKE_FRONT_URL" ] && [ -z "$WAKE_MAC" ]; then
    echo "set WAKE_FRONT_URL or WAKE_MAC, so there is a way to wake it"; exit 2
fi

healthy() { curl -sf -m 3 "$GATEWAY_URL/health" >/dev/null 2>&1; }

if healthy; then
    echo "homelabai is awake"
    exit 0
fi

magic_packet() {
    python3 - "$WAKE_MAC" "$WAKE_BROADCAST" <<'PY'
import re, socket, sys
mac = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", sys.argv[1]))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(b"\xff" * 6 + mac * 16, (sys.argv[2], 9))
PY
}

wake() {
    if [ -n "$WAKE_FRONT_URL" ]; then
        # The wake front keeps resending packets on its own for 5 min; a 429 only means someone
        # pressed it in the last 2 min, so it is waking already
        curl -s -m 5 -o /dev/null -X POST "$WAKE_FRONT_URL/__wake/now" && return 0
        echo "wake front unreachable at $WAKE_FRONT_URL"
    fi
    if [ -n "$WAKE_MAC" ]; then
        magic_packet || echo "could not send the magic packet"
    fi
}

echo "homelabai is asleep; waking it"
began=$(date +%s)
last_sent=0
while (( $(date +%s) - began < WAKE_TIMEOUT )); do
    if healthy; then
        echo "homelabai is awake after $(( $(date +%s) - began )) s"
        exit 0
    fi
    if (( $(date +%s) - last_sent >= 30 )); then
        wake
        last_sent=$(date +%s)
    fi
    sleep 2
done
echo "homelabai did not wake within ${WAKE_TIMEOUT} s"
exit 1
