# Deployment Runbook — written for the first server (Hetzner CX22)

> ⚠️ Marked 2026-10-11. This runbook was written for the first production
> server, a Hetzner Cloud VM. That server is gone: production moved to a Contabo
> VPS on 2026-09-03 (address, access and what runs there — `CLAUDE.md`, "Current
> production state"). On 2026-10-11 the runbook was compared with the repository
> and, by reading only, with the server:
>
> - **Rewritten to match:** Phase 8, and the paragraph about scan times at the
>   end of Phase 6.
> - **Marked outdated, old text left in place under the mark:** Prerequisites
>   and Phase 1, Phase 3, Phase 7, "Rollback", "Monitoring URLs", "Cost
>   breakdown". Each mark says what was found.
> - **Corrected in place:** the list of what `initial_server_setup.sh` does
>   (Phase 2) and the comment on `install_systemd_schedule.sh` (Phase 6).
> - **Noted, not rewritten:** Phase 6 — a note under its command block names
>   what the commands leave out; the block itself was not otherwise re-checked.
> - **Not re-checked:** Phase 4 (beyond the owner and mode of the env file on
>   the server, which match), Phase 5, "Health checks", and in Phase 6 the
>   sentence about the watchlist and the paragraph about Firecrawl.
>
> Nobody has run Phases 1–7 against a fresh machine to see whether they still
> produce a working server, and the repository has no record of how the Contabo
> VPS was set up. Treat them as history of the first install, not as a tested
> procedure.

**Target when written**: Hetzner Cloud CX22 (4 vCPU, 8GB RAM, 80GB SSD), Ubuntu 24.04, single-tenant production.

**Production today** (read from the server 2026-10-11): Contabo VPS, 4 vCPU, 8 GB RAM, 96 GB disk, Ubuntu 24.04, time zone Europe/Berlin.

## Prerequisites (one-time)

> ⚠️ Outdated (marked 2026-10-11). The Hetzner items here and all of Phase 1
> apply to a provider the owner left in September 2026. What the equivalent
> steps were for Contabo is not recorded in the repository.

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

What this does (compared with the script 2026-10-11):
- Installs Postgres 16, Redis, Caddy, the distribution's `python3` (not Python
  3.11, as this line used to say — on Ubuntu 24.04 that is 3.12; the venv on
  the server runs Python 3.12.3), Node.js and pnpm
- Creates `pm` system user + directory layout (`/opt/pharmacy-monitor`)
- Creates the Postgres role `pm` and the database `pharmacy_monitor`. The
  role's password is generated inside the script and never printed, so the
  `STRONG_PASS_FROM_SETUP` that Phase 4 asks for has to be set by hand
  afterwards
- Configures ufw firewall (22, 80, 443 only)
- Enables fail2ban, unattended-upgrades

The steps the script prints at the end repeat Phase 3 (`git clone`,
`python3.11 -m venv`, `uv sync`) and are outdated in the same way; its step 1
names `/home/pm/.ssh/authorized_keys`, while the script itself gives `pm` the
home `/opt/pharmacy-monitor`.

## Phase 3 — Deploy code

> ⚠️ Outdated (marked 2026-10-11). This builds a git checkout on the server
> with `uv` inside its venv. The current server is not that:
> `/opt/pharmacy-monitor` has no `.git`, and `.venv/bin` has neither `uv` nor
> `pip` (read from the server 2026-10-11; a `uv` binary does exist for `pm`, in
> `/opt/pharmacy-monitor/.local/bin`). The release path there is `deploy.yml`
> (Phase 8), which copies `src/`, `frontend/`, `migrations/versions/`,
> `infra/systemd/` and `infra/scripts/` and builds the frontend. It does not
> install Python dependencies: after a change to `pyproject.toml` they are
> installed by hand (`CLAUDE.md`, "Useful commands"). How the venv on the
> current server was first built is not recorded.

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

# Install the versioned schedule: the scrape, watchlist and rematch units plus
# every drop-in under infra/systemd/overrides/, then enable their timers.
# This is required after a release that changes infra/systemd: the final
# ``zz-*`` overrides reset the schedule and ExecStart of older drop-ins instead
# of relying on undocumented files in /etc. (Until 2026-10-11 this comment
# said "the versioned daily cadence" and "it neutralizes old weekly /
# --no-alerts drop-ins"; the full scan is weekly now, and the aptekonline
# drop-in is itself weekly.)
/opt/pharmacy-monitor/infra/scripts/install_systemd_schedule.sh

# Enable remaining services.
systemctl enable --now pharmacy-monitor-api.service
systemctl enable --now pharmacy-monitor-health.timer
systemctl enable --now pharmacy-monitor-backup.timer
```

> ⚠️ Found 2026-10-11, reading the repository and the server:
>
> - Nothing above starts the frontend. `infra/systemd/` has no
>   `pharmacy-monitor-frontend.service`; on the server that unit exists only in
>   `/etc/systemd/system/`. On a machine built by this phase nothing serves the
>   dashboard, and `deploy.yml` checks that unit in its preflight, so it would
>   stop there (read from the workflow, not tried).
> - Three timers that are enabled on the server are enabled neither by the
>   commands above nor by `install_systemd_schedule.sh`:
>   `pharmacy-monitor-intraday.timer`, `pharmacy-monitor-digest-weekly.timer`
>   and `pharmacy-monitor-scrape-watcher.timer`. Beyond this and the frontend,
>   the command block above was not re-checked.
> - `deploy.yml` copies `infra/systemd/` to `/opt/pharmacy-monitor`, not to
>   `/etc/systemd/system/`: installing a changed unit is still a separate step
>   for root, and on 2026-10-11 it had not been taken. The server's
>   `pharmacy-monitor-rematch` units were the old ones (`rematch --reset`,
>   Monday 05:00). The unit last started on 2026-10-05, before the refusal
>   was deployed; the code on the server now refuses that command, so from the
>   next start on it should neither reset pairs nor run a scheduled `rematch`
>   — read from the deployed code, not yet observed. `docs/RUNBOOK.md`, «Выкладка правок
>   сопоставления», has the command that replaces the unit.
>   The scrape service drop-in from Git (`zz-realtime-alerts.conf`) was not
>   installed either; the aptekonline timer drop-in was.

Rewritten 2026-10-11 from `src/cadence.py`, the units in `infra/systemd/` and
the timers on the server; this paragraph used to say "daily full scans run at
01:00 (Pharmonline), 02:00 (Aptekonline), and 03:00 UTC (Aloe)". The full
catalog of each site is collected once a week, in the night to Monday. The
timers are not all weekly: the Aloe and Pharmonline timers fire every night at
01:00, the Aptekonline timer on Mondays at 02:00 — server time (Europe/Berlin),
not UTC. On a night when the site has already been collected this week, `run`
exits without scraping; a failed scan is therefore retried the next night for
Aloe and Pharmonline, and a week later for Aptekonline. Pharmonline has a second
path, a GitHub Actions workflow on Mondays. `src/cadence.py` is the single
source for the rhythm; the block «РАСПИСАНИЕ СБОРА» in `CLAUDE.md` explains it.

⚠️ Note of 2026-10-13: the paragraph above describes the weekly rhythm. With the
change of 2026-10-13 the full catalog of every site is collected every night
(two alarms, 01:00 and a retry at 03:15 server time), the Aptekonline weekly
drop-in is gone from the repository and the Monday GitHub Actions schedule for
Pharmonline is removed. On the server this holds only after the change is
deployed and root has removed the drop-in from `/etc` and installed the timer —
`docs/RUNBOOK.md`, «Ежедневный сбор: что стоит на сервере». Note that
`install_systemd_schedule.sh` below does not remove drop-ins from `/etc`.

The scheduled scans evaluate real-time alert rules but deliberately do not
send the bulky HTML/Excel scrape report. The priority watchlist is a separate
curated list of confirmed URLs and runs every three hours; it can notify only
about a local price change, never publish a partial cross-site recommendation.

For Pharmonline, Decodo remains the default verified transport. Firecrawl is
an optional, whole-catalog fallback for the autonomous workflow after all
Decodo retries fail. Add `FIRECRAWL_API_KEY`,
`PHARMONLINE_PUBLIC_API_FIRECRAWL_FALLBACK=required`, and a bounded
`PHARMONLINE_FIRECRAWL_MAX_REQUESTS=140` only after a separately approved
production configuration change. Do not replace Decodo or enable keyless mode
in the production EnvironmentFile.

## Phase 7 — Caddy reverse proxy

> ⚠️ Outdated (marked 2026-10-11). `infra/Caddyfile` has no domain to fill in
> any more: the production block names `leaddrive.cloud` and
> `www.leaddrive.cloud` directly, and `DOMAIN.example.com` survives only in a
> commented-out pilot block. And `systemctl reload
> caddy` is the wrong command for this file: it sets `admin off`, and by
> `CLAUDE.md` and `docs/RUNBOOK.md` reload fails under that setting — use
> `systemctl restart caddy` (`admin off` is in the server's Caddyfile too; the
> reload itself was not tried on 2026-10-11). The server's `/etc/caddy/Caddyfile`
> is not a copy of the file in Git and `deploy.yml` does not touch it.

```bash
# As root:
nano /opt/pharmacy-monitor/infra/Caddyfile  # set DOMAIN.example.com → real domain
cp /opt/pharmacy-monitor/infra/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
# Caddy auto-issues Let's Encrypt cert in ~30s
```

## Phase 8 — GitHub Actions deploy

A push or a merge into `main` does not start a deploy. It runs
`ci-pipeline.yml` (lint, tests, frontend) and nothing after it. The release
workflow, `.github/workflows/deploy.yml`, has one trigger, `workflow_dispatch`,
so it runs only when someone starts it:

```bash
gh workflow run deploy.yml --ref main -f apply_migrations=false
```

The workflow does not look at CI — start it once CI is green on the commit
being deployed. It copies code from its own checkout with `rsync` and runs no
`git` command on the server: `/opt/pharmacy-monitor` there is a copy without
`.git` (checked 2026-10-11). `infra/deploy.sh`, which updated the server with
`git reset --hard origin/main`, has not been called by any workflow since
`deploy.yml` was rewritten on 2026-05-30, and was removed on 2026-10-11 — from
the repository and, the same day, from the server, where a copy dated
2026-05-15 had remained.
The preflight checks, the `apply_migrations` switch and the cautions about when
to start are in the workflow itself and in `docs/RUNBOOK.md`, in the note that
opens the section «Обновления / деплой нового кода» (the code block under that
note is the old procedure) — read them there, not in a copy here.

`deploy.yml` is the release path, but it is not the only workflow that runs a
checkout's code on the server. As of 2026-10-11, at least:

- Three hand-started workflows overwrite live code under
  `/opt/pharmacy-monitor/src`: `category-hotfix-deploy.yml`,
  `health-pharmonline-hotfix-deploy.yml` and
  `activate-pharmonline-public-api-autonomy.yml` (the last one also
  `migrations/`, `templates/` and `alembic.ini`).
- Four workflows copy their checkout's `src/` into a temporary directory under
  `/opt/pharmacy-monitor` and run it from there, leaving the live code as it
  was. One of them, `autonomous-pharmonline-decodo-public-api.yml`, runs on a
  weekly schedule from `main`, after checking that CI passed on that commit —
  so for that scrape a merge into `main` already is the release
  (`docs/RUNBOOK.md`, «Выкладка правок сопоставления»). (With the change of
  2026-10-13 its schedule is removed; it runs only when started by hand, and
  the sentence still holds for such a run.) None of the four runs
  `alembic upgrade`: `recover-pharmonline-decodo-public-api.yml` reads the
  database revision (`alembic current`, `alembic heads`) and no longer
  upgrades it. `deploy.yml` is the only workflow that runs `alembic upgrade`
  against the production database. That is a statement about Alembic, not
  about the schema: two of the four start the CLI's `run`, and the CLI's
  `init_db()` calls `create_all` on PostgreSQL (`CLAUDE.md`, «Модели и
  миграции сверяет CI»).

The second item was re-counted later on 2026-10-11, after PR #49 removed nine
workflows that day: it had said six workflows, and that the recover workflow
upgraded the production database. This is not an account of all 13 workflows
that remain; read a workflow before starting it.

One secret is involved (GitHub repo settings → Secrets and variables →
Actions):

| Secret | Value |
|---|---|
| `SSH_PRIVATE_KEY` | private key the workflows use to log in to the server as `pm` |

It is the only secret the repository has (`gh secret list`, 2026-10-11). The
server address and its SSH host key are not secrets: they are written into
each workflow that connects to the server, and the key is kept in
`infra/prod_known_hosts`. Never take the host key from the network
(`ssh-keyscan`, `StrictHostKeyChecking=accept-new`):
`tests/test_prod_host_pinning.py` fails on either in `.github/`, `infra/` and
`scripts/`.

`deploy.yml` restarts the two services as `pm` through `sudo -n` and checks
first that this is allowed (step "Preflight least-privilege service restart").
The one-time rule it expects on the server, from the workflow header:

```
# /etc/sudoers.d/pm-deploy
pm ALL=(root) NOPASSWD: /usr/bin/systemctl restart pharmacy-monitor-api.service, /usr/bin/systemctl restart pharmacy-monitor-frontend.service
```

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

> ⚠️ Outdated (marked 2026-10-11). The code steps below cannot work on the
> current server: `/opt/pharmacy-monitor` is a copy without `.git` (checked
> 2026-10-11), so `git log` and `git reset --hard` fail there. Where
> `docs/RUNBOOK.md` describes taking code back, it goes through `main` and
> `deploy.yml`: revert the merge, wait for green CI, deploy («Выкладка правок
> сопоставления», the rollback steps), or a new commit through a PR (the note
> under «Restore drill»). `deploy.yml` refuses a commit that does not contain
> the production database's migration revision, and it leaves on the server
> any file a release added under `src/` (that `rsync` runs without `--delete`).
> Deploying an old ref runs that ref's own `deploy.yml`, which calls Alembic
> from the live directory; that is why `deploy.yml` never replaces
> `migrations/env.py` there (`docs/RUNBOOK.md`, «Откат выкладкой прошлого
> коммита»).
>
> The database step at the end does not match the server either (read
> 2026-10-11): every dump in `/var/backups/pharmacy-monitor/` is GPG-encrypted
> and named `pharmacy-monitor-<timestamp>.sql.gz.gpg`, so there is no `.sql.gz`
> file for `gunzip -c` to read. Do not adapt that line by adding a decrypt
> step: `backup.sh` dumps with `--clean`, so the stream begins by dropping
> every table of the database it is piped into. `docs/RUNBOOK.md`, «Копия
> бэкапа вне сервера», restores into a separate empty database and says the
> restore has never been tried on this server. The `alembic downgrade -1` line
> was not re-checked.

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

> ⚠️ Outdated (marked 2026-10-11). Grafana is not Grafana Cloud: it runs on the
> server itself behind Caddy, at `/grafana` (`docs/RUNBOOK.md`,
> «Observability»; `grafana-server` and `prometheus` were active on
> 2026-10-11). No Uptime-Kuma unit or container was found on the server that
> day; whether one runs elsewhere is not recorded. The Sentry line was not
> re-checked beyond the fact that `SENTRY_DSN` is set on the server.

- **Sentry** — `https://sentry.io/organizations/<your-org>/issues/`
- **Grafana Cloud** — `https://<your-org>.grafana.net/`
- **Uptime-Kuma** — typically `https://uptime.<your-domain>/`

## Cost breakdown (monthly)

> ⚠️ Outdated (marked 2026-10-11). The table is the estimate made for the
> Hetzner install and was not re-priced. Its first two rows no longer exist:
> the server is a Contabo VPS, and there is no object storage — the nightly
> dump stays on the server's own disk, B2 is not configured (`docs/RUNBOOK.md`,
> «Копия бэкапа вне сервера»). The repository records no price for the Contabo
> VPS. "Add ~€50/mo if proxy plan needed" is no longer an "if": by the comment
> in `src/cadence.py` (2026-10-04) Aptekonline can only be collected through a
> paid exit, about $49 a month for a weekly scan against $149 for a daily one.
> Other proxy prices are scattered through the repository (`.env.example`,
> `CLAUDE.md`); which of them are still paid was not established. The Telegram
> bot is not set up (`TELEGRAM_BOT_TOKEN` is empty on the server). The Resend,
> Sentry and domain rows were not re-checked.

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
