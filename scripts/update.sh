#!/usr/bin/env bash
# Pull the latest code and rebuild. Your data, settings and wallet are kept (Docker volumes + .env).
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
echo "Updated. Open http://localhost:8080"
