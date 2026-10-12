<#
.SYNOPSIS
  Remove the HomeMesh program for the current user.

.DESCRIPTION
      powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 [-RemoveData]

  Stops HomeMesh, removes the per-user autostart entry, the program folder
  (%LOCALAPPDATA%\Programs\HomeMesh) and its PATH entry.

  Your data (conversations, memories, settings, keys, backups) in %LOCALAPPDATA%\HomeMesh and
  %APPDATA%\HomeMesh is KEPT unless you pass -RemoveData and type DELETE to confirm.
#>
[CmdletBinding()]
param([switch]$RemoveData)
$ErrorActionPreference = "Stop"

$Prefix = Join-Path $env:LOCALAPPDATA "Programs\HomeMesh"
$BinDir = Join-Path $Prefix "bin"
$Exe = Join-Path $Prefix "venv\Scripts\homemesh.exe"

if (Test-Path $Exe) {
  # Native calls with ErrorActionPreference=Continue: Windows PowerShell 5.1 would otherwise turn
  # any stderr output into a terminating error when output is redirected.
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  & $Exe stop | Out-Host
  if ($LASTEXITCODE) { $ErrorActionPreference = $prev; Write-Host "Could not stop HomeMesh; run 'homemesh stop --force' first." -ForegroundColor Red; exit 1 }
  & $Exe service uninstall | Out-Host
  $ErrorActionPreference = $prev
}

# Remove only our own entry from the user PATH, preserving everything else unexpanded.
$key = Get-Item -Path "HKCU:\Environment"
$raw = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$parts = @($raw -split ";" | Where-Object { $_ -ne "" })
if ($parts -contains $BinDir) {
  $new = (@($parts | Where-Object { $_ -ne $BinDir })) -join ";"
  New-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value $new -PropertyType ExpandString -Force | Out-Null
  [Environment]::SetEnvironmentVariable("HOMEMESH_ENV_REFRESH", "1", "User")
  [Environment]::SetEnvironmentVariable("HOMEMESH_ENV_REFRESH", $null, "User")
}

if (Test-Path $Prefix) { Remove-Item -Recurse -Force $Prefix }
Write-Host "HomeMesh program removed."

$DataDir = Join-Path $env:LOCALAPPDATA "HomeMesh"
$ConfDir = Join-Path $env:APPDATA "HomeMesh"
if ($RemoveData) {
  Write-Host "This permanently deletes $DataDir and $ConfDir (conversations, memories, keys, backups)."
  $answer = Read-Host "Type DELETE to confirm"
  if ($answer -ceq "DELETE") {
    foreach ($d in @($DataDir, $ConfDir)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
    Write-Host "Data deleted."
  } else {
    Write-Host "Kept your data."
  }
} else {
  Write-Host "Your data is kept in $DataDir and $ConfDir (re-run with -RemoveData to delete it)."
}
