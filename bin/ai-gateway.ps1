<#
.SYNOPSIS
  ai-gateway: start a coding agent or assistant on this AI gateway's models - the gateway's
  `ollama launch`. Windows (bin/ai-gateway.sh is the same for macOS and Linux).

.DESCRIPTION
  ai-gateway launch claude -Model qwen3.6-256k
  ai-gateway launch opencode -Model qwen3.6-256k
  ai-gateway launch qwen -Model qwen3.6-256k
  ai-gateway launch codex -Model qwen3.6-256k
  ai-gateway launch hermes -Model gemma4:31b
  ai-gateway launch openclaw -Model gemma4:31b
  ai-gateway launch claude -Model qwen3.6-256k -- -p "fix the failing test"    # after --: the app's own
  ai-gateway launch claude-desktop             # the Claude desktop app, all its sessions
  ai-gateway launch claude-desktop -Restore    # back to your Claude account
  ai-gateway sessions                       # this computer's Claude Code sessions, in the playground
  ai-gateway sessions --allow-commands      # ...and messages sent to them there may run commands too
  ai-gateway models                                                             # what you may use
  ai-gateway update                                                             # the newest launcher

  -Model, -Context (tokens; default: what the gateway says), -Gateway, -Config (set it up, do not start).

  Every app is pointed at the gateway in a place of its own, so the way you use it otherwise is left
  alone: Claude Code gets its own config folder (~\.claude-gateway), Hermes its own home
  (~\.hermes-gateway); OpenCode, Qwen Code and Codex get everything for this one run from the
  environment and their command line. OpenClaw keeps one config, so a provider named "ai-gateway" is
  added to ~\.openclaw\openclaw.json (backed up first) and made the default.

  The key comes from AI_GATEWAY_API_KEY, else ~\.ai-gateway\key, else ~\.qwen\.env, else a prompt
  that does not echo (and offers to keep it in ~\.ai-gateway\key). An app that is not installed is
  not installed for you: its install command is printed instead.
#>

# Read by hand rather than with param(): PowerShell would otherwise bind the app's own arguments to
# these by position, or call `-p` ambiguous, and the app would never see them.
$Gateway = if ($env:AI_GATEWAY_URL) { $env:AI_GATEWAY_URL } else { "https://ai-gateway.mainul35.dev" }
$Command = if ($args.Count) { $args[0] } else { "" }
$App = ""; $Model = ""; $Context = 0; $ConfigOnly = $false; $Restore = $false; $Extra = @()
$i = 1
if ($Command -eq "launch" -and $args.Count -gt 1) { $App = $args[1]; $i = 2 }
for (; $i -lt $args.Count; $i++) {
    switch -Exact ($args[$i]) {
        "-Model"   { $Model = $args[++$i]; continue }
        "-Context" { $Context = [int]$args[++$i]; continue }
        "-Gateway" { $Gateway = $args[++$i]; continue }
        "-Config"  { $ConfigOnly = $true; continue }
        "-Restore" { $Restore = $true; continue }
        "--"       { $Extra += $args[($i + 1)..($args.Count - 1)] | Where-Object { $_ -ne $null }; $i = $args.Count; continue }
        default    { $Extra += $args[$i] }
    }
}
$Gateway = $Gateway.TrimEnd('/')
$Apps = @("claude", "claude-desktop", "opencode", "qwen", "codex", "hermes", "openclaw")
function Fail($message) { Write-Host "ai-gateway: $message" -ForegroundColor Red; exit 1 }

# Windows PowerShell 5.1, the one every Windows has, lacks ConvertFrom-Json -AsHashtable and writes
# "utf8" with a byte-order mark, which Node apps (OpenClaw) refuse to parse. These work in 5.1 and 7.
function ConvertTo-Hashtable($value) {
    if ($value -is [System.Management.Automation.PSCustomObject]) {
        $table = [ordered]@{}
        foreach ($p in $value.PSObject.Properties) { $table[$p.Name] = ConvertTo-Hashtable $p.Value }
        return $table
    }
    if ($value -is [System.Collections.IList]) { return ,@($value | ForEach-Object { ConvertTo-Hashtable $_ }) }
    return $value
}
function ConvertFrom-JsonTable($text) { return ConvertTo-Hashtable ($text | ConvertFrom-Json) }
function Write-Utf8($path, $text) {
    [System.IO.File]::WriteAllText($path, ($text -join "`n") + "`n", (New-Object System.Text.UTF8Encoding $false))
}
if ($Command -notin @("launch", "models", "update", "sessions")) {
    Get-Help $MyInvocation.MyCommand.Path -Detailed | Out-String | Write-Host; exit 0
}

# --- the key -----------------------------------------------------------------------------------
function Get-GatewayKey {
    if ($env:AI_GATEWAY_API_KEY) { return $env:AI_GATEWAY_API_KEY }
    $saved = Join-Path $HOME ".ai-gateway\key"
    if (Test-Path $saved) { $k = (Get-Content $saved -Raw).Trim(); if ($k) { return $k } }
    $file = Join-Path $HOME ".qwen\.env"
    if (Test-Path $file) {
        $line = Get-Content $file | Where-Object { $_ -match '^\s*AI_GATEWAY_API_KEY\s*=' } | Select-Object -First 1
        $value = if ($line) { ($line -replace '^\s*AI_GATEWAY_API_KEY\s*=\s*', '').Trim().Trim('"', "'") } else { "" }
        if ($value) { return $value }
    }
    $secure = Read-Host "AI gateway API key (from the gateway's Dashboard)" -AsSecureString
    $k = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $k) { Fail "no gateway key" }
    if ((Read-Host "Keep it in ~\.ai-gateway\key for next time? [y/N]") -match '^[yY]') {
        New-Item -ItemType Directory -Force (Split-Path $saved) | Out-Null
        Set-Content -Path $saved -Value $k -NoNewline
    }
    return $k
}
$Key = Get-GatewayKey

# The gateway's machine sleeps when idle. A request with the key wakes it, and is answered 503
# ("waking") until it is up, a minute or two: asked again until then. With $put, that is sent as JSON.
function Invoke-Gateway($path, $outFile, $put) {
    for ($try = 1; ; $try++) {
        try {
            $headers = @{ Authorization = "Bearer $Key" }
            if ($put) {
                return Invoke-RestMethod -Method Put -Uri "$Gateway$path" -Headers $headers -TimeoutSec 60 `
                    -ContentType "application/json" -Body ($put | ConvertTo-Json -Compress)
            }
            if ($outFile) { Invoke-WebRequest -Uri "$Gateway$path" -Headers $headers -TimeoutSec 60 -UseBasicParsing -OutFile $outFile; return }
            return Invoke-RestMethod -Uri "$Gateway$path" -Headers $headers -TimeoutSec 60
        } catch {
            $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            if ($status -ne 503 -or $try -ge 15) { throw }
            if ($try -eq 1) { Write-Host "The gateway's machine is waking up; this takes a minute or two..." }
            Start-Sleep -Seconds 10
        }
    }
}

# --- a newer launcher, from the gateway ----------------------------------------------------------
# Downloaded with the key, so a sleeping gateway wakes for it; checked before it replaces this file
if ($Command -eq "update") {
    $target = $PSCommandPath; $new = "$target.new"
    try { Invoke-Gateway "/downloads/ai-gateway.ps1" $new }
    catch { Remove-Item $new -ErrorAction SilentlyContinue; Fail "could not download the launcher from ${Gateway}: $($_.Exception.Message)" }
    $tokens = $null; $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($new, [ref]$tokens, [ref]$errors)
    if ($errors.Count -or -not (Select-String -Path $new -Pattern "function Get-GatewayKey" -Quiet)) {
        Remove-Item $new; Fail "what the gateway sent is not the launcher; yours is unchanged"
    }
    if ((Get-FileHash $new).Hash -eq (Get-FileHash $target).Hash) { Remove-Item $new; Write-Host "ai-gateway is up to date."; exit 0 }
    Move-Item $new $target -Force
    Write-Host "ai-gateway is updated ($target)." -ForegroundColor Cyan
    exit 0
}

# --- this computer's Claude Code sessions, in the playground ---------------------------------------
# The sessions agent (served by the gateway: app/static/claude-sessions.py) reports the sessions Claude
# Code keeps for this launcher (~\.claude-gateway) to the playground's Code mode, and runs a message sent
# to one of them there, in its folder. It stays in the foreground: Ctrl+C stops it. Fetched anew each
# time, so it is the gateway's own version; checked to be Python before it is run.
if ($Command -eq "sessions") {
    $python = Get-Command python, py -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $python) { Fail "Python is needed for the sessions agent (https://www.python.org/downloads/)" }
    $folder = Join-Path $HOME ".ai-gateway"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $agent = Join-Path $folder "claude-sessions.py"; $new = "$agent.new"
    try { Invoke-Gateway "/static/claude-sessions.py" $new }
    catch { Remove-Item $new -ErrorAction SilentlyContinue; Fail "could not download the sessions agent from ${Gateway}: $($_.Exception.Message)" }
    & $python.Source -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" $new 2>$null
    if ($LASTEXITCODE -ne 0 -or -not (Select-String -Path $new -Pattern "def read_session" -Quiet)) {
        Remove-Item $new; Fail "what the gateway sent is not the sessions agent"
    }
    Move-Item $new $agent -Force
    $env:GATEWAY_KEY = $Key
    & $python.Source $agent --gateway $Gateway @Extra
    exit $LASTEXITCODE
}

# --- what the gateway says about the models ------------------------------------------------------
try {
    $Models = (Invoke-Gateway "/v1/models").data
} catch { Fail "the gateway at $Gateway did not answer with a model list (is the key right?)" }

if ($Command -eq "models") {
    foreach ($m in ($Models | Where-Object { $_.id -ne "auto" })) {
        $caps = @($m.capabilities) | Where-Object { $_ -in @("tools", "vision") }
        "{0,-44} {1,8} {2}" -f $m.id, ($(if ($m.context_length) { $m.context_length } else { "?" })), ($caps -join " ")
    }
    exit 0
}
if ($App -notin $Apps) { Fail "launch which app? one of: $($Apps -join ', ')" }
# --- the Claude desktop app ----------------------------------------------------------------------
# A third-party inference profile of its own, written where the app keeps them - as Ollama's
# `ollama launch claude-desktop` does - and made the one in use. The app then asks the gateway for its
# models, and the gateway offers Claude's IDs standing for its own (app/claude_desktop.py). Every
# desktop session moves to the gateway, Code and Cowork alike, until -Restore.
if ($App -eq "claude-desktop") {
    $local = $env:LOCALAPPDATA
    $id = "00000000-0000-4000-8000-00000000a16e"; $name = "AI Gateway"
    function Read-Json($path) { if (Test-Path $path) { $t = Get-Content $path -Raw; if ($t.Trim()) { return (ConvertFrom-JsonTable $t) } }; return @{} }
    function Write-Json($path, $value) {
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        if (Test-Path $path) { Copy-Item $path "$path.bak-ai-gateway" -Force }
        Write-Utf8 $path ($value | ConvertTo-Json -Depth 20)
    }
    # The app reads which profile is in use only when it starts: so it is closed and opened again.
    # Only the desktop app's own processes, known by where they are installed - Claude Code is a
    # claude.exe too. The restart runs as a process of its own, started through WMI and so outside
    # this shell's job: typed in a terminal inside the app, this command closes with the app, and the
    # restart still finishes. False when the app is not installed.
    function Restart-ClaudeDesktop {
        $app = Get-StartApps | Where-Object { $_.Name -eq "Claude" } | Select-Object -First 1
        if (-not $app) { return $false }
        $mine = '\\WindowsApps\\Claude_|\\AnthropicClaude\\'
        $find = "Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { `$_.Path -match '$mine' }"
        $script = "$find | Stop-Process -Force -ErrorAction SilentlyContinue; " +
                  "for (`$i = 0; `$i -lt 60 -and ($find); `$i++) { Start-Sleep -Milliseconds 250 }; " +
                  "Start-Process 'shell:AppsFolder\$($app.AppID)'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
        $started = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded" }
        return $started.ReturnValue -eq 0
    }
    # The app picks among Claude's names, and the gateway decides what each stands for: given -Model,
    # every name the app starts on stands for it, for this key's person (app/claude_desktop.py).
    # Without one - and on -Restore - that choice is cleared and the names go back to the defaults.
    function Set-DesktopModel($model) {
        try { $null = Invoke-Gateway "/auth/desktop-model" $null @{ model = $(if ($model) { $model } else { $null }) }; return $true }
        catch {
            $why = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
            Write-Host "The gateway did not take the model choice: $why" -ForegroundColor Yellow; return $false
        }
    }
    $mode = if ($Restore) { "1p" } else { "3p" }
    # Where the app reads which profile is in use: its plain folders, and a Store install's own copy
    $configs = @("$local\Claude-3p\claude_desktop_config.json", "$local\Claude\claude_desktop_config.json") +
        @(Get-ChildItem "$local\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json" -ErrorAction SilentlyContinue |
          ForEach-Object FullName) + @(if (Test-Path "$local\Claude Nest") { "$local\Claude Nest\claude_desktop_config.json" })
    foreach ($path in $configs | Select-Object -Unique) { $c = Read-Json $path; $c.deploymentMode = $mode; Write-Json $path $c }
    $metaPath = "$local\Claude-3p\configLibrary\_meta.json"; $profilePath = "$local\Claude-3p\configLibrary\$id.json"
    $meta = Read-Json $metaPath
    $meta.entries = @(@($meta.entries) | Where-Object { $_ -and $_.id -ne $id })
    if ($Restore) {
        $null = Set-DesktopModel $null
        if ($meta.appliedId -eq $id) { $meta.Remove("appliedId") }
        Remove-Item $profilePath -ErrorAction SilentlyContinue
        Write-Json $metaPath $meta
        if (Restart-ClaudeDesktop) { Write-Host "The Claude desktop app is set back to your Claude account, and is restarting." }
        else { Write-Host "The Claude desktop app is set back to your Claude account. Quit it (also from the tray) and open it again." }
        exit 0
    }
    $meta.appliedId = $id; $meta.entries += @{ id = $id; name = $name }
    $p = Read-Json $profilePath
    $p.inferenceProvider = "gateway"; $p.inferenceGatewayBaseUrl = $Gateway; $p.inferenceGatewayApiKey = $Key
    $p.inferenceGatewayAuthScheme = "bearer"; $p.deploymentDisplayName = $name; $p.chatTabEnabled = $true
    $p.disableDeploymentModeChooser = $false; $p.coworkEgressAllowedHosts = @("*"); $p.disableEssentialTelemetry = $true
    $p.disableNonessentialTelemetry = $true; $p.autoModeEnabled = $false
    $p.Remove("inferenceModels")      # the app asks the gateway for its models instead
    Write-Json $profilePath $p
    Write-Json $metaPath $meta
    $chose = (Set-DesktopModel $Model) -and $Model
    if (Restart-ClaudeDesktop) { Write-Host "The Claude desktop app now uses $Gateway (profile `"AI Gateway`"), and is restarting." -ForegroundColor Cyan }
    else { Write-Host "The Claude desktop app now uses $Gateway (profile `"AI Gateway`"): quit it (also from the tray) and open it again." -ForegroundColor Cyan }
    if ($chose) { Write-Host "It runs $Model, whichever of Claude's names it shows (Sonnet 4.6 stays Auto)." -ForegroundColor Cyan }
    else { Write-Host "Its model picker offers Claude's names standing for gateway models; -Model <name> makes them all one model." }
    Write-Host "'ai-gateway launch claude-desktop -Restore' undoes this."
    exit 0
}

if (-not $Model) { $Model = "qwen3.6-256k" }
$Entry = $Models | Where-Object { $_.id -eq $Model } | Select-Object -First 1
if (-not $Entry) { Fail "$Model is not a model you may use here; 'ai-gateway models' lists them" }
if ("tools" -notin @($Entry.capabilities)) { Write-Warning "$Model is not listed as able to call tools; agents need that to do anything" }
$Vision = "vision" -in @($Entry.capabilities)
if (-not $Context) { $Context = if ($Entry.context_length) { [int]$Entry.context_length } else { 32768 } }
$MaxOutput = [Math]::Min(16384, [int]($Context / 4))

function Need($cmd, $name, $install) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "$name is not installed. Install it with:`n  $install" -ForegroundColor Yellow
    exit 1
}

# Only for the app started below: the variables go back to what they were when it exits
$script:saved = @{}
function Set-ForRun($vars) {
    foreach ($name in $vars.Keys) {
        if (-not $script:saved.ContainsKey($name)) { $script:saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
        [Environment]::SetEnvironmentVariable($name, $vars[$name], "Process")
    }
}
# Not `exit (Start-App ...)`: that would capture everything the app prints as the function's result,
# and nothing would reach the screen. The exit code is kept in $script:ExitCode instead.
$script:ExitCode = 0
function Start-App($exe, $appArgs) {
    if ($ConfigOnly) { Write-Host "configured; start it with: ai-gateway launch $App -Model $Model"; return }
    Write-Host "$App -> $Gateway, model $Model ($Context tokens of context)" -ForegroundColor Cyan
    try { & $exe @appArgs; $script:ExitCode = $LASTEXITCODE }
    finally { foreach ($name in $script:saved.Keys) { [Environment]::SetEnvironmentVariable($name, $script:saved[$name], "Process") } }
}

# The project this session works in, for the gateway's project tools (its MCP server): owner/repo from
# the git remote, else the folder's name. Agents get the person's other projects to search and read,
# and the notes earlier sessions left about how they connect.
function Get-Workspace {
    $url = ""
    try { $url = (git remote get-url origin 2>$null) } catch {}
    if ($url -match 'github\.com[:/]+(.+?)(\.git)?$') { $name = $Matches[1] }
    else {
        $top = ""
        try { $top = (git rev-parse --show-toplevel 2>$null) } catch {}
        $name = Split-Path -Leaf $(if ($top) { $top } else { (Get-Location).Path })
    }
    return ($name -replace '[^A-Za-z0-9._/-]', '_')
}
$Workspace = Get-Workspace
$McpUrl = "$Gateway/v1/mcp"

switch ($App) {
    "claude" {
        Need "claude" "Claude Code" "irm https://claude.ai/install.ps1 | iex"
        # Its own config folder: signed in to claude.ai in the usual one, Claude Code sends that login's
        # token instead of ANTHROPIC_AUTH_TOKEN. Every role on one model, so the GPU never swaps mid-task.
        Set-ForRun @{
            CLAUDE_CONFIG_DIR = (Join-Path $HOME ".claude-gateway"); ANTHROPIC_BASE_URL = $Gateway; ANTHROPIC_AUTH_TOKEN = $Key
            ANTHROPIC_API_KEY = $null; ANTHROPIC_MODEL = $Model; ANTHROPIC_DEFAULT_OPUS_MODEL = $Model
            ANTHROPIC_DEFAULT_SONNET_MODEL = $Model; ANTHROPIC_DEFAULT_HAIKU_MODEL = $Model; CLAUDE_CODE_SUBAGENT_MODEL = $Model
            CLAUDE_CODE_MAX_CONTEXT_TOKENS = "$Context"
            CLAUDE_CODE_AUTO_COMPACT_WINDOW = "$([Math]::Max(100000, [int]($Context * 0.85)))"
            CLAUDE_CODE_MAX_OUTPUT_TOKENS = "$MaxOutput"; CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = "1"
            CLAUDE_CODE_DISABLE_THINKING = "1"; CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
            CLAUDE_CODE_ATTRIBUTION_HEADER = "0"; CLAUDE_CODE_AUTO_MODE_SERVER = "0"; DISABLE_ERROR_REPORTING = "1"
            DISABLE_FEEDBACK_COMMAND = "1"; CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY = "1"; ENABLE_CLAUDEAI_MCP_SERVERS = "false"
        }
        # The project tools, for this run: one file per workspace (it holds the key), and the tools allowed
        # without asking - they read your own projects and notes. Given after the app's own arguments,
        # since --mcp-config takes every word that follows it.
        $dir = Join-Path $HOME ".claude-gateway"
        New-Item -ItemType Directory -Force $dir | Out-Null
        $mcp = Join-Path $dir ("ai-gateway-mcp-" + ($Workspace -replace '/', '_') + ".json")
        $allow = Join-Path $dir "ai-gateway-allow.json"
        Write-Utf8 $mcp (@{ mcpServers = @{ "ai-gateway" = @{ type = "http"; url = $McpUrl
            headers = @{ Authorization = "Bearer $Key"; "X-Workspace" = $Workspace } } } } | ConvertTo-Json -Depth 6 -Compress)
        Write-Utf8 $allow '{"permissions":{"allow":["mcp__ai-gateway"]}}'
        Start-App "claude" ($Extra + @("--settings", $allow, "--mcp-config", $mcp)); exit $script:ExitCode
    }
    "opencode" {
        Need "opencode" "OpenCode" "npm install -g opencode-ai@latest"
        $entry = @{ name = $Model; tool_call = $true; limit = @{ context = $Context; output = $MaxOutput } }
        if ($Vision) { $entry.modalities = @{ input = @("text", "image"); output = @("text") } }
        # Inline: nothing in opencode.json changes, and this run alone sees the gateway
        $config = @{ '$schema' = "https://opencode.ai/config.json"; model = "ai-gateway/$Model"
            provider = @{ "ai-gateway" = @{ npm = "@ai-sdk/openai-compatible"; name = "AI Gateway"
                options = @{ baseURL = "$Gateway/v1"; apiKey = $Key }; models = @{ $Model = $entry } } }
            mcp = @{ "ai-gateway" = @{ type = "remote"; enabled = $true; url = $McpUrl
                headers = @{ Authorization = "Bearer $Key"; "X-Workspace" = $Workspace } } } }
        Set-ForRun @{ OPENCODE_CONFIG_CONTENT = ($config | ConvertTo-Json -Depth 10 -Compress) }
        Start-App "opencode" $Extra; exit $script:ExitCode
    }
    "qwen" {
        Need "qwen" "Qwen Code" "npm install -g @qwen-code/qwen-code@latest"
        Set-ForRun @{ OPENAI_API_KEY = $Key; OPENAI_BASE_URL = "$Gateway/v1"; OPENAI_MODEL = $Model }
        Start-App "qwen" (@("--auth-type", "openai", "--model", $Model) + $Extra); exit $script:ExitCode
    }
    "codex" {
        Need "codex" "Codex CLI" "npm install -g @openai/codex"
        # A provider of its own for this run only (-c), speaking the Responses API the gateway passes on
        Set-ForRun @{ AI_GATEWAY_API_KEY = $Key }
        $codexArgs = @("-c", 'model_provider="ai-gateway"', "-c", 'model_providers.ai-gateway.name="AI Gateway"',
            "-c", "model_providers.ai-gateway.base_url=`"$Gateway/v1/`"", "-c", 'model_providers.ai-gateway.wire_api="responses"',
            "-c", 'model_providers.ai-gateway.env_key="AI_GATEWAY_API_KEY"', "-c", "model_context_window=$Context",
            "-c", "mcp_servers.ai-gateway.url=`"$McpUrl`"", "-c", 'mcp_servers.ai-gateway.bearer_token_env_var="AI_GATEWAY_API_KEY"',
            "-c", "mcp_servers.ai-gateway.http_headers={`"X-Workspace`"=`"$Workspace`"}", "-m", $Model)
        Start-App "codex" ($codexArgs + $Extra); exit $script:ExitCode
    }
    "hermes" {
        Need "hermes" "Hermes Agent" "powershell -NoProfile -ExecutionPolicy Bypass -Command `"& ([scriptblock]::Create((irm https://hermes-agent.nousresearch.com/install.ps1))) -SkipSetup`""
        # A Hermes home of its own, written whole by this launcher: the usual one is not touched
        $home2 = Join-Path $HOME ".hermes-gateway"
        New-Item -ItemType Directory -Force $home2 | Out-Null
        $q = { param($v) '"' + ($v -replace '\\', '\\' -replace '"', '\"') + '"' }
        Write-Utf8 (Join-Path $home2 "config.yaml") @(
            "# Written by ai-gateway launch hermes; rewritten on every launch.",
            "model:", "  provider: ai-gateway", "  default: $(& $q $Model)", "  base_url: $(& $q "$Gateway/v1")",
            "  api_key: $(& $q $Key)", "  context_length: $Context",
            "providers:", "  ai-gateway:", "    name: AI Gateway", "    api: $(& $q "$Gateway/v1")",
            "    api_key: $(& $q $Key)", "    default_model: $(& $q $Model)", "    models: [$(& $q $Model)]",
            "toolsets: [hermes-cli, web]"
        )
        Set-ForRun @{ HERMES_HOME = $home2 }
        Start-App "hermes" $Extra; exit $script:ExitCode
    }
    "openclaw" {
        Need "openclaw" "OpenClaw" "npm install -g openclaw@latest   (OpenClaw recommends WSL2 on Windows)"
        $file = Join-Path $HOME ".openclaw\openclaw.json"
        if (-not (Test-Path $file)) { Fail "OpenClaw has not been set up yet: run 'openclaw onboard' once, then this again" }
        Copy-Item $file "$file.bak-ai-gateway" -Force
        $c = ConvertFrom-JsonTable (Get-Content $file -Raw)
        if (-not $c.models) { $c.models = @{} }; if (-not $c.models.providers) { $c.models.providers = @{} }
        if (-not $c.models.mode) { $c.models.mode = "merge" }
        # Typed, so a one-item list stays a list in the JSON: $(...) would unwrap it to "text"
        [string[]]$inputs = if ($Vision) { "text", "image" } else { "text" }
        $c.models.providers["ai-gateway"] = @{ baseUrl = "$Gateway/v1"; apiKey = $Key; api = "openai-completions"
            models = @(@{ id = $Model; name = $Model; input = $inputs
                cost = @{ input = 0; output = 0; cacheRead = 0; cacheWrite = 0 }; contextWindow = $Context; maxTokens = $MaxOutput }) }
        if (-not $c.agents) { $c.agents = @{} }; if (-not $c.agents.defaults) { $c.agents.defaults = @{} }
        if (-not $c.agents.defaults.model) { $c.agents.defaults.model = @{} }
        $c.agents.defaults.model.primary = "ai-gateway/$Model"
        # An allowlist, where there is one, has to name the model or OpenClaw refuses it
        if ($c.agents.defaults.models -and -not $c.agents.defaults.models["ai-gateway/$Model"]) { $c.agents.defaults.models["ai-gateway/$Model"] = @{} }
        Write-Utf8 $file ($c | ConvertTo-Json -Depth 30)
        if (-not $ConfigOnly) {
            openclaw daemon restart *> $null; if ($LASTEXITCODE) { openclaw daemon start *> $null }
            if ($LASTEXITCODE) { Write-Warning "could not restart the OpenClaw gateway; run 'openclaw gateway run' in another terminal" }
        }
        Start-App "openclaw" $(if ($Extra.Count) { $Extra } else { @("tui") }); exit $script:ExitCode
    }
}
