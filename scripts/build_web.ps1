# Build the web UI and copy it into the Python package (src\homemesh\web_dist).
# Requires Node.js >= 20.19 (https://nodejs.org).
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location (Join-Path $Root "apps\web")
try {
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm not found. Install Node.js 20.19+ first." }
  # npm prints warnings on stderr; with "Stop", Windows PowerShell 5.1 would treat them as fatal
  # when output is redirected, so rely on exit codes for these two calls.
  $ErrorActionPreference = "Continue"
  npm ci --no-audit --no-fund | Out-Host; if ($LASTEXITCODE) { throw "npm ci failed" }
  npm run build | Out-Host; if ($LASTEXITCODE) { throw "npm run build failed" }
  $ErrorActionPreference = "Stop"
  $Dest = Join-Path $Root "src\homemesh\web_dist"
  if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
  Copy-Item -Recurse (Join-Path $Root "apps\web\dist") $Dest
  Write-Host "Web UI built into src\homemesh\web_dist"
} finally { Pop-Location }
