#!/usr/bin/env bash
# Makes sure homelabai is awake and its gateway answering, waking it with Wake-on-LAN if needed.
# Runs on proxy-vm (anything on the same LAN works). Exits 0 once the gateway is healthy.
#
#   bin/wake-homelabai.sh
#
# Settings (environment variables), read from the wake front's wake.env when not set:
#   GATEWAY_URL     the gateway on the LAN      (default: WAKE_UPSTREAM from wake.env)
#   WAKE_MAC        homelabai's NIC             (default: from wake.env)
#   WAKE_BROADCAST  the LAN broadcast address   (default: from wake.env, else 255.255.255.255)
#   WAKE_ENV        where wake.env lives        (default: ~/gateway-wake/wake.env)
#   WAKE_TIMEOUT    seconds to wait             (default: 300)
set -uo pipefail

WAKE_ENV=${WAKE_ENV:-$HOME/gateway-wake/wake.env}
if [ -r "$WAKE_ENV" ]; then
    from_env() { grep -E "^$1=" "$WAKE_ENV" | tail -1 | cut -d= -f2-; }
    GATEWAY_URL=${GATEWAY_URL:-$(from_env WAKE_UPSTREAM)}
    WAKE_MAC=${WAKE_MAC:-$(from_env WAKE_MAC)}
    WAKE_BROADCAST=${WAKE_BROADCAST:-$(from_env WAKE_BROADCAST)}
fi
WAKE_BROADCAST=${WAKE_BROADCAST:-255.255.255.255}
WAKE_TIMEOUT=${WAKE_TIMEOUT:-300}
: "${GATEWAY_URL:?set GATEWAY_URL or WAKE_UPSTREAM in $WAKE_ENV}"
: "${WAKE_MAC:?set WAKE_MAC or WAKE_MAC in $WAKE_ENV}"

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

echo "homelabai is asleep; waking it"
began=$(date +%s)
last_sent=0
while (( $(date +%s) - began < WAKE_TIMEOUT )); do
    if healthy; then
        echo "homelabai is awake after $(( $(date +%s) - began )) s"
        exit 0
    fi
    # Resent every 15 s, in case one was lost
    if (( $(date +%s) - last_sent >= 15 )); then
        magic_packet || echo "could not send the magic packet"
        last_sent=$(date +%s)
    fi
    sleep 2
done
echo "homelabai did not wake within ${WAKE_TIMEOUT} s"
exit 1
