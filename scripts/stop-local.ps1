# pm5bot: Stop local database and background services
Set-Location (Split-Path -Parent $PSScriptRoot)

$possiblePaths = @(
  "C:\Program Files\PostgreSQL\18\bin",
  "C:\Program Files\PostgreSQL\17\bin",
  "C:\Program Files\PostgreSQL\16\bin",
  "C:\Program Files\PostgreSQL\15\bin"
)
$pgBin = $null
foreach ($p in $possiblePaths) {
  if (Test-Path "$p\pg_ctl.exe") { $pgBin = $p; break }
}
if (-not $pgBin) {
  $cmd = Get-Command pg_ctl -ErrorAction SilentlyContinue
  if ($cmd) { $pgBin = Split-Path -Parent $cmd.Source }
}

$baseDir = Join-Path $env:LOCALAPPDATA "pm5bot"
$pgData = Join-Path $baseDir "pgdata"
if ($pgBin -and (Test-Path $pgData)) {
  Write-Host "Stopping local PostgreSQL..." -ForegroundColor Yellow
  & "$pgBin\pg_ctl.exe" -D $pgData stop -m fast
}
Write-Host "Local services stopped." -ForegroundColor Green
