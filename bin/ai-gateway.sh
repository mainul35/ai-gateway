#!/usr/bin/env bash
# ai-gateway: start a coding agent or assistant on this AI gateway's models - the gateway's
# `ollama launch`. macOS and Linux (bin/ai-gateway.ps1 is the same for Windows).
#
#   ai-gateway launch claude --model qwen3.6-256k
#   ai-gateway launch opencode --model qwen3.6-256k
#   ai-gateway launch qwen --model qwen3.6-256k
#   ai-gateway launch codex --model qwen3.6-256k
#   ai-gateway launch hermes --model gemma4:31b
#   ai-gateway launch openclaw --model gemma4:31b
#   ai-gateway launch claude --model qwen3.6-256k -- -p "fix the failing test"   # after --: the app's own
#   ai-gateway launch claude-desktop            # the Claude desktop app (macOS, Linux), all its sessions
#   ai-gateway launch claude-desktop --restore  # back to your Claude account
#   ai-gateway models                                                             # what you may use
#   ai-gateway update                                                             # the newest launcher
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
APPS="claude claude-desktop opencode qwen codex hermes openclaw"

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "ai-gateway: $*" >&2; exit 1; }

# --- arguments ---------------------------------------------------------------------------------
command=${1:-}; [ $# -gt 0 ] && shift
APP="" MODEL="" CONTEXT="" CONFIG_ONLY=0 RESTORE=0
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
        --restore) RESTORE=1; shift ;;
        --)        shift; extra+=("$@"); break ;;
        -h|--help) usage ;;
        *)         extra+=("$1"); shift ;;
    esac
done
GATEWAY=${GATEWAY%/}
case "$command" in
    launch) ;;
    models|update) ;;
    ""|-h|--help|help) usage ;;
    *) die "unknown command '$command' (launch, models, update)" ;;
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

# The gateway's machine sleeps when idle. A request with the key wakes it, and is answered 503
# ("waking") until it is up, a minute or two: asked again until then. Into the file $2.
gateway_get() {
    local try=1 code
    while :; do
        code=$(curl -sS -m 60 -o "$2" -w '%{http_code}' -H "Authorization: Bearer $KEY" "$GATEWAY$1") || code=000
        [ "$code" = 200 ] && return 0
        [ "$code" = 503 ] && [ $try -lt 15 ] || return 1
        [ $try = 1 ] && echo "The gateway's machine is waking up; this takes a minute or two..." >&2
        try=$((try + 1)); sleep 10
    done
}

# A JSON body $2 sent to the gateway's $1 with PUT, waiting out a wake the same way; the answer on stdout
gateway_put() {
    local try=1 code out
    out=$(mktemp)
    while :; do
        code=$(curl -sS -m 60 -o "$out" -w '%{http_code}' -X PUT -H "Authorization: Bearer $KEY"                -H 'Content-Type: application/json' --data "$2" "$GATEWAY$1") || code=000
        [ "$code" = 503 ] && [ $try -lt 15 ] || break
        [ $try = 1 ] && echo "The gateway's machine is waking up; this takes a minute or two..." >&2
        try=$((try + 1)); sleep 10
    done
    cat "$out"; rm -f "$out"
    [ "$code" = 200 ]
}

# --- a newer launcher, from the gateway ----------------------------------------------------------
# Downloaded with the key, so a sleeping gateway wakes for it; checked before it replaces this file
if [ "$command" = update ]; then
    target=${BASH_SOURCE[0]}; new="$target.new"
    gateway_get /downloads/ai-gateway.sh "$new" || { rm -f "$new"; die "could not download the launcher from $GATEWAY"; }
    if ! bash -n "$new" 2>/dev/null || ! grep -q '^read_key()' "$new"; then
        rm -f "$new"; die "what the gateway sent is not the launcher; yours is unchanged"
    fi
    if cmp -s "$new" "$target"; then rm -f "$new"; echo "ai-gateway is up to date."; exit 0; fi
    cat "$new" > "$target" && rm -f "$new"      # written into, so the file keeps its permissions
    echo "ai-gateway is updated ($target)."
    exit 0
fi

# --- what the gateway says about the models ------------------------------------------------------
models_file=$(mktemp)
gateway_get /v1/models "$models_file" \
    || { rm -f "$models_file"; die "the gateway at $GATEWAY did not answer with a model list (is the key right?)"; }
models_json=$(cat "$models_file"); rm -f "$models_file"
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

# --- the Claude desktop app ----------------------------------------------------------------------
# A third-party inference profile of its own, written where the app keeps them - as Ollama's
# `ollama launch claude-desktop` does - and made the one in use. The app then asks the gateway for its
# models, and the gateway offers Claude's IDs standing for its own (app/claude_desktop.py). Every
# desktop session moves to the gateway, Code and Cowork alike, until --restore.
if [ "$APP" = claude-desktop ] && [ "$(uname -s)" != Darwin ]; then
    [ "$(uname -s)" = Linux ] || die "the Claude desktop app is set up from here on macOS and Linux; on Windows use ai-gateway.ps1"
    # Where the app keeps its settings on Linux: the XDG config folder, as Electron apps do, or the
    # folder a Flatpak or a Snap gives it - the first that has the app's own folder in it
    support=""
    for dir in "${XDG_CONFIG_HOME:-$HOME/.config}" "$HOME"/.var/app/*/config "$HOME"/snap/*/current/.config; do
        if [ -d "$dir/Claude" ] || [ -d "$dir/Claude-3p" ]; then support=$dir; break; fi
    done
    [ -n "$support" ] || die "the Claude desktop app's settings were not found (looked in ${XDG_CONFIG_HOME:-$HOME/.config}, Flatpak and Snap folders); open the app once, then run this again"
    command -v python3 >/dev/null || die "python3 is needed to write the Claude desktop app's settings"
    # The same profile as on macOS (below), written by Python rather than by AppleScript
    AIGW_MODE=$([ "$RESTORE" = 1 ] && echo restore || echo apply) AIGW_URL="$GATEWAY" AIGW_KEY="$KEY" \
    AIGW_SUPPORT="$support" python3 - <<'EOF' || die "could not write the Claude desktop app's settings"
import json, os
ID, NAME = "00000000-0000-4000-8000-00000000a16e", "AI Gateway"
base, restore = os.environ["AIGW_SUPPORT"], os.environ["AIGW_MODE"] == "restore"

def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
    os.chmod(path + ".new", 0o600)           # it holds the gateway key
    os.replace(path + ".new", path)

def mode(path, value):
    config = read(path)
    config["deploymentMode"] = value
    write(path, config)

mode(os.path.join(base, "Claude", "claude_desktop_config.json"), "1p" if restore else "3p")
third = os.path.join(base, "Claude-3p")
mode(os.path.join(third, "claude_desktop_config.json"), "1p" if restore else "3p")
meta_path = os.path.join(third, "configLibrary", "_meta.json")
profile_path = os.path.join(third, "configLibrary", ID + ".json")
meta = read(meta_path)
meta["entries"] = [e for e in meta.get("entries") or [] if e and e.get("id") != ID]
if restore:
    if meta.get("appliedId") == ID:
        del meta["appliedId"]
    if os.path.exists(profile_path):
        os.remove(profile_path)
else:
    meta["appliedId"] = ID
    meta["entries"].append({"id": ID, "name": NAME})
    profile = read(profile_path)
    profile.update(inferenceProvider="gateway", inferenceGatewayBaseUrl=os.environ["AIGW_URL"],
                   inferenceGatewayApiKey=os.environ["AIGW_KEY"], inferenceGatewayAuthScheme="bearer",
                   deploymentDisplayName=NAME, chatTabEnabled=True, disableDeploymentModeChooser=False,
                   coworkEgressAllowedHosts=["*"], disableEssentialTelemetry=True,
                   disableNonessentialTelemetry=True, autoModeEnabled=False)
    profile.pop("inferenceModels", None)    # the app asks the gateway for its models instead
    write(profile_path, profile)
write(meta_path, meta)
EOF
    chosen=null; chose=0
    if [ "$RESTORE" != 1 ] && [ -n "$MODEL" ]; then
        chosen=${MODEL//\\/\\\\}; chosen="\"${chosen//\"/\\\"}\""
    fi
    if answer=$(gateway_put /auth/desktop-model "{\"model\":$chosen}"); then
        [ "$chosen" != null ] && chose=1
    else
        echo "The gateway did not take the model choice: $answer" >&2
    fi
    # Not restarted from here: on Linux the app goes by several names, depending on how it was
    # installed, and "claude" is also Claude Code - quitting the wrong one would end someone's session
    if [ "$RESTORE" = 1 ]; then
        echo "The Claude desktop app is set back to your Claude account. Quit it and open it again."
    else
        echo "The Claude desktop app now uses $GATEWAY (profile \"AI Gateway\"), set in $support."
        echo "Quit it and open it again: it reads which profile is in use only when it starts."
        if [ "$chose" = 1 ]; then echo "It runs $MODEL, whichever of Claude's names it shows (Sonnet 4.6 stays Auto)."
        else echo "Its model picker offers Claude's names standing for gateway models; --model <name> makes them all one model."; fi
        echo "ai-gateway launch claude-desktop --restore undoes this."
    fi
    exit 0
fi

if [ "$APP" = claude-desktop ]; then
    support="$HOME/Library/Application Support"
    [ -d "$support/Claude" ] || [ -d "$support/Claude-3p" ] || die "the Claude desktop app has not been opened on this Mac yet; open it once, then run this again"
    AIGW_MODE=$([ "$RESTORE" = 1 ] && echo restore || echo apply) AIGW_URL="$GATEWAY" AIGW_KEY="$KEY" \
    AIGW_SUPPORT="$support" osascript -l JavaScript -e '
        ObjC.import("Foundation");
        const env = $.NSProcessInfo.processInfo.environment;
        const get = k => ObjC.unwrap(env.objectForKey(k));
        const ID = "00000000-0000-4000-8000-00000000a16e", NAME = "AI Gateway";
        const fm = $.NSFileManager.defaultManager;
        const read = p => { const s = $.NSString.stringWithContentsOfFileEncodingError(p, 4, null);
                            return s.isNil() ? {} : JSON.parse(ObjC.unwrap(s) || "{}"); };
        const write = (p, o) => { fm.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(
                                      $(p).stringByDeletingLastPathComponent, true, $(), null);
                                  $(JSON.stringify(o, null, 2)).writeToFileAtomicallyEncodingError(p, true, 4, null); };
        const base = get("AIGW_SUPPORT"), restore = get("AIGW_MODE") === "restore";
        const mode = (p, m) => { const c = read(p); c.deploymentMode = m; write(p, c); };
        mode(base + "/Claude/claude_desktop_config.json", restore ? "1p" : "3p");
        const third = base + "/Claude-3p";
        mode(third + "/claude_desktop_config.json", restore ? "1p" : "3p");
        const metaPath = third + "/configLibrary/_meta.json", profilePath = third + "/configLibrary/" + ID + ".json";
        const meta = read(metaPath);
        meta.entries = (meta.entries || []).filter(e => !e || e.id !== ID);
        if (restore) {
            if (meta.appliedId === ID) delete meta.appliedId;
            fm.removeItemAtPathError(profilePath, null);
        } else {
            meta.appliedId = ID; meta.entries.push({id: ID, name: NAME});
            const p = read(profilePath);
            Object.assign(p, {inferenceProvider: "gateway", inferenceGatewayBaseUrl: get("AIGW_URL"),
                inferenceGatewayApiKey: get("AIGW_KEY"), inferenceGatewayAuthScheme: "bearer",
                deploymentDisplayName: NAME, chatTabEnabled: true, disableDeploymentModeChooser: false,
                coworkEgressAllowedHosts: ["*"], disableEssentialTelemetry: true,
                disableNonessentialTelemetry: true, autoModeEnabled: false});
            delete p.inferenceModels;      // the app asks the gateway for its models instead
            write(profilePath, p);
        }
        write(metaPath, meta);
        "ok";' >/dev/null || die "could not write the Claude desktop app's settings"
    # The app picks among Claude's names, and the gateway decides what each stands for: given --model,
    # every name the app starts on stands for it, for this key's person (app/claude_desktop.py).
    # Without one - and on --restore - that choice is cleared and the names go back to the defaults.
    chosen=null; chose=0
    if [ "$RESTORE" != 1 ] && [ -n "$MODEL" ]; then
        chosen=${MODEL//\\/\\\\}; chosen="\"${chosen//\"/\\\"}\""
    fi
    if answer=$(gateway_put /auth/desktop-model "{\"model\":$chosen}"); then
        [ "$chosen" != null ] && chose=1
    else
        echo "The gateway did not take the model choice: $answer" >&2
    fi
    # The app reads which profile is in use only when it starts: so it is quit and opened again, by a
    # process of its own - typed in a terminal inside the app, this command closes with the app
    then=" Quit it and open it again."
    if open -Ra Claude 2>/dev/null; then
        nohup sh -c 'osascript -e "quit app \"Claude\"" >/dev/null 2>&1
                     i=0; while pgrep -xq Claude && [ $i -lt 60 ]; do sleep 0.25; i=$((i + 1)); done
                     open -a Claude' >/dev/null 2>&1 &
        then=" It is restarting."
    fi
    if [ "$RESTORE" = 1 ]; then
        echo "The Claude desktop app is set back to your Claude account.$then"
    else
        echo "The Claude desktop app now uses $GATEWAY (profile \"AI Gateway\").$then"
        if [ "$chose" = 1 ]; then echo "It runs $MODEL, whichever of Claude's names it shows (Sonnet 4.6 stays Auto)."
        else echo "Its model picker offers Claude's names standing for gateway models; --model <name> makes them all one model."; fi
        echo "ai-gateway launch claude-desktop --restore undoes this."
    fi
    exit 0
fi

# The app's usual model here, when none is named: a big one that can call tools
[ -n "$MODEL" ] || MODEL=qwen3.6-256k
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

# The project this session works in, for the gateway's project tools (its MCP server): owner/repo from
# the git remote, else the folder's name. Agents get the person's other projects to search and read,
# and the notes earlier sessions left about how they connect.
workspace() {
    url=$(git remote get-url origin 2>/dev/null || true)
    case "$url" in
        *github.com[:/]*) printf '%s' "$url" | sed -E 's#^.*github\.com[:/]+##; s#\.git$##' ;;
        *) basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" ;;
    esac | tr -c 'A-Za-z0-9._/-' '_'
}
WORKSPACE=$(workspace)
MCP_URL="$GATEWAY/v1/mcp"

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
    # The project tools, for this run: one file per workspace (it holds the key, so only you may read
    # it), and the tools allowed without asking - they read your own projects and notes. Given after
    # the app's own arguments, since --mcp-config takes every word that follows it.
    slug=$(printf '%s' "$WORKSPACE" | tr '/' '_')
    mcp="$CLAUDE_CONFIG_DIR/ai-gateway-mcp-$slug.json"; allow="$CLAUDE_CONFIG_DIR/ai-gateway-allow.json"
    (umask 077; mkdir -p "$CLAUDE_CONFIG_DIR"
     printf '{"mcpServers":{"ai-gateway":{"type":"http","url":"%s","headers":{"Authorization":"Bearer %s","X-Workspace":"%s"}}}}\n' \
         "$(json_str "$MCP_URL")" "$(json_str "$KEY")" "$(json_str "$WORKSPACE")" > "$mcp"
     printf '{"permissions":{"allow":["mcp__ai-gateway"]}}\n' > "$allow")
    run claude ${extra[@]+"${extra[@]}"} --settings "$allow" --mcp-config "$mcp"
    ;;
opencode)
    need opencode "OpenCode" "curl -fsSL https://opencode.ai/install | bash"
    modalities=""; [ "$VISION" = 1 ] && modalities=',"modalities":{"input":["text","image"],"output":["text"]}'
    m=$(json_str "$MODEL")
    # Inline: nothing in opencode.json changes, and this run alone sees the gateway
    export OPENCODE_CONFIG_CONTENT="{\"\$schema\":\"https://opencode.ai/config.json\",\"provider\":{\"ai-gateway\":{\"npm\":\"@ai-sdk/openai-compatible\",\"name\":\"AI Gateway\",\"options\":{\"baseURL\":\"$(json_str "$GATEWAY")/v1\",\"apiKey\":\"$(json_str "$KEY")\"},\"models\":{\"$m\":{\"name\":\"$m\",\"tool_call\":true,\"limit\":{\"context\":$CONTEXT,\"output\":$MAX_OUTPUT}$modalities}}}},\"model\":\"ai-gateway/$m\",\"mcp\":{\"ai-gateway\":{\"type\":\"remote\",\"enabled\":true,\"url\":\"$(json_str "$MCP_URL")\",\"headers\":{\"Authorization\":\"Bearer $(json_str "$KEY")\",\"X-Workspace\":\"$(json_str "$WORKSPACE")\"}}}}"
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
        -c mcp_servers.ai-gateway.url="\"$MCP_URL\"" -c mcp_servers.ai-gateway.bearer_token_env_var='"AI_GATEWAY_API_KEY"' \
        -c "mcp_servers.ai-gateway.http_headers={\"X-Workspace\"=\"$WORKSPACE\"}" \
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
