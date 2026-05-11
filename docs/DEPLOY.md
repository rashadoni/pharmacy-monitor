# Deployment Runbook — Hetzner CX22

**Target**: Hetzner Cloud CX22 (4 vCPU, 8GB RAM, 80GB SSD), Ubuntu 24.04, single-tenant production.

## Prerequisites (one-time)

- [ ] Hetzner Cloud account with project created
- [ ] Domain name (or subdomain) with A record pointing to server IP
- [ ] GitHub repo for the codebase (push access)
- [ ] SSH key pair (`~/.ssh/id_ed25519` recommended) added to your Hetzner account

## Phase 1 — Provision VM

```bash
# Via Hetzner Cloud Console (UI) or hcloud CLI:
hcloud server create \
  --name pharmacy-monitor-prod \
  --type cx22 \
  --image ubuntu-24.04 \
  --location nbg1 \
  --ssh-key your-key

# Note the public IP from output, set DNS A record
```

## Phase 2 — Initial server setup

```bash
ssh root@<server-ip>
# Paste/run as root:
bash <(curl -fsSL https://raw.githubusercontent.com/<your-org>/<repo>/main/infra/scripts/initial_server_setup.sh)
```

What this does:
- Installs Postgres 16, Redis, Caddy, Python 3.11, Node.js
- Creates `pm` system user + directory layout (`/opt/pharmacy-monitor`)
- Configures ufw firewall (22, 80, 443 only)
- Enables fail2ban, unattended-upgrades

## Phase 3 — Deploy code

```bash
# As pm user:
ssh pm@<server-ip>
cd /opt/pharmacy-monitor
git clone https://github.com/<your-org>/<repo> .

# Python deps
python3.11 -m venv .venv
.venv/bin/pip install uv
.venv/bin/uv sync --no-dev
.venv/bin/playwright install chromium  # browser binary

# Frontend (when ready)
cd frontend && pnpm install --frozen-lockfile && pnpm build && cd ..

# Generate strong secrets
openssl rand -hex 32  # JWT_SECRET
openssl rand -hex 32  # PHARMACY_API_KEY
```

## Phase 4 — Configure environment

```bash
# As root:
sudo -i
cp /opt/pharmacy-monitor/.env.example /etc/pharmacy-monitor/env
chown pm:pm /etc/pharmacy-monitor/env
chmod 600 /etc/pharmacy-monitor/env
nano /etc/pharmacy-monitor/env
# Fill in:
#   DATABASE_URL=postgresql+psycopg://pm:STRONG_PASS_FROM_SETUP@127.0.0.1:5432/pharmacy_monitor
#   REDIS_URL=redis://localhost:6379/0
#   JWT_SECRET=<random hex 32>
#   PHARMACY_API_KEY=<random hex 32>
#   SMTP_HOST=smtp.resend.com  (or your provider)
#   SMTP_PASSWORD=<resend api key>
#   TELEGRAM_BOT_TOKEN=<from @BotFather>
#   SENTRY_DSN=<from sentry.io project>
```

## Phase 5 — Run migrations

```bash
# Apply schema to fresh Postgres
sudo -u pm bash -c 'cd /opt/pharmacy-monitor && .venv/bin/alembic upgrade head'

# (Optional) Migrate pilot data from SQLite
# Copy your laptop's data/db.sqlite to server first via scp:
scp data/db.sqlite pm@<server-ip>:/opt/pharmacy-monitor/data/db.sqlite
sudo -u pm bash -c 'cd /opt/pharmacy-monitor && .venv/bin/python scripts/sqlite_to_pg.py'
```

## Phase 6 — systemd services

```bash
# As root:
cp /opt/pharmacy-monitor/infra/systemd/*.{service,timer} /etc/systemd/system/
systemctl daemon-reload

# Stagger scrape times via override files
mkdir -p /etc/systemd/system/pharmacy-monitor-scrape@aptekonline.timer.d
cat > /etc/systemd/system/pharmacy-monitor-scrape@aptekonline.timer.d/override.conf <<EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* 02:00:00
EOF

mkdir -p /etc/systemd/system/pharmacy-monitor-scrape@aloe.timer.d
cat > /etc/systemd/system/pharmacy-monitor-scrape@aloe.timer.d/override.conf <<EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:00:00
EOF

# Enable + start
systemctl enable --now pharmacy-monitor-api.service
systemctl enable --now pharmacy-monitor-scrape@pharmonline.timer
systemctl enable --now pharmacy-monitor-scrape@aptekonline.timer
systemctl enable --now pharmacy-monitor-scrape@aloe.timer
systemctl enable --now pharmacy-monitor-health.timer
systemctl enable --now pharmacy-monitor-backup.timer
```

## Phase 7 — Caddy reverse proxy

```bash
# As root:
nano /opt/pharmacy-monitor/infra/Caddyfile  # set DOMAIN.example.com → real domain
cp /opt/pharmacy-monitor/infra/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
# Caddy auto-issues Let's Encrypt cert in ~30s
```

## Phase 8 — GitHub Actions deploy

In GitHub repo settings → Secrets:

| Secret | Value |
|---|---|
| `HETZNER_HOST` | server IP or domain |
| `HETZNER_SSH_USER` | `pm` |
| `HETZNER_SSH_KEY` | private key contents (`cat ~/.ssh/id_ed25519`) |
| `HETZNER_SSH_KNOWN_HOSTS` | output of `ssh-keyscan <host>` |

Allow `pm` to run select sudo commands:
```
# /etc/sudoers.d/pm
pm ALL=(ALL) NOPASSWD: /bin/systemctl restart pharmacy-monitor-api.service
pm ALL=(ALL) NOPASSWD: /bin/systemctl restart pharmacy-monitor-frontend.service
pm ALL=(ALL) NOPASSWD: /bin/systemctl reload caddy
pm ALL=(ALL) NOPASSWD: /bin/systemctl reload nginx
```

Push to `main` → CI runs lint + tests → on success, deploy.yml SSH'es to Hetzner and runs `infra/deploy.sh`.

## Health checks

```bash
# API
curl https://<your-domain>/health

# Service status
systemctl status pharmacy-monitor-api
systemctl list-timers --all | grep pharmacy

# Recent logs
journalctl -u pharmacy-monitor-api -n 100 --no-pager
journalctl -u pharmacy-monitor-scrape@pharmonline -n 100 --no-pager

# DB connections
sudo -u postgres psql -c "SELECT count(*) FROM pg_stat_activity"

# Backups
ls -lth /var/backups/pharmacy-monitor/ | head
```

## Rollback

```bash
# Last good revision
ssh pm@<server>
cd /opt/pharmacy-monitor
git log --oneline -5
git reset --hard <good-sha>
.venv/bin/alembic downgrade -1   # ONLY if migration broke things
sudo systemctl restart pharmacy-monitor-api
```

For DB rollback:
```bash
gunzip -c /var/backups/pharmacy-monitor/pharmacy-monitor-<timestamp>.sql.gz | \
  sudo -u postgres psql pharmacy_monitor
```

## Monitoring URLs (after Phase 11 in main plan)

- **Sentry** — `https://sentry.io/organizations/<your-org>/issues/`
- **Grafana Cloud** — `https://<your-org>.grafana.net/`
- **Uptime-Kuma** — typically `https://uptime.<your-domain>/`

## Cost breakdown (monthly)

| Service | Cost |
|---|---|
| Hetzner CX22 | €6 |
| Hetzner Object Storage (backups) | ~€2 |
| Domain name | ~$10/year ≈ €1/mo |
| Resend (email, free tier 3K/mo enough for pilot) | €0 |
| Sentry (free tier) | €0 |
| Telegram bot | €0 |
| **Total** | **~€10/month** |

Add ~€50/mo if proxy plan needed (Phase 11).
