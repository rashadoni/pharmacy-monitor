#!/usr/bin/env bash
#
# Run on the Hetzner server (CI calls this via SSH).
# Pulls latest code, syncs deps, applies migrations, restarts services.
#
# Manual run:
#   ssh pm@server 'cd /opt/pharmacy-monitor && bash infra/deploy.sh'

set -euo pipefail

cd /opt/pharmacy-monitor

echo "==> Fetching latest code"
git fetch --all --prune
git reset --hard origin/main

echo "==> Backing up DB before migrations"
infra/scripts/backup.sh || echo "  (backup script missing or failed — proceed with caution)"

echo "==> Syncing Python deps"
.venv/bin/uv sync --frozen --no-dev

echo "==> Running alembic migrations"
.venv/bin/alembic upgrade head

if [ -d frontend ] && [ -f frontend/package.json ]; then
  echo "==> Building frontend"
  cd frontend
  pnpm install --frozen-lockfile
  pnpm build
  cd ..
fi

echo "==> Reloading services (no-downtime if SIGTERM gracefully handled)"
sudo systemctl restart pharmacy-monitor-api.service
# Frontend (if running as standalone Next.js)
if systemctl list-unit-files | grep -q pharmacy-monitor-frontend.service; then
  sudo systemctl restart pharmacy-monitor-frontend.service
fi
echo "==> Reloading nginx/caddy config"
sudo systemctl reload caddy 2>/dev/null || sudo systemctl reload nginx 2>/dev/null || true

echo "==> Health check"
sleep 3
curl -fsS http://127.0.0.1:8080/health || (echo "API health check FAILED" && exit 1)

echo "==> Deploy complete: $(git rev-parse --short HEAD)"
