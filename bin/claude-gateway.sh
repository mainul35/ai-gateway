#!/usr/bin/env bash
# Runs Claude Code against this AI gateway, in this terminal only - macOS and Linux
# (bin/claude-gateway.ps1 is the same for Windows).
#
#   ./claude-gateway.sh
#   ./claude-gateway.sh --model qwen3-coder:30b --context 32768
#   ./claude-gateway.sh -p "summarise this repo" --permission-mode acceptEdits
#
# --model, --context, --gateway and --max-output are this script's; everything else goes to claude as
# typed. Everything is set for the one `claude` process this starts: nothing is written to
# settings.json, so every other Claude Code session - and the desktop app - keeps using Anthropic's
# models.
#
# The key is taken from, in order: the AI_GATEWAY_API_KEY environment variable, the same name in
# ~/.qwen/.env (where Qwen Code already reads it), or a prompt that does not echo what is typed.
#
# Anthropic does not support Claude Code on non-Claude models; this runs it on your own anyway.
#
# Written for the bash 3.2 macOS still ships, so no associative arrays or ${var,,}.
set -euo pipefail

MODEL=qwen3-next-80b
# The model's real context window. Claude Code assumes 200k for a name it does not know, and would let
# a conversation grow past what the model can read.
CONTEXT=131072
GATEWAY=https://ai-gateway.mainul35.dev
MAX_OUTPUT=16384

claude_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --model)      MODEL=$2; shift 2 ;;
        --context)    CONTEXT=$2; shift 2 ;;
        --gateway)    GATEWAY=$2; shift 2 ;;
        --max-output) MAX_OUTPUT=$2; shift 2 ;;
        *)            claude_args+=("$1"); shift ;;
    esac
done

command -v claude >/dev/null 2>&1 || {
    echo "claude is not installed: https://docs.claude.com/en/docs/claude-code/setup" >&2; exit 1; }

key=${AI_GATEWAY_API_KEY:-}
if [ -z "$key" ] && [ -f "$HOME/.qwen/.env" ]; then
    key=$(sed -n 's/^[[:space:]]*AI_GATEWAY_API_KEY[[:space:]]*=[[:space:]]*//p' "$HOME/.qwen/.env" \
          | head -n 1 | tr -d '\r' | sed "s/^[\"']//; s/[\"'][[:space:]]*\$//; s/[[:space:]]*\$//")
fi
if [ -z "$key" ]; then
    read -r -s -p "AI gateway API key: " key || true; echo
fi
[ -n "$key" ] || { echo "No gateway key. Create one on the gateway's Dashboard, then run this again." >&2; exit 1; }

# Compaction starts before the window is full; Claude Code will not set it below 100k
compact=$((CONTEXT * 85 / 100)); [ "$compact" -ge 100000 ] || compact=100000

echo "Claude Code -> ${GATEWAY%/}, model $MODEL ($CONTEXT tokens of context)"
# An API key from elsewhere would be sent instead of the gateway's.
# CLAUDE_CONFIG_DIR: a configuration folder of its own. Signed in to claude.ai in the usual one,
# Claude Code sends that login's token in place of ANTHROPIC_AUTH_TOKEN - to the gateway, which rightly
# refuses it. Here there is no login to prefer, and the usual sign-in is never touched.
# The aliases, subagents and background jobs all use the same model: a second one would make the
# gateway swap models on the GPU in the middle of a task.
exec env -u ANTHROPIC_API_KEY \
    CLAUDE_CONFIG_DIR="$HOME/.claude-gateway" \
    ANTHROPIC_BASE_URL="${GATEWAY%/}" \
    ANTHROPIC_AUTH_TOKEN="$key" \
    ANTHROPIC_MODEL="$MODEL" \
    ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL" \
    ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL" \
    ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL" \
    CLAUDE_CODE_SUBAGENT_MODEL="$MODEL" \
    CLAUDE_CODE_MAX_CONTEXT_TOKENS="$CONTEXT" \
    CLAUDE_CODE_AUTO_COMPACT_WINDOW="$compact" \
    CLAUDE_CODE_MAX_OUTPUT_TOKENS="$MAX_OUTPUT" \
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
    CLAUDE_CODE_DISABLE_THINKING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
    claude ${claude_args[@]+"${claude_args[@]}"}
