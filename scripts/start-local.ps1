# pm5bot: Start local instance (No Docker) on Windows (PowerShell)
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "=== Starting pm5bot Local Instance ===" -ForegroundColor Cyan

# 1. Locate and ensure PostgreSQL is running on port 5433
$pgBin = $null
$possiblePaths = @(
  "C:\Program Files\PostgreSQL\18\bin",
  "C:\Program Files\PostgreSQL\17\bin",
  "C:\Program Files\PostgreSQL\16\bin",
  "C:\Program Files\PostgreSQL\15\bin"
)
foreach ($p in $possiblePaths) {
  if (Test-Path "$p\pg_ctl.exe") { $pgBin = $p; break }
}
if (-not $pgBin) {
  $cmd = Get-Command pg_ctl -ErrorAction SilentlyContinue
  if ($cmd) { $pgBin = Split-Path -Parent $cmd.Source }
}

$baseDir = Join-Path $env:LOCALAPPDATA "pm5bot"
$pgData = Join-Path $baseDir "pgdata"
$pgLog = Join-Path $baseDir "pg.log"

if ($pgBin -and (Test-Path $pgData)) {
  & "$pgBin\pg_isready.exe" -h 127.0.0.1 -p 5433 -U postgres > $null 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting local PostgreSQL server on port 5433..." -ForegroundColor Yellow
    & "$pgBin\pg_ctl.exe" -D $pgData -l $pgLog -o "-p 5433" start
    Start-Sleep -Seconds 2
  }
}

# 2. Run migrations
Write-Host "Checking database migrations..." -ForegroundColor Yellow
& .\.venv\Scripts\alembic.exe -c backend\alembic.ini upgrade head

$pw = ""
if (Test-Path .env) {
  $match = Select-String -Path .env -Pattern '^ADMIN_PASSWORD=(.*)'
  if ($match) { $pw = $match.Matches[0].Groups[1].Value }
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "  pm5bot server running at http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  Open console: http://localhost:8000" -ForegroundColor Cyan
Write-Host "  Username:     admin" -ForegroundColor Cyan
if ($pw) {
  # not printed: it would sit in the terminal scrollback and any screen share
  Write-Host "  Password:     the ADMIN_PASSWORD line in .env (you can delete that line after your first login)" -ForegroundColor Cyan
}
Write-Host "==========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Gray

& .\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
