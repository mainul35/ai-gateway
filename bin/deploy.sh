#!/usr/bin/env bash
# Deploys this project to the gateway server in one step, and rolls back if it does not come up.
#
#   bin/deploy.sh
#
# Settings (environment variables):
#   DEPLOY_HOST        ssh target                  (default: mainul35@homelabai)
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

step() { printf '\n==> %s\n' "$*"; }

step "Checking the code compiles before sending anything"
python - <<'PY'
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

STAMP=$(date +%Y%m%d-%H%M%S)

step "Backing up the current server code ($STAMP)"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE_DIR && mkdir -p .deploy-backup && \
  tar czf .deploy-backup/$STAMP.tar.gz app utils bin config/engines.yaml config/models.yaml config/searxng \
      docker-compose.gateway.yml docker-compose.homelab.yml requirements.txt requirements-gateway.txt 2>/dev/null; \
  ls -1t .deploy-backup/*.tar.gz | tail -n +6 | xargs -r rm -f; \
  echo \"kept \$(ls .deploy-backup | wc -l) backup(s)\""

step "Syncing the project"
tar czf - \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='config/config.properties' \
    app utils bin config/engines.yaml config/models.yaml config/searxng scripts \
    docker-compose.gateway.yml docker-compose.homelab.yml .env.example \
    requirements.txt requirements-gateway.txt \
  | ssh -o BatchMode=yes "$HOST" "tar xzf - -C $REMOTE_DIR && echo synced"

step "Installing dependencies if they changed, restarting, checking health"
ssh -o BatchMode=yes "$HOST" "REMOTE_DIR=$REMOTE_DIR STAMP=$STAMP bash -s" <<'REMOTE'
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
    pkill -f 'uvicorn app.main:app' >/dev/null 2>&1; sleep 2
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
code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' "$PUBLIC_URL/health" || true)
echo "$PUBLIC_URL/health -> $code"
[ "$code" = "200" ] || { echo "public check failed (the server itself is healthy)"; exit 1; }

printf '\nDeployed %s\n' "$STAMP"
