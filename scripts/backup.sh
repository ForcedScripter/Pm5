#!/usr/bin/env bash
# Back up the database (trades, settings, encrypted wallet, recorded market history) to ./backups.
# Keep .env too: MASTER_KEY is needed to decrypt the saved wallet.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
name="pm5-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T postgres pg_dump -U pm5 -d pm5 -Fc -f "/tmp/$name" || { echo "Backup failed. Is pm5bot running? (docker compose ps)"; exit 1; }
docker compose cp "postgres:/tmp/$name" "backups/$name"
docker compose exec -T postgres rm -f "/tmp/$name"
echo "Saved backups/$name ($(du -h "backups/$name" | cut -f1))"
echo "Restore: docker compose cp backups/$name postgres:/tmp/r.dump && docker compose exec -T postgres pg_restore -U pm5 -d pm5 --clean --if-exists /tmp/r.dump"
