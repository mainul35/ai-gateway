# Claude Code on the gateway

Claude Code can use this gateway's models through Anthropic's Messages API, which the gateway
answers at `/v1/messages` (code: `app/routers/anthropic_v1.py`). Anthropic does not support Claude
Code on non-Claude models; this runs it on our own anyway, and open models are weaker at its tool
use than Claude is.

## Use it: `bin/claude-gateway.ps1`

Run it from the project you want to work on, in PowerShell:

```powershell
C:\Users\mainu\Documents\codes\ollama-model-checker\bin\claude-gateway.ps1
C:\Users\mainu\Documents\codes\ollama-model-checker\bin\claude-gateway.ps1 -Model qwen3-coder:30b -Context 32768
C:\Users\mainu\Documents\codes\ollama-model-checker\bin\claude-gateway.ps1 -p "fix the failing test" --permission-mode acceptEdits
```

On macOS and Linux, `bin/claude-gateway.sh` does the same, with `--model`, `--context`, `--gateway`
and `--max-output`. The Dashboard's "Connect an AI agent" card has a one-line install for it
(`~/.local/bin/claude-gateway`, with the gateway's address filled in); the gateway serves both
launchers at `/downloads/claude-gateway.ps1` and `/downloads/claude-gateway.sh`.

`-Model`, `-Context`, `-Gateway` and `-MaxOutput` are the launcher's own; everything else goes to
`claude` as typed. Everything it sets applies to that one `claude` process only - nothing is
written to your usual Claude settings, so other sessions and the desktop app stay on Anthropic.

What it sets, and why:

| Variable | Value | Why |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude-gateway` | A separate config folder. In the usual one you are signed in to claude.ai, and Claude Code 2.1.162 sends that login's token instead of the gateway key. Here there is no login to prefer; gateway sessions also keep their own history and settings. |
| `ANTHROPIC_BASE_URL` | `https://ai-gateway.mainul35.dev` | where requests go |
| `ANTHROPIC_AUTH_TOKEN` | your gateway key | from `AI_GATEWAY_API_KEY` in the environment, else `~/.qwen/.env`, else a prompt |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | `-Model` (default `qwen3-next-80b`) | one model for every role, so a background job never makes the GPU swap models mid-task |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `-Context` (default 131072) | the model's real window; Claude Code assumes 200k for a name it does not know |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | 85% of it, at least 100000 | compact before the window fills |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `-MaxOutput` (default 16384) | |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, `CLAUDE_CODE_DISABLE_THINKING`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | what Anthropic's gateway docs recommend for a backend that is not Claude |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `0` | drops a system-prompt block the model does not need |

The first run in `~/.claude-gateway` asks the usual first-run questions (theme, trusting the folder).

## Choosing a model

Any gateway model that can call tools - `GET /v1/models` lists them with `"tools"` in their
capabilities. Pass its real context size with `-Context`, or Claude Code lets a conversation grow
past it:

| Model | `-Context` | Notes |
|---|---|---|
| `qwen3-next-80b` (default) | 131072 | llama.cpp on the 3090; ~50 s to load the first time |
| `qwen3-coder:30b` | 32768 | Ollama; quicker for small edits |

## On the gateway side

- `claude-` followed by a gateway model's name, with `:` and `.` written as `-`, is that model:
  `claude-gemma4-31b` is `gemma4:31b`. This is how the desktop app, which takes only Claude-looking
  names, can offer more than one model.
- Any other `claude-*` name - Claude Code's own - maps to `anthropic.model.default` in
  `config/config.properties` (else `qwen3-next-80b`), and `claude-haiku-*` - its background jobs -
  to `anthropic.model.small` (else the same model).
- A request with an image for a model without vision gets a 400 naming the models that have it,
  instead of llama.cpp's 500 (which clients retry). `qwen3-next-80b` reads text only.
- Requests for Ollama and llama.cpp models are passed through unchanged; both speak the Messages
  API. Other backends are translated to and from the OpenAI chat API (`app/anthropic_compat.py`).
- Usage shows on the dashboard under the endpoint `anthropic_messages`.

## Without the launcher

In one project only, `.claude/settings.local.json` (never the committed `.claude/settings.json`,
since it holds the key):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://ai-gateway.mainul35.dev",
    "ANTHROPIC_AUTH_TOKEN": "<your gateway key>",
    "ANTHROPIC_MODEL": "qwen3-next-80b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3-next-80b",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "131072",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "CLAUDE_CODE_DISABLE_THINKING": "1"
  }
}
```

While signed in to claude.ai, Claude Code 2.1.162 may still send that login's token instead (see
`CLAUDE_CONFIG_DIR` above); the launcher avoids this, a settings file cannot (`CLAUDE_CONFIG_DIR`
cannot be set from project settings). Run `/status` to see which credential is in use.

The **desktop app** reads a gateway only from its app-wide Developer → Configure Third-Party
Inference setting, which moves every desktop session - Code and Cowork - to the gateway at once.
