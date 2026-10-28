# Bring up the whole CadSwarm demo on this machine: build the host and web UI, start the
# local vite preview server, open a Cloudflare quick tunnel for a public URL, init the
# data folder against Maincloud, run the host through with-env.ps1 (an env file is optional), then print
# a single-use invite (with QR code) for the phone.
#
#   powershell -File scripts\demo-up.ps1
#   powershell -File scripts\demo-up.ps1 -SkipBuild
#   powershell -File scripts\demo-up.ps1 -WebUrl https://<your-vercel-site>/app/
#   powershell -File scripts\demo-up.ps1 -AgentCommand C:\path\claude.exe,-p,{prompt},--model,sonnet
#
# -Data <dir>          data folder; default $env:USERPROFILE\Documents\Projects\cadswarm-demo
# -Db <name>           SpacetimeDB database name on Maincloud
# -Python <exe>        Python with the cadswarm package (runs `cadswarm setup` and the helper)
# -Agent <name>        claude | codex | opencode: the AI that builds briefs
# -AgentCommand <argv> override agent_command in host.json ({prompt} is replaced)
# -WebPort <n>         local vite preview port (default 4173)
# -WebUrl <url>        use this public URL as-is instead of starting a tunnel
# -SkipBuild           skip the cargo build and npm ci/build steps
#
# Tools are found from PATH (or SPACETIME_BIN / the usual install folders) and fail with
# a plain message when missing. Everything started here is recorded in
# <Data>\.cadswarm-team\demo-pids.json and logged in the same folder; stop it again with
# scripts\demo-down.ps1.

param(
  [string]$Data = "$env:USERPROFILE\Documents\Projects\cadswarm-demo",
  [string]$Db = "cadswarm-calhacks-0180",
  [string]$Python = "",
  [ValidateSet("claude","codex","opencode")][string]$Agent = "claude",
  [string[]]$AgentCommand = @(),
  [int]$WebPort = 4173,
  [string]$WebUrl = "",
  [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Run an external command, merging stderr into stdout, and throw a readable message on failure.
# (Windows PowerShell 5.1 turns a native command's stderr into errors under 'Stop'.)
function Invoke-Native([string]$Exe, [string[]]$ArgList) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ArgList 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        throw "$Exe failed (exit $code): " + (($out | Select-Object -Last 5) -join ' | ')
    }
    return $out
}

# Quote an argument for Start-Process (it joins -ArgumentList with spaces and does not quote).
function ConvertTo-ArgText([string]$Value) {
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

$state = Join-Path $Data ".cadswarm-team"
if (-not (Test-Path -LiteralPath $state)) { New-Item -ItemType Directory -Path $state -Force | Out-Null }
$pidFile = Join-Path $state "demo-pids.json"

# A stack from an earlier run that is still up: starting another would lose track of it.
if (Test-Path -LiteralPath $pidFile) {
    $old = $null
    try { $old = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json } catch { }
    foreach ($name in 'preview', 'tunnel', 'host') {
        $id = 0
        if ($old -and $old.$name) { $id = [int]$old.$name }
        if ($id -le 0) { continue }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        if ($proc -and $proc.CommandLine -match 'vite|cloudflared|cadswarm-host|with-env\.ps1') {
            throw "the demo stack is already running ($name pid $id); stop it first with scripts\demo-down.ps1"
        }
    }
}

# Record what is running after every start, so demo-down can stop it even if a later step fails.
$viteProc = $null
$tunnelProc = $null
$hostProc = $null
function Save-Pids {
    $pids = @{ preview = 0; tunnel = 0; host = 0 }
    if ($viteProc) { $pids.preview = $viteProc.Id }
    if ($tunnelProc) { $pids.tunnel = $tunnelProc.Id }
    if ($hostProc) { $pids.host = $hostProc.Id }
    [System.IO.File]::WriteAllText($pidFile, ($pids | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))
}

# --- tools ---------------------------------------------------------------------------
$spacetime = ""
if ($env:SPACETIME_BIN -and (Test-Path -LiteralPath $env:SPACETIME_BIN)) {
    $spacetime = $env:SPACETIME_BIN
} else {
    $cmd = Get-Command spacetime -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $spacetime = $cmd.Source
    } elseif (Test-Path -LiteralPath "$env:LOCALAPPDATA\SpacetimeDB\spacetime.exe") {
        $spacetime = "$env:LOCALAPPDATA\SpacetimeDB\spacetime.exe"
    }
}
if (-not $spacetime) {
    throw "spacetime not found: set SPACETIME_BIN, put spacetime on PATH, or install it to $env:LOCALAPPDATA\SpacetimeDB\spacetime.exe"
}

$cloudflared = ""
if (-not $WebUrl) {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $cloudflared = $cmd.Source
    } elseif (Test-Path -LiteralPath "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe") {
        $cloudflared = "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe"
    } elseif (Test-Path -LiteralPath "$env:ProgramFiles\cloudflared\cloudflared.exe") {
        $cloudflared = "$env:ProgramFiles\cloudflared\cloudflared.exe"
    }
    if (-not $cloudflared) {
        throw "cloudflared not found: put it on PATH or install it to $env:ProgramFiles\cloudflared\, or pass -WebUrl to skip the tunnel"
    }
}

$cmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $cmd -or -not $cmd.Source) { throw "node not found on PATH" }
$node = $cmd.Source

$cmd = Get-Command npm -ErrorAction SilentlyContinue
if (-not $cmd -or -not $cmd.Source) { throw "npm not found on PATH" }
$npm = $cmd.Source

$hostExe = "$root\host\target\release\cadswarm-host.exe"

if (-not $Python) {
    if (Test-Path -LiteralPath "$root\.venv\Scripts\python.exe") {
        $Python = "$root\.venv\Scripts\python.exe"
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd -or -not $cmd.Source) { throw "python not found: install it, create $root\.venv, or pass -Python" }
        $Python = $cmd.Source
    }
}

# --- build ---------------------------------------------------------------------------
if (-not $SkipBuild) {
    Invoke-Native cargo @('build','--release','--manifest-path',"$root\host\Cargo.toml") | Out-Null
    if (-not (Test-Path -LiteralPath "$root\web\node_modules")) {
        Invoke-Native $npm @('--prefix',"$root\web",'ci') | Out-Null
    }
    Invoke-Native $npm @('--prefix',"$root\web",'run','build') | Out-Null
}
if (-not (Test-Path -LiteralPath $hostExe)) {
    throw "host binary not found: $hostExe"
}

# --- web preview + tunnel ------------------------------------------------------------
if (-not $WebUrl) {
    $viteArgs = @(
        "$root\web\node_modules\vite\bin\vite.js",
        'preview','--config','web/vite.config.ts','web',
        '--port',"$WebPort",'--strictPort','--host','127.0.0.1'
    ) | ForEach-Object { ConvertTo-ArgText $_ }
    $viteProc = Start-Process -FilePath $node -ArgumentList $viteArgs -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Save-Pids

    $tunnelLog = Join-Path $state "tunnel.log"
    $tunnelOut = Join-Path $state "tunnel.out.log"
    if (Test-Path -LiteralPath $tunnelLog) { Remove-Item -LiteralPath $tunnelLog -Force }
    $tunnelProc = Start-Process -FilePath $cloudflared `
        -ArgumentList @('tunnel','--no-autoupdate','--url',"http://127.0.0.1:$WebPort") `
        -RedirectStandardError $tunnelLog -RedirectStandardOutput $tunnelOut `
        -WindowStyle Hidden -PassThru
    Save-Pids

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $tunnelLog) {
            $text = ""
            try { $text = Get-Content -LiteralPath $tunnelLog -Raw -ErrorAction SilentlyContinue } catch { $text = "" }
            if ($text -and $text -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                $WebUrl = $Matches[0]
                break
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $WebUrl) { throw "cloudflared did not report a trycloudflare.com URL within 60s (see $tunnelLog)" }
}

$deadline = (Get-Date).AddSeconds(30)
$reachable = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing "$WebUrl/boards.json" -TimeoutSec 10
        if ($resp.StatusCode -eq 200) { $reachable = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $reachable) {
    Write-Warning "$WebUrl/boards.json did not answer 200 within 30s; carrying on anyway"
}

# --- init the data folder ------------------------------------------------------------
# init reads the spacetime CLI's login token, so the CLI must be on PATH.
$spacetimeDir = Split-Path -Parent $spacetime
$env:PATH = "$spacetimeDir;$env:PATH"
Invoke-Native $hostExe @('--data',$Data,'init','--mode','maincloud','--db',$Db,'--web-url',$WebUrl,'--python',$Python,'--agent',$Agent) | Out-Null

if ($AgentCommand.Count -gt 0) {
    $hostJson = Join-Path $Data "host.json"
    $cfg = Get-Content -LiteralPath $hostJson -Raw | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName agent_command -NotePropertyValue $AgentCommand -Force
    [System.IO.File]::WriteAllText($hostJson, ($cfg | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
}

# --- run the host (through with-env.ps1; an env file is optional) ---------------------
$hostStatus = Join-Path $state "host-status.json"
$hostLog = Join-Path $state "host.log"
$hostErr = Join-Path $state "host.err.log"
if (Test-Path -LiteralPath $hostStatus) { Remove-Item -LiteralPath $hostStatus -Force }

$shell = if ($PSVersionTable.PSEdition -eq 'Core') { (Get-Process -Id $PID).Path } else { 'powershell.exe' }
$hostArgs = @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',
    "$root\scripts\with-env.ps1",
    $hostExe,'--data',$Data,'run'
) | ForEach-Object { ConvertTo-ArgText $_ }
$hostProc = Start-Process -FilePath $shell -ArgumentList $hostArgs `
    -WorkingDirectory $Data -WindowStyle Hidden `
    -RedirectStandardOutput $hostLog -RedirectStandardError $hostErr -PassThru
Save-Pids

function Get-ErrTail {
    if (Test-Path -LiteralPath $hostErr) {
        return ((Get-Content -LiteralPath $hostErr -Tail 20 -ErrorAction SilentlyContinue) -join ' | ')
    }
    return "(no $hostErr)"
}

$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $hostProc.Refresh()
    if ($hostProc.HasExited) {
        throw "host exited (exit $($hostProc.ExitCode)) before reporting ready: $(Get-ErrTail)"
    }
    if (Test-Path -LiteralPath $hostStatus) {
        try {
            $status = Get-Content -LiteralPath $hostStatus -Raw | ConvertFrom-Json
            if ($status.ready -eq $true) { $ready = $true; break }
        } catch { }
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    throw "host did not report ready within 90s ($hostStatus missing or not ready): $(Get-ErrTail)"
}

# --- invite --------------------------------------------------------------------------
$invite = Invoke-Native $hostExe @('--data',$Data,'invite','--kind','human','--role','lead')
foreach ($line in $invite) { Write-Host $line }

$phoneLine = $null
foreach ($line in $invite) {
    if (([string]$line).TrimStart().StartsWith('phone:')) { $phoneLine = [string]$line; break }
}

$bar = '=' * 64
Write-Host ""
Write-Host $bar
Write-Host "Open on your phone (single-use invite)"
if ($phoneLine) { Write-Host $phoneLine }
Write-Host $bar
Write-Host ""
Write-Host "Another person: & '$hostExe' --data '$Data' invite --kind human"
Write-Host 'Stop everything: scripts\demo-down.ps1'
