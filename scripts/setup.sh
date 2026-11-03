#!/usr/bin/env bash
# pm5bot: first-time setup on your own computer (macOS / Linux). Safe to run again.
# Creates .env with fresh random secrets (if missing), builds and starts everything.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "Docker is not installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start Docker Desktop and try again."; exit 1; }

# (no `tr </dev/urandom | head`: under pipefail its SIGPIPE silently kills the script)
rand() { local s=""; while [ ${#s} -lt "$1" ]; do s="$s$(head -c 64 /dev/urandom | base64 | LC_ALL=C tr -dc 'A-Za-z0-9')"; done; printf '%s' "${s:0:$1}"; }
if [ ! -f .env ]; then
  # Fernet key = url-safe base64 of 32 random bytes
  MASTER_KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n')
  ADMIN_PASSWORD=$(rand 16)
  sed -e "s|^ENV=.*|ENV=production|" \
      -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand 32)|" \
      -e "s|^MASTER_KEY=.*|MASTER_KEY=${MASTER_KEY}|" \
      -e "s|^JWT_SECRET=.*|JWT_SECRET=$(rand 48)|" \
      -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" .env.example > .env
  chmod 600 .env
  echo "Created .env with new secrets. BACK IT UP: without MASTER_KEY a saved wallet can't be decrypted."
fi

if [ "${SKIP_BUILD:-0}" = "1" ]; then docker compose up -d; else docker compose up -d --build; fi

printf "Waiting for the backend"
for _ in $(seq 1 90); do
  if curl -fs http://127.0.0.1:8080/api/health 2>/dev/null | grep -q '"db":true'; then echo " ready."; break; fi
  printf "."; sleep 2
done
echo
echo "  Open:      http://localhost:8080"
echo "  Username:  admin"
echo "  Password:  $(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2-)"
echo
echo "  Paper trading only until you set ALLOW_LIVE_TRADING=true in .env (see docs/execution.md)."
