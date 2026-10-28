# Run a command with CadSwarm's optional settings from an env file loaded into its environment.
#
# Nothing is required to use or develop CADSwarm: the AI helpers use the sign-in of the CLI you
# already run (claude, codex or opencode), and SpacetimeDB uses the CLI's own stored login. This
# loader only exists for optional extras (the demo's Elastic/Creao keys, a headless machine) and for
# developer overrides. No file? It runs the command with the environment you already have.
#
#   powershell -File scripts\with-env.ps1 python -m pytest -q
#   powershell -File scripts\with-env.ps1 host\target\release\cadswarm-host.exe --data .\team run
#   powershell -File scripts\with-env.ps1 -EnvFile C:\path\to\keys.env python -m pytest -q
#
# Where the file comes from, in order:
#   1. -EnvFile <path>            (the only case where a missing file is an error)
#   2. $env:CADSWARM_ENV_FILE
#   3. %APPDATA%\CadSwarm\cadswarm.env
#   4. .env in this checkout (git-ignored)
#   5. cadswarm.env next to the checkout (outside every repo), so all worktrees share one file
# Never commit it. Values are set for this process only and are not echoed.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot

# No param block on purpose: it would bind the command's own flags (python -c, cargo --release).
$envFile = ""
$argv = New-Object System.Collections.Generic.List[string]
$i = 0
while ($i -lt $args.Count) {
    if ($args[$i] -eq "-EnvFile" -and $i + 1 -lt $args.Count) {
        $envFile = $args[$i + 1]
        $i += 2
        continue
    }
    $argv.Add([string]$args[$i])
    $i += 1
}

if ($envFile -and -not (Test-Path -LiteralPath $envFile)) {
    throw "env file not found: $envFile"  # never fall back to another file's keys when one was named
}
$candidates = @()
if ($envFile) { $candidates += (Resolve-Path -LiteralPath $envFile).Path }
if ($env:CADSWARM_ENV_FILE) { $candidates += $env:CADSWARM_ENV_FILE }
if ($env:APPDATA) { $candidates += (Join-Path $env:APPDATA "CadSwarm\cadswarm.env") }
$candidates += (Join-Path $root ".env")
$candidates += (Join-Path (Split-Path -Parent $root) "cadswarm.env")
$file = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $file = $candidate; break }
}

if ($file) {
    foreach ($raw in [System.IO.File]::ReadAllLines($file)) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { continue }
        $name = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or
                                      ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($name) { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
    }
}
# No file is fine (and the usual case): the command runs with the environment it already has.

if ($argv.Count -eq 0) {
    if ($file) {
        Write-Host "loaded $file"
    } else {
        Write-Host "no env file (optional); environment left as it is"
    }
    exit 0
}
$exe = $argv[0]
$rest = @($argv | Select-Object -Skip 1)
if ($env:CADSWARM_ENV_DEBUG) { Write-Host "env: $(if ($file) { $file } else { 'none (optional)' }) | exe: $exe | args: $($rest -join ' | ')" }
if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
    Write-Error "command not found: $exe"
    exit 127
}
& $exe $rest
exit $LASTEXITCODE
