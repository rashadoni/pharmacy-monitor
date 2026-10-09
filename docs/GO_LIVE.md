# Go-Live Runbook

Final pre-flight checklist + DNS cutover + 72h on-call.

## T-7 days (one week before)

- [ ] Hetzner CX22 provisioned, OS hardened (`infra/scripts/initial_server_setup.sh` ran clean)
- [ ] Domain DNS A-record pointed at Hetzner IP (TTL 300s for fast cutover/rollback)
- [ ] Caddy auto-provisioned Let's Encrypt cert; SSL Labs grade A or better
- [ ] All systemd services enabled + healthy:
  ```bash
  systemctl is-active pharmacy-monitor-api
  systemctl is-active pharmacy-monitor-frontend
  systemctl is-active pharmacy-monitor-telegram-bot
  systemctl list-timers --all | grep pharmacy-monitor
  ```
- [ ] Backup ran at least once (`ls -la /var/backups/pharmacy-monitor/*.sql.gz`)
- [ ] Backup restored on a 2nd machine successfully (proves backup integrity)
- [ ] Sentry receiving events (force a test error, see it in Sentry UI)
- [ ] Prometheus + Grafana dashboard rendering metrics
- [ ] Telegram bot responds to `/help` (proves token correct)

## T-3 days

- [ ] Run `docs/SECURITY.md` checklist end-to-end. Sign off.
- [ ] Run k6 load test against staging (`load-test/api.k6.js`):
  - p95 < 500ms ✓
  - error rate < 1% ✓
  - 100 concurrent VUs sustained ✓
- [ ] Run full E2E test suite from CI (`gh run list --workflow=ci.yml`)
- [ ] All cron / systemd timers ran successfully:
  ```bash
  journalctl -u pharmacy-monitor-scrape@pharmonline --since "24 hours ago" | grep run_ok
  journalctl -u pharmacy-monitor-health --since "24 hours ago" | grep -c "" # heartbeat count
  ```
- [ ] DB has 5+ days of historical runs (so analytics + forecast have data)
- [ ] Brand catalog audited (no obvious gaps for client's category)

## T-24h

- [ ] Final DB backup taken + offsite-uploaded (`infra/scripts/backup.sh` + manual rclone)
- [ ] Notify client: "We're going live tomorrow at 14:00. Expect 5min downtime if DNS swap."
- [ ] Paging contact for on-call established (Telegram chat with alert bot)

## T-0: Cutover

```bash
# 1. Final code on main
ssh pm@server 'cd /opt/pharmacy-monitor && git log -1'  # verify HEAD

# 2. Pre-cutover snapshot
ssh pm@server 'sudo -u pm bash /opt/pharmacy-monitor/infra/scripts/backup.sh'

# 3. Apply final migrations (no-op if already applied)
ssh pm@server 'cd /opt/pharmacy-monitor && .venv/bin/alembic upgrade head'

# 4. Smoke check
curl -fsS https://your-domain.com/health
# Expect: {"status": "up", "last_run_at": "...", "last_run_status": "ok"}

# 5. DNS cutover (if not already done)
# In your DNS panel: A record pricing.your-domain.com → Hetzner IP, TTL 300

# 6. Hard test as real user (incognito)
# - Open https://your-domain.com → /login
# - Enter your real email → magic-link arrives
# - Click link → /overview loads with data
# - Search "nestle" in /comparison → results appear
# - /settings → Telegram: get a one-time code, send the bot /start <code>
# - Receive a test alert in Telegram (force one if needed)

# 7. Announce
# Telegram channel / Slack: "🚀 Pharmacy Monitor is live at https://your-domain.com"
```

## T+1h: First-hour checks

- [ ] No critical events in Sentry
- [ ] `/metrics` reports `pharmacy_app_info` with correct git_sha
- [ ] First scheduled scrape ran (or upcoming within 24h)
- [ ] Client's email got a test alert (low-severity to verify pipeline)
- [ ] `pharmacy-monitor health-check` returns exit 0

## T+24h

- [ ] Daily digest email arrived at 08:00 (if recipients opted in)
- [ ] Cron `pharmacy-monitor-scrape@*.timer` succeeded for all 3 sites
- [ ] Postgres backup ran at 04:00, file size sane
- [ ] No `captcha_hits_total` increase (if there's a spike, install proxy)
- [ ] Grafana shows steady-state metrics

## T+72h: Sign-off

- [ ] No data loss / corruption (compare row counts before vs after to staging snapshot)
- [ ] Client confirms: "I see new alerts daily, dashboard is responsive on phone, telegram works"
- [ ] All known issues from launch documented in `docs/KNOWN_ISSUES.md`

## Rollback procedure

If something is broken in production and we need to go back:

```bash
# 1. Revert to last good code revision
ssh pm@server
cd /opt/pharmacy-monitor
git log --oneline -10  # find last good SHA
git reset --hard <sha>

# 2. If DB migration broke things — downgrade
.venv/bin/alembic downgrade -1

# 3. Or fully restore from backup (DESTROYS current DB)
sudo systemctl stop pharmacy-monitor-api
gunzip -c /var/backups/pharmacy-monitor/pharmacy-monitor-PRECUTOVER.sql.gz \
  | sudo -u postgres psql pharmacy_monitor
sudo systemctl start pharmacy-monitor-api

# 4. If domain was switched — revert DNS
# Set A record back to old server, TTL 300

# 5. Notify
# Telegram: "🔄 Rolled back to v0.1.X. Investigating, ETA fix: ..."
```

## Post-mortem template

For any P1 incident in first 30 days, fill in `docs/post-mortems/YYYY-MM-DD-<title>.md`:

```markdown
# Incident: <title>

**Date**: 2026-XX-XX HH:MM (Asia/Baku)
**Duration**: from HH:MM to HH:MM
**Severity**: P1 / P2 / P3
**Author**: <name>

## What happened
<2-3 sentences for non-technical reader>

## Timeline
- HH:MM — Sentry alert fired
- HH:MM — Engineer paged via Telegram
- HH:MM — Root cause identified
- HH:MM — Mitigation deployed
- HH:MM — Resolved

## Root cause
<technical explanation>

## What went well
- ...

## What went poorly
- ...

## Action items
- [ ] (priority) (owner) (deadline) — <action>
- [ ] ...
```

## On-call rotation

For first 14 days, primary on-call = engineer who deployed.
Sentry → email + Telegram notification.
Uptime-Kuma → Telegram alert if site down >2 minutes.
Health-check (every hour) → AlertEvent in DB → daily digest if any.

After 14 days, hand off to client's IT or scheduled rotation.

## Contact

- **Primary on-call**: ___________________ (Telegram: @___)
- **Backup**: ____________________________ (Telegram: @___)
- **Client PO**: __________________________ (Email + phone)
- **Sentry org**: https://sentry.io/organizations/_____/
- **Hetzner Cloud Console**: https://console.hetzner.cloud
- **Domain registrar**: ___________________
- **DNS panel**: __________________________
