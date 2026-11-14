param(
    [Parameter(Mandatory = $true)][string]$AppRoot,
    [string]$WorkflowPath,
    [string]$OAuthEnvFile,
    [string]$CodeRoot,
    [int]$Port = 18331,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
if (-not $CodeRoot) { $CodeRoot = Split-Path -Parent $PSScriptRoot }
$applicationRoot = [IO.Path]::GetFullPath($AppRoot)
$sourceRoot = [IO.Path]::GetFullPath($CodeRoot)
if (-not $WorkflowPath) {
    $WorkflowPath = Join-Path $applicationRoot 'WORKFLOW.md'
}

function Import-LocalEnv([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $name = $matches[1]
            $value = $matches[2].Trim()
            if ($value.Length -ge 2 -and (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )) { $value = $value.Substring(1, $value.Length - 2) }
            Set-Item -LiteralPath "Env:$name" -Value $value
        }
    }
}

Import-LocalEnv (Join-Path $applicationRoot 'env')
Import-LocalEnv $OAuthEnvFile
if (-not $env:SYMPHONY_GITHUB_TOKEN) {
    $token = & gh auth token 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $token) {
        throw 'Missing host GitHub token; configure SYMPHONY_GITHUB_TOKEN or gh authentication.'
    }
    $env:SYMPHONY_GITHUB_TOKEN = "$token".Trim()
}
$env:PYTHONPATH = Join-Path $sourceRoot 'python'
$env:PYTHONUNBUFFERED = '1'
$env:PI_CODING_AGENT_DIR = Join-Path $applicationRoot 'pi-agent'
$env:SYMPHONY_WORKSPACE_ROOT = (Join-Path $applicationRoot 'workspaces').Replace('\', '/')
$env:SYMPHONY_SESSIONS_DIR = (Join-Path $applicationRoot 'sessions').Replace('\', '/')
$env:SYMPHONY_SKILLS_DIR = (Join-Path $sourceRoot '.codex/skills').Replace('\', '/')
$env:SYMPHONY_DEPLOY_KEY = (Join-Path $applicationRoot 'agent_deploy_key').Replace('\', '/')
if (-not $env:SYMPHONY_GIT_NAME) { $env:SYMPHONY_GIT_NAME = 'Symphony' }
if (-not $env:SYMPHONY_GIT_EMAIL) {
    $env:SYMPHONY_GIT_EMAIL = 'symphony@users.noreply.github.com'
}
$python = (Get-Command python -ErrorAction Stop).Source
$arguments = @(
    '-m', 'symphony',
    '--workflow', $WorkflowPath,
    '--port', "$Port",
    '--i-understand-that-this-will-be-running-without-the-usual-guardrails'
)
if ($Check) {
    & $python @arguments --check
    exit $LASTEXITCODE
}

foreach ($directory in @('logs', 'workspaces', 'sessions')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $applicationRoot $directory) | Out-Null
}
# Keep the launcher alive for the service's lifetime. The lock prevents
# accidentally starting a second local claimant with the same app directory.
$lockPath = Join-Path $applicationRoot 'run.lock'
$lockStream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
try {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "Port $Port is already listening; inspect the existing instance before starting."
    }
    $logPath = Join-Path $applicationRoot 'logs/main.log'
    & $python @arguments --log-file $logPath
    $serviceExit = $LASTEXITCODE
} finally {
    $lockStream.Dispose()
}
exit $serviceExit
