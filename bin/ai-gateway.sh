#!/usr/bin/env bash
# ai-gateway: start a coding agent or assistant on this AI gateway's models - the gateway's
# `ollama launch`. macOS and Linux (bin/ai-gateway.ps1 is the same for Windows).
#
#   ai-gateway launch claude --model qwen3-next-80b
#   ai-gateway launch opencode --model qwen3-coder:30b
#   ai-gateway launch qwen --model qwen3-coder:30b
#   ai-gateway launch codex --model qwen3-coder:30b
#   ai-gateway launch hermes --model gemma4:31b
#   ai-gateway launch openclaw --model gemma4:31b
#   ai-gateway launch claude --model qwen3-next-80b -- -p "fix the failing test"   # after --: the app's own
#   ai-gateway models                                                             # what you may use
#
# Options: --model (default: the app's usual one here), --context (tokens; default: what the gateway
# says the model has), --gateway (default below), --config (set the app up, do not start it).
#
# Every app is pointed at the gateway in a place of its own, so the way you use it otherwise is left
# alone: Claude Code gets its own config folder (~/.claude-gateway), Hermes its own home
# (~/.hermes-gateway), OpenCode, Qwen Code and Codex get everything for this one run from the
# environment and their command line. OpenClaw keeps one config, so a provider named "ai-gateway" is
# added to ~/.openclaw/openclaw.json (backed up first) and made the default.
#
# The key comes from AI_GATEWAY_API_KEY, else ~/.ai-gateway/key, else ~/.qwen/.env, else a prompt
# that does not echo (and offers to keep it in ~/.ai-gateway/key, readable by you only).
#
# An app that is not installed is not installed for you: its install command is printed instead.
# Written for the bash 3.2 macOS still ships.
set -euo pipefail

GATEWAY=${AI_GATEWAY_URL:-https://ai-gateway.mainul35.dev}
APPS="claude opencode qwen codex hermes openclaw"

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "ai-gateway: $*" >&2; exit 1; }

# --- arguments ---------------------------------------------------------------------------------
command=${1:-}; [ $# -gt 0 ] && shift
APP="" MODEL="" CONTEXT="" CONFIG_ONLY=0
extra=()
if [ "$command" = launch ]; then
    APP=${1:-}; [ $# -gt 0 ] && shift
fi
while [ $# -gt 0 ]; do
    case "$1" in
        --model)   MODEL=${2:?--model needs a name}; shift 2 ;;
        --context) CONTEXT=${2:?--context needs a number}; shift 2 ;;
        --gateway) GATEWAY=${2:?--gateway needs a URL}; shift 2 ;;
        --config)  CONFIG_ONLY=1; shift ;;
        --)        shift; extra+=("$@"); break ;;
        -h|--help) usage ;;
        *)         extra+=("$1"); shift ;;
    esac
done
GATEWAY=${GATEWAY%/}
case "$command" in
    launch) ;;
    models) ;;
    ""|-h|--help|help) usage ;;
    *) die "unknown command '$command' (launch, models)" ;;
esac
if [ "$command" = launch ]; then
    case " $APPS " in *" $APP "*) ;; *) die "launch which app? one of: $APPS" ;; esac
fi

# --- the key -----------------------------------------------------------------------------------
read_key() {
    local k=${AI_GATEWAY_API_KEY:-}
    [ -n "$k" ] || { [ -r "$HOME/.ai-gateway/key" ] && k=$(tr -d '\r\n' < "$HOME/.ai-gateway/key"); } || true
    if [ -z "$k" ] && [ -r "$HOME/.qwen/.env" ]; then
        k=$(sed -n 's/^[[:space:]]*AI_GATEWAY_API_KEY[[:space:]]*=[[:space:]]*//p' "$HOME/.qwen/.env" \
            | head -n 1 | tr -d '\r' | sed "s/^[\"']//; s/[\"'][[:space:]]*\$//; s/[[:space:]]*\$//")
    fi
    if [ -z "$k" ]; then
        read -r -s -p "AI gateway API key (from the gateway's Dashboard): " k || true; echo >&2
        [ -n "$k" ] || die "no gateway key"
        read -r -p "Keep it in ~/.ai-gateway/key for next time? [y/N] " keep || true
        case "$keep" in [yY]*)
            (umask 077; mkdir -p "$HOME/.ai-gateway"; printf '%s\n' "$k" > "$HOME/.ai-gateway/key") ;;
        esac
    fi
    printf '%s' "$k"
}
KEY=$(read_key)

# --- what the gateway says about the models ------------------------------------------------------
models_json=$(curl -fsS -m 30 -H "Authorization: Bearer $KEY" "$GATEWAY/v1/models") \
    || die "the gateway at $GATEWAY did not answer with a model list (is the key right?)"
# One line, no spaces around the JSON punctuation, so the matching below never depends on formatting
models_json=$(printf '%s' "$models_json" | tr -d '\n\r' | sed 's/"[[:space:]]*:[[:space:]]*/":/g; s/,[[:space:]]*"/,"/g; s/,[[:space:]]*{/,{/g')

model_entry() {   # the one model's object out of /v1/models, flattened; no nested braces in there
    printf '%s' "$models_json" | tr -d '\n' | grep -o "{\"id\":\"$(printf '%s' "$1" | sed 's/[.[\*^$/]/\\&/g')\"[^}]*}" | head -n 1
}

if [ "$command" = models ]; then
    printf '%s' "$models_json" | grep -o '"id":"[^"]*"[^}]*' | grep -v '^"id":"auto"' | while read -r entry; do
        id=$(printf '%s' "$entry" | sed 's/^"id":"\([^"]*\)".*/\1/')
        ctx=$(printf '%s' "$entry" | sed -n 's/.*"context_length":\([0-9]*\).*/\1/p')
        tools=$(printf '%s' "$entry" | grep -q '"tools"' && echo " tools" || true)
        vision=$(printf '%s' "$entry" | grep -q '"vision"' && echo " vision" || true)
        printf '%-44s %8s%s%s\n' "$id" "${ctx:-?}" "$tools" "$vision"
    done
    exit 0
fi

# The app's usual model here, when none is named: a big one that can call tools
[ -n "$MODEL" ] || MODEL=qwen3-next-80b
entry=$(model_entry "$MODEL" || true)     # no match is an answer, not a failure
[ -n "$entry" ] || die "$MODEL is not a model you may use here; 'ai-gateway models' lists them"
printf '%s' "$entry" | grep -q '"tools"' \
    || echo "note: $MODEL is not listed as able to call tools; agents need that to do anything" >&2
VISION=0; printf '%s' "$entry" | grep -q '"vision"' && VISION=1
if [ -z "$CONTEXT" ]; then
    CONTEXT=$(printf '%s' "$entry" | sed -n 's/.*"context_length":\([0-9]*\).*/\1/p')
    CONTEXT=${CONTEXT:-32768}
fi
MAX_OUTPUT=16384; [ "$MAX_OUTPUT" -lt "$CONTEXT" ] || MAX_OUTPUT=$((CONTEXT / 4))

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

need() {   # the app's command, or its install command and out
    command -v "$1" >/dev/null 2>&1 && return 0
    echo "$2 is not installed. Install it with:" >&2
    echo "  $3" >&2
    exit 1
}

run() {
    if [ "$CONFIG_ONLY" = 1 ]; then
        echo "configured; start it with: ai-gateway launch $APP --model $MODEL"
        exit 0
    fi
    echo "$APP -> $GATEWAY, model $MODEL ($CONTEXT tokens of context)" >&2
    exec "$@"
}

# --- the apps ------------------------------------------------------------------------------------
case "$APP" in
claude)
    need claude "Claude Code" "curl -fsSL https://claude.ai/install.sh | bash"
    compact=$((CONTEXT * 85 / 100)); [ "$compact" -ge 100000 ] || compact=100000
    # Its own config folder: signed in to claude.ai in the usual one, Claude Code sends that login's
    # token instead of ANTHROPIC_AUTH_TOKEN. Every role on one model, so the GPU never swaps mid-task.
    export CLAUDE_CONFIG_DIR="$HOME/.claude-gateway" ANTHROPIC_BASE_URL="$GATEWAY" ANTHROPIC_AUTH_TOKEN="$KEY" \
        ANTHROPIC_MODEL="$MODEL" ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL" ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL" \
        ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL" CLAUDE_CODE_SUBAGENT_MODEL="$MODEL" \
        CLAUDE_CODE_MAX_CONTEXT_TOKENS="$CONTEXT" CLAUDE_CODE_AUTO_COMPACT_WINDOW="$compact" \
        CLAUDE_CODE_MAX_OUTPUT_TOKENS="$MAX_OUTPUT" CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
        CLAUDE_CODE_DISABLE_THINKING=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 CLAUDE_CODE_ATTRIBUTION_HEADER=0 \
        CLAUDE_CODE_AUTO_MODE_SERVER=0 DISABLE_ERROR_REPORTING=1 DISABLE_FEEDBACK_COMMAND=1 \
        CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 ENABLE_CLAUDEAI_MCP_SERVERS=false
    unset ANTHROPIC_API_KEY
    run claude ${extra[@]+"${extra[@]}"}
    ;;
opencode)
    need opencode "OpenCode" "curl -fsSL https://opencode.ai/install | bash"
    modalities=""; [ "$VISION" = 1 ] && modalities=',"modalities":{"input":["text","image"],"output":["text"]}'
    m=$(json_str "$MODEL")
    # Inline: nothing in opencode.json changes, and this run alone sees the gateway
    export OPENCODE_CONFIG_CONTENT="{\"\$schema\":\"https://opencode.ai/config.json\",\"provider\":{\"ai-gateway\":{\"npm\":\"@ai-sdk/openai-compatible\",\"name\":\"AI Gateway\",\"options\":{\"baseURL\":\"$(json_str "$GATEWAY")/v1\",\"apiKey\":\"$(json_str "$KEY")\"},\"models\":{\"$m\":{\"name\":\"$m\",\"tool_call\":true,\"limit\":{\"context\":$CONTEXT,\"output\":$MAX_OUTPUT}$modalities}}}},\"model\":\"ai-gateway/$m\"}"
    run opencode ${extra[@]+"${extra[@]}"}
    ;;
qwen)
    need qwen "Qwen Code" "npm install -g @qwen-code/qwen-code@latest"
    export OPENAI_API_KEY="$KEY" OPENAI_BASE_URL="$GATEWAY/v1" OPENAI_MODEL="$MODEL"
    run qwen --auth-type openai --model "$MODEL" ${extra[@]+"${extra[@]}"}
    ;;
codex)
    need codex "Codex CLI" "npm install -g @openai/codex"
    # A provider of its own for this run only (-c), speaking the Responses API the gateway passes on;
    # the key is read from AI_GATEWAY_API_KEY, so OPENAI_API_KEY is left as it is
    export AI_GATEWAY_API_KEY="$KEY"
    run codex -c model_provider='"ai-gateway"' -c model_providers.ai-gateway.name='"AI Gateway"' \
        -c model_providers.ai-gateway.base_url="\"$GATEWAY/v1/\"" -c model_providers.ai-gateway.wire_api='"responses"' \
        -c model_providers.ai-gateway.env_key='"AI_GATEWAY_API_KEY"' -c model_context_window="$CONTEXT" \
        -m "$MODEL" ${extra[@]+"${extra[@]}"}
    ;;
hermes)
    need hermes "Hermes Agent" "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup"
    # A Hermes home of its own, written whole by this launcher: the usual ~/.hermes is not touched
    export HERMES_HOME="$HOME/.hermes-gateway"
    (umask 077; mkdir -p "$HERMES_HOME"; cat > "$HERMES_HOME/config.yaml" <<YAML
# Written by ai-gateway launch hermes; rewritten on every launch. ~/.hermes is your usual Hermes.
model:
  provider: ai-gateway
  default: "$(json_str "$MODEL")"
  base_url: "$(json_str "$GATEWAY")/v1"
  api_key: "$(json_str "$KEY")"
  context_length: $CONTEXT
providers:
  ai-gateway:
    name: AI Gateway
    api: "$(json_str "$GATEWAY")/v1"
    api_key: "$(json_str "$KEY")"
    default_model: "$(json_str "$MODEL")"
    models: ["$(json_str "$MODEL")"]
toolsets: [hermes-cli, web]
YAML
    )
    run hermes ${extra[@]+"${extra[@]}"}
    ;;
openclaw)
    need openclaw "OpenClaw" "npm install -g openclaw@latest"
    command -v node >/dev/null 2>&1 || die "OpenClaw needs Node.js, which is not on PATH"
    config="$HOME/.openclaw/openclaw.json"
    [ -f "$config" ] || die "OpenClaw has not been set up yet: run 'openclaw onboard' once, then this again"
    cp "$config" "$config.bak-ai-gateway"
    # Merged, not replaced: other providers and settings stay. Read back by node, which OpenClaw
    # itself runs on, so there is no hand-rolled JSON editing here.
    AIGW_CONFIG="$config" AIGW_URL="$GATEWAY/v1" AIGW_KEY="$KEY" AIGW_MODEL="$MODEL" AIGW_CONTEXT="$CONTEXT" \
    AIGW_OUTPUT="$MAX_OUTPUT" AIGW_VISION="$VISION" node -e '
        const fs = require("fs"); const e = process.env;
        const c = JSON.parse(fs.readFileSync(e.AIGW_CONFIG, "utf8"));
        c.models = c.models || {}; c.models.providers = c.models.providers || {};
        if (!c.models.mode) c.models.mode = "merge";
        c.models.providers["ai-gateway"] = {baseUrl: e.AIGW_URL, apiKey: e.AIGW_KEY, api: "openai-completions",
          models: [{id: e.AIGW_MODEL, name: e.AIGW_MODEL, input: e.AIGW_VISION === "1" ? ["text", "image"] : ["text"],
                    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0},
                    contextWindow: Number(e.AIGW_CONTEXT), maxTokens: Number(e.AIGW_OUTPUT)}]};
        c.agents = c.agents || {}; c.agents.defaults = c.agents.defaults || {};
        c.agents.defaults.model = Object.assign({}, c.agents.defaults.model, {primary: "ai-gateway/" + e.AIGW_MODEL});
        // An allowlist, where there is one, has to name the model or OpenClaw refuses it
        if (c.agents.defaults.models) c.agents.defaults.models["ai-gateway/" + e.AIGW_MODEL] = c.agents.defaults.models["ai-gateway/" + e.AIGW_MODEL] || {};
        fs.writeFileSync(e.AIGW_CONFIG, JSON.stringify(c, null, 2), {mode: 0o600});
    '
    if [ "$CONFIG_ONLY" = 1 ]; then run true; fi
    # The gateway daemon reads its config when it starts: restart it, or start it
    openclaw daemon restart >/dev/null 2>&1 || openclaw daemon start >/dev/null 2>&1 \
        || echo "note: could not restart the OpenClaw gateway; run 'openclaw gateway run' in another terminal" >&2
    if [ ${#extra[@]} -gt 0 ]; then run openclaw "${extra[@]}"; else run openclaw tui; fi
    ;;
esac
