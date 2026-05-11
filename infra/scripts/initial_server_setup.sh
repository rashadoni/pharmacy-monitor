#!/usr/bin/env bash
#
# Run ONCE on a fresh Hetzner Ubuntu 24.04 VM as root.
# Creates the `pm` system user, installs system deps, sets up directory layout.
#
# Manual run:
#   ssh root@hetzner-ip 'bash -s' < infra/scripts/initial_server_setup.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root"; exit 1
fi

echo "==> Updating apt"
apt update && apt upgrade -y

echo "==> Installing system deps"
# Ubuntu 24.04 ships with Python 3.12 (3.11 not in default repos).
# Our pyproject.toml requires-python=">=3.11", so 3.12 works.
apt install -y \
  postgresql-16 \
  redis-server \
  caddy \
  python3 python3-venv python3-pip python3-dev build-essential \
  git curl jq ufw fail2ban unattended-upgrades \
  nodejs npm

# Verify Python version meets requirement (>=3.11)
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Python: $PY_VER"

# Enable pnpm via corepack (Node 18+ supports it)
corepack enable 2>/dev/null || npm install -g pnpm

echo "==> Creating pm system user"
id -u pm >/dev/null 2>&1 || useradd -r -s /bin/bash -d /opt/pharmacy-monitor -m pm

echo "==> Creating directory layout"
mkdir -p /opt/pharmacy-monitor /etc/pharmacy-monitor /var/backups/pharmacy-monitor
chown -R pm:pm /opt/pharmacy-monitor /var/backups/pharmacy-monitor

echo "==> Postgres setup"
sudo -u postgres psql <<EOF
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_user WHERE usename='pm') THEN
    CREATE USER pm WITH PASSWORD 'CHANGE_ME_$(openssl rand -hex 16)';
  END IF;
END\$\$;
CREATE DATABASE pharmacy_monitor OWNER pm;
EOF

echo "==> Firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable

echo "==> fail2ban"
systemctl enable fail2ban
systemctl start fail2ban

echo "==> Unattended upgrades"
dpkg-reconfigure -plow unattended-upgrades

echo
echo "==> Done. Next steps:"
echo "    1. Place SSH pubkey for 'pm' user:  /home/pm/.ssh/authorized_keys"
echo "    2. As pm user: git clone <repo> /opt/pharmacy-monitor"
echo "    3. cd /opt/pharmacy-monitor && python3.11 -m venv .venv && .venv/bin/pip install uv && .venv/bin/uv sync"
echo "    4. cp .env.example /etc/pharmacy-monitor/env  (fill values)"
echo "    5. cp infra/Caddyfile /etc/caddy/Caddyfile  (set domain)"
echo "    6. cp infra/systemd/*.{service,timer} /etc/systemd/system/"
echo "    7. systemctl daemon-reload"
echo "    8. systemctl enable --now pharmacy-monitor-api"
echo "    9. systemctl enable --now pharmacy-monitor-scrape@pharmonline.timer"
echo "   10. systemctl enable --now pharmacy-monitor-scrape@aptekonline.timer"
echo "   11. systemctl enable --now pharmacy-monitor-scrape@aloe.timer"
echo "   12. systemctl enable --now pharmacy-monitor-health.timer"
echo "   13. systemctl enable --now pharmacy-monitor-backup.timer"
echo "   14. systemctl reload caddy"
