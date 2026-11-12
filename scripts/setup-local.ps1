# pm5bot: Local setup without Docker on Windows (PowerShell). Safe to run again.
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "=== pm5bot Local Setup (No Docker) ===" -ForegroundColor Cyan

# 1. Locate PostgreSQL binaries
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
if (-not $pgBin) {
  Write-Host "PostgreSQL binaries not found. Please install PostgreSQL from https://www.postgresql.org/download/windows/" -ForegroundColor Red
  exit 1
}
Write-Host "Found PostgreSQL at: $pgBin" -ForegroundColor Green

# 2. Initialize local data directory in LOCALAPPDATA (avoids OneDrive lock issues)
$baseDir = Join-Path $env:LOCALAPPDATA "pm5bot"
if (-not (Test-Path $baseDir)) { New-Item -ItemType Directory -Path $baseDir | Out-Null }
$pgData = Join-Path $baseDir "pgdata"
$pgLog = Join-Path $baseDir "pg.log"

function RandLong([int]$n) { -join (1..$n | ForEach-Object { [char](Get-Random -InputObject (48..57 + 65..90 + 97..122)) }) }
$pgPwFile = Join-Path $baseDir "pg.password"
if (-not (Test-Path $pgData)) {
  Write-Host "Initializing local PostgreSQL database cluster in $pgData..." -ForegroundColor Yellow
  # password auth: without it any program on this PC could read or change the trading database
  RandLong 32 | Set-Content -Encoding ascii -NoNewline $pgPwFile
  & "$pgBin\initdb.exe" -D $pgData -U postgres -A scram-sha-256 --pwfile=$pgPwFile --encoding=UTF8
}
$pgPw = ""
if (Test-Path $pgPwFile) { $pgPw = (Get-Content $pgPwFile -Raw).Trim(); $env:PGPASSWORD = $pgPw }
$pgAuth = "postgres"
if ($pgPw) { $pgAuth = "postgres:$pgPw" }

# 3. Start local PostgreSQL on port 5433
Write-Host "Starting local PostgreSQL server on port 5433..." -ForegroundColor Yellow
& "$pgBin\pg_ctl.exe" -D $pgData -l $pgLog -o "-p 5433" start
Start-Sleep -Seconds 2

# Check if ready
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
  & "$pgBin\pg_isready.exe" -h 127.0.0.1 -p 5433 -U postgres > $null 2>&1
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Start-Sleep -Seconds 1
}
if (-not $ready) {
  Write-Host "PostgreSQL failed to start. Check $pgLog" -ForegroundColor Red
  exit 1
}
Write-Host "PostgreSQL is ready on 127.0.0.1:5433" -ForegroundColor Green

# 4. Create pm5 database if missing
$dbList = & "$pgBin\psql.exe" -h 127.0.0.1 -p 5433 -U postgres -lqt
if ($dbList -notmatch "\bpm5\b") {
  Write-Host "Creating 'pm5' database..." -ForegroundColor Yellow
  & "$pgBin\createdb.exe" -h 127.0.0.1 -p 5433 -U postgres pm5
}

# 5. Create .env if not exists
if (-not (Test-Path .env)) {
  $bytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $masterKey = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
  $admin = RandLong 16
  (Get-Content .env.example) `
    -replace '^ENV=.*', 'ENV=development' `
    -replace '^DATABASE_URL=.*', ("DATABASE_URL=postgresql+asyncpg://" + $pgAuth + "@127.0.0.1:5433/pm5") `
    -replace '^POSTGRES_PASSWORD=.*', 'POSTGRES_PASSWORD=' `
    -replace '^MASTER_KEY=.*', ("MASTER_KEY=" + $masterKey) `
    -replace '^JWT_SECRET=.*', ("JWT_SECRET=" + (RandLong 48)) `
    -replace '^ADMIN_PASSWORD=.*', ("ADMIN_PASSWORD=" + $admin) | Set-Content -Encoding ascii .env
  Write-Host "Created .env with secrets." -ForegroundColor Green
}

# 6. Python virtual environment & backend requirements
if (-not (Test-Path .venv)) {
  Write-Host "Creating Python virtual environment in .venv..." -ForegroundColor Yellow
  python -m venv .venv
}
Write-Host "Installing backend dependencies..." -ForegroundColor Yellow
& .\.venv\Scripts\pip install -r backend\requirements.txt

# 7. Run database migrations
Write-Host "Running database migrations..." -ForegroundColor Yellow
& .\.venv\Scripts\alembic upgrade head

# 8. Install frontend dependencies and build
Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
Push-Location frontend
try {
  npm install
  Write-Host "Building frontend assets..." -ForegroundColor Yellow
  npm run build
} finally {
  Pop-Location
}

$pw = (Select-String -Path .env -Pattern '^ADMIN_PASSWORD=(.*)').Matches[0].Groups[1].Value
Write-Host ""
Write-Host "=== Setup Complete! ===" -ForegroundColor Green
Write-Host "To start the application, run:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "Access credentials:" -ForegroundColor Cyan
Write-Host "  URL:       http://localhost:8000"
Write-Host "  Username:  admin"
Write-Host "  Password:  $pw"
Write-Host ""
