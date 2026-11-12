# pm5bot: start the bot automatically when you log in to Windows (no admin rights needed).
# Run once from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
# Undo with:                    powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
#
# It puts one small launcher in your Startup folder. At login the launcher runs scripts\start-local.ps1
# in a hidden window: that starts the local Postgres if needed, applies migrations and runs the server
# on http://localhost:8000. Evidence (scoreboard, weather and strikes logs) only builds while it runs.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$launcher = Join-Path $startup "pm5bot.cmd"
$script = Join-Path $repo "scripts\start-local.ps1"
if (-not (Test-Path $script)) { throw "start-local.ps1 not found at $script" }

$body = @"
@echo off
rem pm5bot auto-start (created by scripts\install-autostart.ps1)
start "pm5bot" /min powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$script"
"@
Set-Content -Path $launcher -Value $body -Encoding ascii
Write-Host "Installed: $launcher" -ForegroundColor Green
Write-Host "pm5bot will start at your next Windows login. To stop it now or later: scripts\stop-local.ps1"
