<#
.SYNOPSIS
  Runs Claude Code against this AI gateway, in this terminal only.

.DESCRIPTION
  Everything is set for the one `claude` process this starts: the gateway's address and key, which
  model answers, how big its context is, and the switches Anthropic's gateway docs recommend for a
  backend that is not Claude. Nothing is written to settings.json, so every other Claude Code
  session - and the desktop app - keeps using Anthropic's models.

  The key is taken from, in order: the AI_GATEWAY_API_KEY environment variable, the same name in
  ~/.qwen/.env (where Qwen Code already reads it), or a prompt that does not echo what is typed.

  Anthropic does not support Claude Code on non-Claude models; this runs it on your own anyway.

.EXAMPLE
  .\bin\claude-gateway.ps1
  .\bin\claude-gateway.ps1 -Model qwen3.6-256k -Context 32768
  .\bin\claude-gateway.ps1 -p "summarise this repo" --permission-mode acceptEdits

  -Model, -Context, -Gateway and -MaxOutput are this script's; everything else goes to claude as typed.
#>

# Read by hand rather than with param(): PowerShell would otherwise bind claude's own arguments to
# these by position, or call `-p` ambiguous, and claude would never see them.
$Model = "qwen3.6-256k"
# The model's real context window. Claude Code assumes 200k for a name it does not know, and would let
# a conversation grow past what the model can read.
$Context = 131072
$Gateway = "https://ai-gateway.mainul35.dev"
$MaxOutput = 16384
$ClaudeArgs = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Exact ($args[$i]) {
        "-Model"     { $Model = $args[++$i]; continue }
        "-Context"   { $Context = [int]$args[++$i]; continue }
        "-Gateway"   { $Gateway = $args[++$i]; continue }
        "-MaxOutput" { $MaxOutput = [int]$args[++$i]; continue }
        "--"         { continue }
        default      { $ClaudeArgs += $args[$i] }
    }
}

function Get-GatewayKey {
    if ($env:AI_GATEWAY_API_KEY) { return $env:AI_GATEWAY_API_KEY }
    $file = Join-Path $HOME ".qwen\.env"
    if (Test-Path $file) {
        # Not $Matches: -match inside Where-Object sets it in the filter's own scope, not here
        $line = Get-Content $file | Where-Object { $_ -match '^\s*AI_GATEWAY_API_KEY\s*=' } | Select-Object -First 1
        $value = if ($line) { ($line -replace '^\s*AI_GATEWAY_API_KEY\s*=\s*', '').Trim().Trim('"', "'") } else { "" }
        if ($value) { return $value }
    }
    $secure = Read-Host "AI gateway API key" -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

$key = Get-GatewayKey
if (-not $key) { Write-Error "No gateway key. Create one under Users in the gateway, then run this again."; exit 1 }

# Only for the claude started below: the variables go back to what they were when it exits
$set = @{
    # A configuration folder of its own. Signed in to claude.ai in the usual one, Claude Code sends that
    # login's token in place of ANTHROPIC_AUTH_TOKEN - to the gateway, which rightly refuses it. Here
    # there is no login to prefer, gateway sessions keep their own history and settings, and the
    # claude.ai sign-in every other session uses is never touched.
    CLAUDE_CONFIG_DIR                           = (Join-Path $HOME ".claude-gateway")
    ANTHROPIC_BASE_URL                          = $Gateway.TrimEnd('/')
    ANTHROPIC_AUTH_TOKEN                        = $key
    ANTHROPIC_MODEL                             = $Model
    # The aliases, subagents and background jobs (session titles) all use the same model: a second
    # one would make the gateway swap models on the GPU in the middle of a task
    ANTHROPIC_DEFAULT_OPUS_MODEL                = $Model
    ANTHROPIC_DEFAULT_SONNET_MODEL              = $Model
    ANTHROPIC_DEFAULT_HAIKU_MODEL               = $Model
    CLAUDE_CODE_SUBAGENT_MODEL                  = $Model
    CLAUDE_CODE_MAX_CONTEXT_TOKENS              = "$Context"
    # Compaction starts before the window is full; Claude Code will not set it below 100k
    CLAUDE_CODE_AUTO_COMPACT_WINDOW             = "$([Math]::Max(100000, [int]($Context * 0.85)))"
    CLAUDE_CODE_MAX_OUTPUT_TOKENS               = "$MaxOutput"
    # What Anthropic's gateway docs recommend for a backend that is not Claude
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS      = "1"
    CLAUDE_CODE_DISABLE_THINKING                = "1"
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC    = "1"
    CLAUDE_CODE_ATTRIBUTION_HEADER              = "0"
}
$saved = @{}
foreach ($name in $set.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, $set[$name], "Process")
}
# An API key from elsewhere would be sent instead of the gateway's
$savedApiKey = $env:ANTHROPIC_API_KEY
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

Write-Host "Claude Code -> $($set.ANTHROPIC_BASE_URL), model $Model ($Context tokens of context)" -ForegroundColor Cyan
try {
    & claude @ClaudeArgs
    $code = $LASTEXITCODE
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
    if ($savedApiKey) { $env:ANTHROPIC_API_KEY = $savedApiKey }
}
exit $code
