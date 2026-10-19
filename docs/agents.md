# Agents and assistants on the gateway: `ai-gateway launch`

The gateway's `ollama launch`: one command starts a coding agent or an assistant on one of the
gateway's models, pointed at the gateway with your key.

```bash
ai-gateway launch claude   --model qwen3-next-80b
ai-gateway launch opencode --model qwen3-coder:30b
ai-gateway launch codex    --model qwen3-coder:30b
ai-gateway launch qwen     --model qwen3-coder:30b
ai-gateway launch hermes   --model gemma4:31b
ai-gateway launch openclaw --model gemma4:31b
ai-gateway models          # what you may use, with context sizes
```

On Windows the same with `-Model`, `-Context`, `-Config`. The Dashboard's **Connect an AI agent →
Applications** lists every command for a chosen model and the one-time install for your computer.
The launchers are `bin/ai-gateway.sh` (macOS, Linux; bash 3.2 and up) and `bin/ai-gateway.ps1`
(PowerShell 7), served at `/downloads/` with the gateway's own address filled in.

## What it does to each app

Built from what `ollama launch` itself does (Ollama's `cmd/launch/*.go`), with the gateway's address
and a real key in place of Ollama's. Each app is pointed at the gateway somewhere of its own, so the
way it is used otherwise does not change:

| App | How | Where |
|---|---|---|
| Claude Code | Messages API: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, every model role on the one model, context and compaction from the model's real size | its own config folder, `~/.claude-gateway` (signed in to claude.ai, the usual one sends that login instead of the key) |
| OpenCode | an `ai-gateway` provider (`@ai-sdk/openai-compatible`, `/v1`), with context, output and image limits | `OPENCODE_CONFIG_CONTENT`, this run only |
| Codex CLI | an `ai-gateway` model provider on the Responses API (`/v1/responses`), key from `AI_GATEWAY_API_KEY` | `-c` overrides, this run only |
| Qwen Code | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `--auth-type openai` | environment, this run only |
| Hermes Agent | `model.provider: ai-gateway`, `base_url`, `api_key`, `context_length` | its own home, `~/.hermes-gateway` (`HERMES_HOME`), rewritten each launch |
| OpenClaw | an `ai-gateway` provider (`openai-completions`, `/v1`) made the default model, and added to the allowlist if there is one; then the OpenClaw gateway is restarted and its TUI opened | merged into `~/.openclaw/openclaw.json`, backed up to `openclaw.json.bak-ai-gateway` first. Needs `openclaw onboard` once beforehand |

The model's context size comes from `/v1/models` (`context_length`): for Ollama models it is capped
at `ollama.context.length` (default 65536), which must match `OLLAMA_CONTEXT_LENGTH` in Ollama's
service, since that, not what the model was trained for, is what Ollama serves.

Nothing is installed for you: an app that is missing gets its install command printed.

The key: `AI_GATEWAY_API_KEY`, else `~/.ai-gateway/key`, else `~/.qwen/.env`, else a prompt that does
not echo and offers to keep it in `~/.ai-gateway/key` (readable by you only).

## The Claude desktop app

```bash
ai-gateway launch claude-desktop             # macOS (ai-gateway.ps1 on Windows)
ai-gateway launch claude-desktop --restore   # back to the Claude account
```

As `ollama launch claude-desktop` does: a third-party inference profile named "AI Gateway" is written
where the app keeps them (`Claude-3p/configLibrary/` in `~/Library/Application Support` or
`%LOCALAPPDATA%`), made the one in use (`_meta.json`), and the app switched to it
(`deploymentMode: 3p` in its configs; a Store install's own copy too). No model list is written: the
app asks the gateway, which answers a GET `/v1/models` carrying `anthropic-version` with Claude's own
IDs, each standing for a gateway model and shown by that model's name (`app/claude_desktop.py`):

| Claude ID | Default gateway model |
|---|---|
| claude-sonnet-5 | `anthropic.model.default` (qwen3-next-80b) |
| claude-opus-5 | a model that can see (gemma4:31b) |
| claude-haiku-4-5-20251001 | `anthropic.model.small`, else the default |
| claude-fable-5 | a coder (qwen3-coder:30b) |
| claude-sonnet-4-6 | auto - the gateway picks per request |

Set `anthropic.desktop.models` ("claude-opus-5=gemma4:31b, ...") to choose. A person is only offered
the models they may use. Quit the app (from the tray too) and open it again after either command.
**Every** desktop session moves to the gateway, Code and Cowork alike, until `--restore`.

## On the gateway

- `POST /v1/responses` (`app/routers/responses_v1.py`) passes the Responses API through to Ollama and
  llama.cpp, which both have it, for Codex. Access, the person's limits and the output cap apply as
  everywhere; image URLs in the input are refused (send them inline).
- `/v1/models` carries `context_length` per model.
