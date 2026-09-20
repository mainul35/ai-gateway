#!/usr/bin/env bash
# Starts the gateway if it is not already serving.
#
# Safe to run repeatedly, which is how it is used: cron runs it once at boot and again every minute
# as a watchdog. It exits immediately when the gateway is already healthy.
#
# Install (no root needed):
#   crontab -e
#   @reboot   /path/to/model-gateway/bin/gateway-start.sh
#   * * * * * /path/to/model-gateway/bin/gateway-start.sh
#
# Override defaults with environment variables: GATEWAY_PORT, GATEWAY_DB_PORT, GATEWAY_LOG.
set -u

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PORT=${GATEWAY_PORT:-9000}
DB_PORT=${GATEWAY_DB_PORT:-5433}
LOG=${GATEWAY_LOG:-$APP_DIR/logs/gateway.log}
MAX_LOG_BYTES=52428800  # 50 MB

cd "$APP_DIR" || exit 1
mkdir -p "$(dirname "$LOG")"

# Already answering? Nothing to do. Ten seconds, because a picture job can keep the gateway busy
# for a moment and killing it then would cut off whoever is waiting for the picture.
if curl -sf -m 10 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    exit 0
fi

# One missed answer is not proof of anything. Ask again before doing something drastic.
sleep 5
if curl -sf -m 10 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    exit 0
fi

# A process may exist but be wedged; clear it before starting a fresh one. Uvicorn finishes the
# requests it has in hand first, so wait for the port rather than racing it: a new process that
# cannot bind exits at once, and the next minute would start the whole dance again.
pkill -f 'uvicorn app.main:app' >/dev/null 2>&1
for _ in $(seq 1 20); do
    (echo > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1 || break
    sleep 1
done
# Still holding the port after twenty seconds: it is wedged, so insist.
if (echo > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1; then
    pkill -9 -f 'uvicorn app.main:app' >/dev/null 2>&1
    sleep 2
fi

# The database runs in Docker and may still be starting up right after a reboot.
for _ in $(seq 1 60); do
    (echo > "/dev/tcp/127.0.0.1/$DB_PORT") >/dev/null 2>&1 && break
    sleep 2
done

# Keep the log from growing without bound.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
    mv "$LOG" "$LOG.1"
fi

echo "=== $(date -Is) starting gateway" >> "$LOG"
export CONFIG_FILE=${CONFIG_FILE:-$APP_DIR/config/config.properties}
setsid nohup "$APP_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" \
    >> "$LOG" 2>&1 < /dev/null &
disown
