#!/usr/bin/env bash
# Deploys this project to the gateway server in one step, and rolls back if it does not come up.
#
#   bin/deploy.sh
#
# Settings (environment variables):
#   DEPLOY_HOST        ssh target, or "local" to deploy on this machine without ssh - how the
#                      gateway deploys itself from a clone on its own server (default: mainul35@homelabai)
#   DEPLOY_DIR         project directory on it     (default: model-gateway, relative to the remote home)
#   DEPLOY_PUBLIC_URL  public URL to check after   (default: https://ai-gateway.mainul35.dev)
#
# Never touched on the server: config/config.properties (secrets, SSO settings), .venv, logs, .env.
set -euo pipefail

HOST=${DEPLOY_HOST:-mainul35@homelabai}
REMOTE_DIR=${DEPLOY_DIR:-model-gateway}
PUBLIC_URL=${DEPLOY_PUBLIC_URL:-https://ai-gateway.mainul35.dev}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

PYTHON=${PYTHON:-$(command -v python || command -v python3)}

step() { printf '\n==> %s\n' "$*"; }

# Runs a command on the server, from the home directory, with this script's stdin: over ssh, or here
# when the server is this machine
on_server() {
    if [ "$HOST" = local ]; then
        (cd "$HOME" && bash -c "$*")
    else
        ssh -o BatchMode=yes "$HOST" "$*"
    fi
}

step "Checking the code compiles before sending anything"
"$PYTHON" - <<'PY'
import pathlib, sys
failed = []
for path in list(pathlib.Path("app").rglob("*.py")) + list(pathlib.Path("utils").rglob("*.py")):
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as e:
        failed.append(f"{path}:{e.lineno}: {e.msg}")
if failed:
    print("\n".join(failed)); sys.exit(1)
print("all Python files compile")
PY

# Compiling is not enough: a backslash eaten by a shell leaves a backspace inside a regular
# expression that compiles perfectly and matches nothing it was written to match.
"$PYTHON" scripts/check_control_chars.py app utils scripts

STAMP=$(date +%Y%m%d-%H%M%S)

step "Backing up the current server code ($STAMP)"
on_server "cd $REMOTE_DIR && mkdir -p .deploy-backup && \
  tar czf .deploy-backup/$STAMP.tar.gz app utils bin config/engines.yaml config/models.yaml config/mcp.yaml config/knowledge.yaml config/searxng \
      docker-compose.gateway.yml docker-compose.homelab.yml requirements.txt requirements-gateway.txt 2>/dev/null; \
  ls -1t .deploy-backup/*.tar.gz | tail -n +6 | xargs -r rm -f; \
  echo \"kept \$(ls .deploy-backup | wc -l) backup(s)\""

step "Syncing the project"
tar czf - \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='config/config.properties' \
    app utils bin config/engines.yaml config/models.yaml config/mcp.yaml config/knowledge.yaml config/searxng scripts \
    docker-compose.gateway.yml docker-compose.homelab.yml .env.example \
    requirements.txt requirements-gateway.txt \
  | on_server "tar xzf - -C $REMOTE_DIR && echo synced"

step "Installing dependencies if they changed, restarting, checking health"
on_server "REMOTE_DIR=$REMOTE_DIR STAMP=$STAMP bash -s" <<'REMOTE'
set -u
cd "$REMOTE_DIR" || exit 1

# Files edited on Windows can arrive with CRLF line endings, which breaks shell scripts
sed -i 's/\r$//' bin/*.sh 2>/dev/null
chmod +x bin/*.sh

mkdir -p .deploy-state
wanted=$(cat requirements.txt requirements-gateway.txt | sha256sum | cut -d' ' -f1)
if [ "$wanted" != "$(cat .deploy-state/requirements.sha 2>/dev/null)" ]; then
    echo "requirements changed, installing"
    .venv/bin/pip install -q -r requirements.txt -r requirements-gateway.txt && echo "$wanted" > .deploy-state/requirements.sha
else
    echo "requirements unchanged"
fi

restart() {
    # Uvicorn drains what it is serving before it goes, and a picture job can take a minute. Starting
    # the new one while the old still holds the port makes it exit, and the watchdog then restarts
    # every minute until someone notices.
    pkill -f 'uvicorn app.main:app' >/dev/null 2>&1
    for _ in $(seq 1 30); do
        (echo > /dev/tcp/127.0.0.1/9000) >/dev/null 2>&1 || break
        sleep 1
    done
    (echo > /dev/tcp/127.0.0.1/9000) >/dev/null 2>&1 && { pkill -9 -f 'uvicorn app.main:app'; sleep 2; }
    ./bin/gateway-start.sh
    for _ in $(seq 1 30); do
        sleep 1
        curl -sf -m 3 http://127.0.0.1:9000/health >/dev/null 2>&1 && return 0
    done
    return 1
}

if restart; then
    echo "healthy: $(curl -s http://127.0.0.1:9000/health)"
else
    echo "NOT HEALTHY - last log lines:"
    tail -15 logs/gateway.log
    echo "rolling back to $STAMP"
    tar xzf ".deploy-backup/$STAMP.tar.gz"
    if restart; then
        echo "rolled back; the previous version is running again"
    else
        echo "rollback also failed to start - check logs/gateway.log"
    fi
    exit 1
fi
REMOTE

step "Checking the public URL"
# Several times, not once. The tunnel keeps a pool of connections to this server, and restarting it
# leaves every one of them pointing at a process that is gone. One request opens one fresh
# connection and proves nothing about the rest: the next person through gets a stale one, waits,
# and is shown a 524 by Cloudflare while the gateway sits here perfectly healthy. Going round the
# pool here means the deploy finds those, not somebody trying to use the thing.
failures=0
for attempt in 1 2 3 4 5 6; do
    result=$(curl -s -m 25 -o /dev/null -w '%{http_code} in %{time_total}s' "$PUBLIC_URL/health" || echo "no answer")
    echo "  $attempt: $PUBLIC_URL/health -> $result"
    case "$result" in 200*) ;; *) failures=$((failures + 1)) ;; esac
done
[ "$failures" = "0" ] || { echo "public check failed $failures of 6 times (the server itself is healthy,";     echo "so this is between Cloudflare and here)"; exit 1; }

printf '\nDeployed %s\n' "$STAMP"
