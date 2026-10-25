#!/usr/bin/env bash
# The checks every branch and pull request must pass; the Jenkinsfile runs them, and so can you:
#
#   ci/checks.sh
#
# Needs python3 with the venv module. Installs the wake front's test dependencies into .ci-venv.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

step() { printf '\n==> %s\n' "$*"; }

step "Python compiles"
python3 -m compileall -q app utils scripts wake

step "No control characters in code"
python3 scripts/check_control_chars.py app utils scripts

step "Every page's JavaScript parses"
python3 scripts/check_page_scripts.py

step "Video stories' character voices are real"
python3 scripts/check_voice_table.py

step "No credentials committed"
python3 scripts/check_secrets.py --all

step "Shell scripts parse"
for f in bin/*.sh ops/*/*.sh ci/*.sh scripts/git-hooks/pre-push; do
    bash -n "$f"
done
echo "all parse"

step "Wake front tests"
[ -x .ci-venv/bin/python ] || python3 -m venv .ci-venv
.ci-venv/bin/pip install -q -r wake/requirements.txt pytest
.ci-venv/bin/python -m pytest -q wake/
