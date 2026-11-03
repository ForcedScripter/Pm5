# pm5bot: first-time setup on Windows (PowerShell). Safe to run again.
# Run from the repo folder:   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker is not installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/"; exit 1 }
# Windows PowerShell 5.1 turns redirected native stderr into a terminating error under "Stop": relax it here
$ErrorActionPreference = "Continue"
docker info > $null 2>&1
$dockerOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $dockerOk) { Write-Host "Docker is installed but not running. Start Docker Desktop and try again."; exit 1 }

function RandLong([int]$n) { -join (1..$n | ForEach-Object { [char](Get-Random -InputObject (48..57 + 65..90 + 97..122)) }) }

if (-not (Test-Path .env)) {
  $bytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $masterKey = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')   # Fernet key
  $admin = RandLong 16
  (Get-Content .env.example) `
    -replace '^ENV=.*', 'ENV=production' `
    -replace '^POSTGRES_PASSWORD=.*', ("POSTGRES_PASSWORD=" + (RandLong 32)) `
    -replace '^MASTER_KEY=.*', ("MASTER_KEY=" + $masterKey) `
    -replace '^JWT_SECRET=.*', ("JWT_SECRET=" + (RandLong 48)) `
    -replace '^ADMIN_PASSWORD=.*', ("ADMIN_PASSWORD=" + $admin) | Set-Content -Encoding ascii .env
  Write-Host "Created .env with new secrets. BACK IT UP: without MASTER_KEY a saved wallet can't be decrypted."
}

if ($env:SKIP_BUILD -eq "1") { docker compose up -d } else { docker compose up -d --build }

Write-Host -NoNewline "Waiting for the backend"
for ($i = 0; $i -lt 90; $i++) {
  try { if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/api/health -TimeoutSec 3).Content -match '"db":true') { Write-Host " ready."; break } } catch {}
  Write-Host -NoNewline "."; Start-Sleep 2
}
$pw = (Select-String -Path .env -Pattern '^ADMIN_PASSWORD=(.*)').Matches[0].Groups[1].Value
Write-Host ""
Write-Host "  Open:      http://localhost:8080"
Write-Host "  Username:  admin"
Write-Host "  Password:  $pw"
Write-Host ""
Write-Host "  Paper trading only until you set ALLOW_LIVE_TRADING=true in .env (see docs\execution.md)."
