# Pull the latest code and rebuild. Your data, settings and wallet are kept (Docker volumes + .env).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
git pull --ff-only
docker compose up -d --build
Write-Host "Updated. Open http://localhost:8080"
