# Back up the database (trades, settings, encrypted wallet, recorded market history) to .\backups.
# Keep .env too: MASTER_KEY is needed to decrypt the saved wallet.
# The dump is written inside the container and copied out byte-for-byte (no PowerShell re-encoding).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force backups | Out-Null
$name = "pm5-$(Get-Date -Format yyyyMMdd-HHmmss).dump"
docker compose exec -T postgres pg_dump -U pm5 -d pm5 -Fc -f /tmp/$name
if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed. Is pm5bot running? (docker compose ps)"; exit 1 }
docker compose cp postgres:/tmp/$name backups/$name
docker compose exec -T postgres rm -f /tmp/$name
Write-Host "Saved backups\$name"
Write-Host "Restore:  docker compose cp backups\$name postgres:/tmp/r.dump; docker compose exec -T postgres pg_restore -U pm5 -d pm5 --clean --if-exists /tmp/r.dump"
