<#
.SYNOPSIS
  Per-user installer for HomeMesh AI on Windows 10/11. No administrator rights needed.

.DESCRIPTION
  Run from a checkout or an unpacked release, in a normal (non-admin) PowerShell window:

      powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 [-Upgrade] [-AutoStart] [-NoWeb] [-NoPath] [-Python C:\Path\python.exe]

  "-ExecutionPolicy Bypass" applies to this one PowerShell process only; it does not change
  your system's execution policy.

  What it does:
    * creates a virtual environment in %LOCALAPPDATA%\Programs\HomeMesh\venv
    * installs HomeMesh from this source tree into it (pip downloads its dependencies from PyPI)
    * builds the web UI if Node.js is available and no prebuilt UI is present
    * creates %LOCALAPPDATA%\Programs\HomeMesh\bin\homemesh.cmd and adds that folder to YOUR
      user PATH (skip with -NoPath)
    * with -AutoStart: adds a per-user autostart entry (HKCU\...\Run) so HomeMesh starts at logon

  What it never does: change firewall rules, execution policy, Defender settings, system PATH
  or anything else machine-wide. Your data (%LOCALAPPDATA%\HomeMesh and %APPDATA%\HomeMesh)
  is never touched.
#>
[CmdletBinding()]
param(
  [switch]$Upgrade,
  [switch]$AutoStart,
  [switch]$NoWeb,
  [switch]$NoPath,
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Prefix = Join-Path $env:LOCALAPPDATA "Programs\HomeMesh"
$Venv = Join-Path $Prefix "venv"
$BinDir = Join-Path $Prefix "bin"
$Exe = Join-Path $Venv "Scripts\homemesh.exe"
# HomeMesh is always run as "python.exe -m homemesh": the venv's python.exe is Python's signed
# launcher, whereas pip's generated homemesh.exe is unsigned and Windows 11 Smart App Control may
# block unsigned programs.
$VPy = Join-Path $Venv "Scripts\python.exe"

function Say([string]$Message) { Write-Host $Message -ForegroundColor Cyan }
function Fail([string]$Message) { Write-Host "Error: $Message" -ForegroundColor Red; exit 1 }

# Run a native program and return its exit code. Windows PowerShell 5.1 turns a native program's
# stderr into error records when output is redirected (logging, CI, run from another script), and
# with ErrorActionPreference=Stop a harmless pip/npm warning would abort the install. We rely on
# the exit code instead.
function Invoke-Native([string]$File, [string[]]$Arguments, [switch]$Quiet) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    if ($Quiet) { & $File @Arguments *> $null } else { & $File @Arguments | Out-Host }
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
}

function Invoke-HomeMesh([string[]]$Arguments, [switch]$Quiet) {
  return Invoke-Native $VPy (@("-m", "homemesh") + $Arguments) -Quiet:$Quiet
}

# Python check: version >= 3.11, venv module and SQLite FTS5 (used for search).
$Check = "import sys, venv, sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print(sys.executable); sys.exit(0 if sys.version_info >= (3, 11) else 3)"

function Resolve-Python([string]$Cmd, [string[]]$Pre) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $Cmd @Pre -c $Check 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
  } catch {
  } finally {
    $ErrorActionPreference = $prev
  }
  return $null
}

# ---- Python --------------------------------------------------------------------------
$PyExe = $null
if ($Python) {
  $PyExe = Resolve-Python $Python @()
  if (-not $PyExe) { Fail "$Python is not Python 3.11+ with venv and SQLite FTS5." }
} else {
  # The py launcher is preferred; a bare "python" may be the Microsoft Store stub, which the
  # check above detects because it does not run the code.
  if (Get-Command py -ErrorAction SilentlyContinue) {
    # "-3" is the newest installed 3.x (checked to be >= 3.11 above); the specific versions are a
    # fallback for when the default is older. Only installed versions are tried this way.
    foreach ($v in @("-3", "-3.14", "-3.13", "-3.12", "-3.11")) {
      $PyExe = Resolve-Python "py" @($v)
      if ($PyExe) { break }
    }
  }
  if (-not $PyExe -and (Get-Command python -ErrorAction SilentlyContinue)) { $PyExe = Resolve-Python "python" @() }
}
if (-not $PyExe) {
  Fail "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH' or use the 'py' launcher), or with: winget install Python.Python.3.13"
}
Say "Using Python at $PyExe"

# ---- Web UI ----------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $Root "src\homemesh\web_dist\index.html"))) {
  if (-not $NoWeb -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    Say "Building the web interface..."
    & (Join-Path $Root "scripts\build_web.ps1")
  } else {
    Write-Host "Note: no prebuilt web UI found and Node.js/npm is not available (or -NoWeb)."
    Write-Host "      The API and CLI will work; install Node.js 20.19+ and run scripts\build_web.ps1 for the UI."
  }
}

# ---- Virtual environment + package -------------------------------------------------------
if ((Test-Path $Exe) -and -not $Upgrade) {
  Say "HomeMesh is already installed in $Venv - re-run with -Upgrade to update it."
} else {
  $wasRunning = $false
  if ((Test-Path $Exe) -and $Upgrade) {
    # Exit codes of 'homemesh status': 0 running, 2 running but degraded (e.g. Ollama down), 3 not running.
    # It must be stopped before upgrading: Windows locks the running python.exe and extension modules.
    $code = Invoke-HomeMesh @("status") -Quiet
    $wasRunning = ($code -eq 0 -or $code -eq 2)
    if ($wasRunning) {
      Say "Stopping HomeMesh for the upgrade..."
      if (Invoke-HomeMesh @("stop")) { Fail "Could not stop HomeMesh; run 'homemesh stop --force' and try again." }
    }
  }
  Say "Installing into $Venv ..."
  New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
  if (Invoke-Native $PyExe @("-m", "venv", $Venv)) { Fail "Could not create the virtual environment." }
  if (Invoke-Native $VPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", "pip")) {
    Fail "pip upgrade failed (check your internet connection or proxy)."
  }
  if (Invoke-Native $VPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", $Root)) {
    Fail "Installing HomeMesh failed - see the pip output above."
  }
  if ($wasRunning) {
    Say "Starting HomeMesh again (database migrations run automatically, with a backup first)..."
    [void](Invoke-HomeMesh @("start"))
  }
}

# ---- Command shim + user PATH ----------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Shim = Join-Path $BinDir "homemesh.cmd"
Set-Content -Path $Shim -Encoding ASCII -Value "@echo off`r`n`"%~dp0..\venv\Scripts\python.exe`" -m homemesh %*`r`nexit /b %ERRORLEVEL%`r`n"

if (-not $NoPath) {
  # Read the raw (unexpanded) user PATH from the registry so entries such as %USERPROFILE%\bin
  # keep working; .NET's SetEnvironmentVariable would expand them and change the value type.
  $key = Get-Item -Path "HKCU:\Environment"
  $raw = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  $parts = @($raw -split ";" | Where-Object { $_ -ne "" })
  if ($parts -notcontains $BinDir) {
    $new = (@($parts) + $BinDir) -join ";"
    New-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value $new -PropertyType ExpandString -Force | Out-Null
    # Tell running programs (Explorer) that the environment changed, so new terminals see it.
    [Environment]::SetEnvironmentVariable("HOMEMESH_ENV_REFRESH", "1", "User")
    [Environment]::SetEnvironmentVariable("HOMEMESH_ENV_REFRESH", $null, "User")
    Say "Added $BinDir to your user PATH (open a new terminal to use 'homemesh')."
  }
  $env:Path = "$BinDir;$env:Path"
} else {
  Write-Host "Skipped PATH update. Run HomeMesh as: $Shim"
}

# ---- Optional automatic start ---------------------------------------------------------------
if ($AutoStart) {
  [void](Invoke-HomeMesh @("service", "install"))
}

Say "Installed:"
if (Invoke-HomeMesh @("version")) { Fail "The installed 'homemesh' command does not run." }
Write-Host @"

Next steps (in a new terminal):
  homemesh setup             # choose standalone / coordinator / worker / client
  homemesh service install   # optional: start automatically when you log in
  homemesh start             # start in the background
  homemesh doctor            # check GPU, Ollama, ports, database

Local AI models: install Ollama from https://ollama.com/download and pull a model, e.g.
  ollama pull llama3.2:3b
  ollama pull nomic-embed-text

Firewall: a computer that only runs the WORKER role makes outbound connections only and needs
no firewall change. If THIS computer is the coordinator for other computers, Windows will ask
whether to allow Python on private networks the first time it listens on the LAN - allow it for
Private networks only. See INSTALLATION.md for the manual rule if you prefer to add it yourself.
"@
