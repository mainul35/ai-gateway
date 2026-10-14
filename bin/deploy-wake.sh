#!/usr/bin/env bash
# Deploys the wake front (wake/) on this machine - proxy-vm - by hand, and rolls back if it does not
# come up. The Jenkins job deploy-ai-gateway does the same from a Harbor image; both use compose
# project gateway-wake, so they manage the same container.
#
#   bin/deploy-wake.sh
#
# The wake front runs from WAKE_DIR, outside any clone, so its wake.env (which git never sees)
# survives every deploy. This script copies the code there and rebuilds; Docker only recreates the
# container when the image actually changed, so a deploy with no wake/ changes interrupts nothing.
#
# Settings (environment variables):
#   WAKE_DIR        where the wake front runs  (default: ~/gateway-wake)
#   WAKE_ENV_FILE   settings to use; copied to $WAKE_DIR/wake.env
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WAKE_DIR=${WAKE_DIR:-$HOME/gateway-wake}
IMAGE=gateway-wake-front
CONTAINER=gateway-wake-front
PROJECT=gateway-wake

step() { printf '\n==> %s\n' "$*"; }

mkdir -p "$WAKE_DIR"
if [ -n "${WAKE_ENV_FILE:-}" ]; then
    install -m 600 "$WAKE_ENV_FILE" "$WAKE_DIR/wake.env"
fi
if [ ! -f "$WAKE_DIR/wake.env" ]; then
    # First run of the pipeline: take the settings from the hand-made install, if there is one
    for old in "$HOME/ai-gateway-src/wake/wake.env" "$ROOT/wake/wake.env"; do
        if [ -f "$old" ]; then cp "$old" "$WAKE_DIR/wake.env"; echo "copied settings from $old"; break; fi
    done
fi
if [ ! -f "$WAKE_DIR/wake.env" ]; then
    echo "no $WAKE_DIR/wake.env: copy wake/wake.env.example there and fill it in (see wake/README.md)"
    exit 1
fi
chmod 600 "$WAKE_DIR/wake.env"

step "Syncing the wake front to $WAKE_DIR"
# Everything in wake/ except the settings, which belong to this machine
tar -C "$ROOT/wake" --exclude=wake.env --exclude=__pycache__ -cf - . | tar -C "$WAKE_DIR" -xf -

step "Building"
previous=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)
[ -n "$previous" ] && docker tag "$IMAGE" "$IMAGE:previous"
(cd "$WAKE_DIR" && docker compose -p "$PROJECT" build -q)

# A container started by hand from another folder belongs to another compose project and holds the
# name; replace it once, after the new image is ready, so the gap is a second or two.
owner=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER" 2>/dev/null || true)
if [ -n "$owner" ] && [ "$owner" != "$PROJECT" ]; then
    echo "replacing the $CONTAINER started from project '$owner'"
    docker rm -f "$CONTAINER" >/dev/null
fi

step "Starting"
(cd "$WAKE_DIR" && docker compose -p "$PROJECT" up -d)

healthy() {
    for _ in $(seq 1 20); do
        sleep 1
        curl -sf -m 3 http://127.0.0.1:9100/__wake/health >/dev/null 2>&1 && return 0
    done
    return 1
}

if healthy; then
    echo "healthy: $(curl -s http://127.0.0.1:9100/__wake/status)"
    exit 0
fi

echo "NOT HEALTHY - last log lines:"
docker logs --tail 15 "$CONTAINER" 2>&1 || true
if [ -n "$previous" ]; then
    echo "rolling back to the previous image"
    docker tag "$IMAGE:previous" "$IMAGE"
    (cd "$WAKE_DIR" && docker compose -p "$PROJECT" up -d --force-recreate)
    healthy && echo "rolled back; the previous version is running again" || echo "rollback also failed"
fi
exit 1
