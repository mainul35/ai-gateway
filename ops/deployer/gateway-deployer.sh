#!/usr/bin/env bash
# The deployment pipeline: deploys every green commit on the deploy branch, from proxy-vm.
#
# Runs every 2 minutes from gateway-deployer.timer. Each run:
#   1. fetches the branch; stops if nothing new since the last deploy
#   2. waits for CI (.github/workflows/ci.yml) on that commit; a failed commit is skipped for good
#   3. wakes homelabai if it is asleep              (bin/wake-homelabai.sh)
#   4. deploys the gateway to homelabai over ssh    (bin/deploy.sh: compile check, backup, health
#                                                    check, automatic rollback)
#   5. deploys the wake front on this machine        (bin/deploy-wake.sh: health check, rollback)
#   6. records the commit as deployed
#
# It pulls rather than being pushed to: nothing on the internet needs a way into the LAN, and the
# repository being public is no risk to these machines (a self-hosted GitHub runner on a public
# repository runs code from strangers' pull requests).
#
# Run by hand:   ops/deployer/gateway-deployer.sh            (deploys if there is something new)
#                FORCE=1 ops/deployer/gateway-deployer.sh    (deploys the branch head again)
# Logs:          journalctl -u gateway-deployer -f
#
# Settings: /etc/default/gateway-deployer (see gateway-deployer.conf)
set -uo pipefail

CONF=${DEPLOYER_CONF:-/etc/default/gateway-deployer}
[ -r "$CONF" ] && . "$CONF"

: "${REPO:=mainul35/ai-gateway}"
: "${BRANCH:=develop}"
: "${SRC:=$HOME/gateway-deployer/src}"                # a clone used only by this pipeline
: "${STATE:=$HOME/gateway-deployer/state}"
: "${GATEWAY_SSH:=mainul35@192.168.50.146}"
: "${GATEWAY_DIR:=model-gateway}"
: "${PUBLIC_URL:=https://ai-gateway.mainul35.dev}"
: "${REQUIRE_CI:=1}"
: "${CI_WAIT_MINUTES:=30}"                            # give up on a commit whose CI never reports
: "${FORCE:=0}"

mkdir -p "$STATE"

# This file lives in the clone it is about to check out a new commit into, and bash reads a script
# as it goes. Run from a copy, so a deploy that changes this script cannot change it mid-run.
if [ "${DEPLOYER_COPY:-}" != 1 ]; then
    cp "$0" "$STATE/running.sh" && DEPLOYER_COPY=1 exec bash "$STATE/running.sh" "$@"
fi

exec 9>"$STATE/lock"
flock -n 9 || { echo "another deploy is running"; exit 0; }

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# --- 1. anything new? --------------------------------------------------------------------------
if [ ! -d "$SRC/.git" ]; then
    git clone -q --branch "$BRANCH" "https://github.com/$REPO" "$SRC" || exit 1
fi
git -C "$SRC" fetch -q origin "$BRANCH" || { say "fetch failed"; exit 1; }
sha=$(git -C "$SRC" rev-parse FETCH_HEAD)
short=${sha:0:7}

if [ "$FORCE" != 1 ]; then
    [ "$sha" = "$(cat "$STATE/deployed" 2>/dev/null)" ] && exit 0
    [ "$sha" = "$(cat "$STATE/rejected" 2>/dev/null)" ] && exit 0
fi

# --- 2. CI ---------------------------------------------------------------------------------------
if [ "$REQUIRE_CI" = 1 ] && [ "$FORCE" != 1 ]; then
    verdict=$(curl -sf -m 15 -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO/commits/$sha/check-runs" | python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs:
    print("none")
elif any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "neutral", "skipped") for r in runs):
    print("success")
else:
    print("failure: " + ", ".join(r["name"] for r in runs if r["conclusion"] not in ("success", "neutral", "skipped")))
') || verdict="unknown"

    case "$verdict" in
        success) ;;
        failure*)
            say "$short not deployed, CI failed ($verdict)"
            echo "$sha" > "$STATE/rejected"
            exit 0 ;;
        *)
            # No result yet. Wait, but not forever: a commit CI never looks at is not deployed either.
            first_seen=$(cat "$STATE/waiting.$sha" 2>/dev/null || { date +%s | tee "$STATE/waiting.$sha"; })
            if (( $(date +%s) - first_seen > CI_WAIT_MINUTES * 60 )); then
                say "$short not deployed: no CI result after $CI_WAIT_MINUTES min ($verdict)"
                echo "$sha" > "$STATE/rejected"
            else
                say "$short waiting for CI ($verdict)"
            fi
            exit 0 ;;
    esac
fi
rm -f "$STATE"/waiting.*

say "deploying $short ($(git -C "$SRC" log -1 --format=%s "$sha"))"
git -C "$SRC" checkout -q --force --detach "$sha" || exit 1
cd "$SRC"

# --- 3-5. wake, gateway, wake front --------------------------------------------------------------
gateway_ip=${GATEWAY_SSH#*@}
GATEWAY_URL=${GATEWAY_URL:-http://$gateway_ip:9000} bin/wake-homelabai.sh || { say "$short: could not wake homelabai"; exit 1; }

if ! DEPLOY_HOST="$GATEWAY_SSH" DEPLOY_DIR="$GATEWAY_DIR" DEPLOY_PUBLIC_URL="$PUBLIC_URL" bin/deploy.sh; then
    say "$short: gateway deploy failed (rolled back on the server; see above)"
    exit 1
fi

if ! bin/deploy-wake.sh; then
    say "$short: wake front deploy failed (see above); the gateway itself is deployed"
    exit 1
fi

echo "$sha" > "$STATE/deployed"
say "deployed $short"
