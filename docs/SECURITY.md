# Security Audit Checklist

Run before go-live. Tick each box, paste output for evidence.

## Auth

- [ ] **JWT secret** is 32+ chars from `openssl rand -hex 32`, stored in `/etc/pharmacy-monitor/env` (mode 0600, owned by `pm`)
- [ ] **API key** (`PHARMACY_API_KEY`) is also 32+ chars random, separate value from JWT_SECRET
- [ ] Magic-link tokens expire in 30 minutes (verify in `src/tenants.py:issue_magic_token`)
- [ ] Magic-link tokens are not kept in the database: `tenant_users.magic_token` holds the SHA-256 of the token, and no query carries the token (`tests/test_magic_link_token.py`). A 43-character value there is a dead token left by the code before 2026-10-10. How to check the live table — `docs/RUNBOOK.md`, «Ссылка из письма не входит»
- [ ] Emailed magic links keep the token out of every request address and header: the link carries it after `#`, a page served by the API (not by Next.js, which copies its own address into a request header) posts it in a request body, and only when its button is pressed — a link scanner that executes the page does not spend the link. `tests/test_login_link_in_a_browser.py` opens the links in a real browser and inspects everything the server received. `GET /auth/verify?token=…` is still accepted for tests and scripts; no letter uses it
- [ ] JWT cookie is `httpOnly` + `secure` (in production) + `sameSite=lax`
- [ ] Failed auth attempts are rate-limited per IP (10/min in `require_api_key`)
- [ ] Magic-link sign-in is rate-limited per client address: 10 attempts/min, counted before the token is checked (`verify:<address>`, one counter for `GET` and `POST /auth/verify`). The 11th gets 429 without a database query and the link stays usable. There is deliberately no refusals counter here (unlike `setpw_fail` / `login_fail`): it would fire after the query and only make a dead link look throttled. The counter is per client only while uvicorn takes the address from Caddy's `X-Forwarded-For` (unit flags `--proxy-headers --forwarded-allow-ips '127.0.0.1'`, also uvicorn's defaults); `--no-proxy-headers`, a proxy arriving from an untrusted address or a unix socket make it one counter for everyone. `tests/test_login_link_rate_limit.py` runs uvicorn with the unit's flags, with a `--no-proxy-headers` control
- [ ] User auth attempts are rate-limited per chat_id (5/min in `auth/request`)

```bash
# Verify cookie flags in response:
curl -i -X POST http://localhost:8080/auth/request \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com"}' \
  | grep -i set-cookie  # should not show pm_session here (only after /verify)

# Verify rate-limit:
for i in {1..15}; do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/v1/products; done
# Expect: 401 401 401 ... 429 429 ...
```

## Transport

- [ ] HTTPS only — Caddy auto-provisions Let's Encrypt cert, HTTP → 301 redirect
- [ ] HSTS header sent: `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- [ ] `X-Frame-Options: DENY` (no clickjacking)
- [ ] `X-Content-Type-Options: nosniff`
- [ ] `Referrer-Policy: strict-origin-when-cross-origin`
- [ ] No `Server: ...` header leak (Caddy `-Server` directive in Caddyfile)

```bash
# Run after deploy:
curl -sI https://your-domain.com | grep -iE "(strict-transport|x-frame|x-content|referrer|server)"
# Or use SSL Labs:
open "https://www.ssllabs.com/ssltest/analyze.html?d=your-domain.com"
# Target: A or A+ grade
```

## SQL Injection

All user input → SQLAlchemy ORM → parameterized queries. No raw SQL with f-strings anywhere.

- [ ] `grep -rn "execute(text(.*{.*}.*))" src/` returns 0 matches (no f-strings in raw SQL)
- [ ] `grep -rn 'f".*\.format(.*)"' src/api.py` returns 0 SQL-like results
- [ ] All `Session.scalar(select(...).where(Model.col == user_input))` — never f-string

```bash
# Optional: run sqlmap against staging
sqlmap -u "https://your-domain.com/api/v1/dash/comparison?search=test" \
       --cookie="pm_session=$JWT" \
       --batch --risk=3 --level=5
# Should report: "all tested parameters do not appear to be injectable"
```

## XSS

- [ ] All user-supplied text rendered through React JSX (auto-escaped) — never `dangerouslySetInnerHTML` with non-trusted content
- [ ] `grep -rn "dangerouslySetInnerHTML" frontend/src/` returns 0 matches
- [ ] Error messages from API don't echo user input verbatim into HTML

## CSRF

- [ ] JWT cookie is `sameSite=lax` (blocks cross-site POSTs)
- [ ] State-mutating endpoints (`POST/PATCH/DELETE`) require `pm_session` cookie OR `X-API-Key` header
- [ ] No CORS allow-origin `*` — explicit list in `CORS_ORIGINS` env

```bash
# Verify CORS:
curl -i -H "Origin: https://evil.com" \
  -H "Access-Control-Request-Method: POST" \
  -X OPTIONS https://your-domain.com/api/v1/dash/comparison
# Should NOT include "Access-Control-Allow-Origin: *"
```

## Secrets management

- [ ] No secrets in git history (`git secrets --scan` or `gitleaks`)
- [ ] `.env`, `.env.local` in `.gitignore`
- [ ] Secrets only in `/etc/pharmacy-monitor/env` (mode 0600, owned by `pm:pm`)
- [ ] GitHub Actions secret `SSH_PRIVATE_KEY` — the key workflows use to log in to the server as `pm`, the only secret the repository has (`gh secret list`) — is passed to a step only through `env:` and written only to the key file, never printed to the log. Every workflow that connects to the server reads it, not `deploy.yml` alone: 20 of the 22 files in `.github/workflows/` on 2026-10-11

```bash
# On server:
ls -la /etc/pharmacy-monitor/env
# Expect: -rw------- 1 pm pm

# In workflows: which ones read the secret
grep -l 'secrets\.SSH_PRIVATE_KEY' .github/workflows/*.yml
# ...any that read it other than as an `env:` entry of that name
grep -n 'secrets\.SSH_PRIVATE_KEY' .github/workflows/*.yml | grep -v '^[^:]*:[0-9]*: *SSH_PRIVATE_KEY: \${{ secrets\.SSH_PRIVATE_KEY }} *$'
# Expect: empty
# ...and what the steps do with `$SSH_PRIVATE_KEY`
grep -nE '\$\{?SSH_PRIVATE_KEY' .github/workflows/*.yml
# Expect: only emptiness tests and redirects into ~/.ssh/id_ed25519

# In git:
git log -p -- .env 2>&1 | head  # should be empty
git log -p -- /etc/pharmacy-monitor/env 2>&1 | head  # should be empty
```

## DB

- [ ] PostgreSQL listens on `127.0.0.1` only (not 0.0.0.0)
- [ ] User `pm` has password 32+ chars
- [ ] Backups are encrypted at rest (Hetzner Object Storage encrypts by default)
- [ ] No SUPERUSER role for app user (just owns its own DB)
- [ ] Row-Level Security policies (Postgres) when multi-tenant goes live (currently single-tenant code-side)

```bash
# Verify Postgres binding:
ss -tlnp | grep 5432
# Should show: 127.0.0.1:5432 (not 0.0.0.0:5432)

# Verify pm user permissions:
sudo -u postgres psql -c "\du pm"
# Expect: "pm | Create DB" — but NOT "Superuser"
```

## Server

- [ ] SSH password auth disabled (`PasswordAuthentication no` in sshd_config)
- [ ] Root SSH login disabled (`PermitRootLogin no`)
- [ ] `pm` user has no `sudo` except whitelisted commands (see `/etc/sudoers.d/pm`)
- [ ] `ufw` only allows 22, 80, 443
- [ ] `fail2ban` active for sshd
- [ ] `unattended-upgrades` enabled for security patches

```bash
# Run on server:
ufw status
# Expect: Status: active. Allows 22, 80, 443

systemctl status fail2ban
# Expect: active (running)

cat /etc/ssh/sshd_config | grep -iE "(password|permitroot)"
# Expect: PasswordAuthentication no, PermitRootLogin no
```

## App-level

- [ ] Pydantic validation rejects malformed input (test with garbage payloads)
- [ ] No PII in Sentry events (`send_default_pii=False`)
- [ ] No PII in journald logs, and no login-link tokens either: the application's own log calls carry none, and an emailed link no longer puts its token into a request address or header, which is what Caddy's access log records (2026-10-12, in effect once deployed — `curl -s -o /dev/null -w '%{http_code}' https://leaddrive.cloud/auth/link` answers 200). A second layer is a filter in both of Caddy's logs — the site's `log` block and the global `log default`, where Caddy writes a second record for a request it could not serve (sample in `infra/Caddyfile`, checked against real Caddy by `tests/test_caddy_log_filter.py`): it blanks the value of credential-like query parameters and drops the `X-Api-Key` and `Next-Router-State-Tree` headers. It is a list of names and does not recognise a percent-encoded name. It is installed by root by hand — `grep -c 'format filter' /etc/caddy/Caddyfile` on the server (2 = both blocks, 0 = not installed, and a request that does carry `?token=…` is then recorded whole); steps in docs/RUNBOOK.md «Фильтр в журнале Caddy». uvicorn's request lines drop the query string since 2026-10-11 (`src/access_log.py`, docs/RUNBOOK.md «Журнал API: строка запроса скрыта»). Entries written before the deploy stay until rotation; the tokens in them expired after 30 minutes
- [ ] File uploads (if any) — size-limited, MIME-checked
- [ ] CLI commands that delete data (`db-check --fix`, `inventory truncate`) require explicit `--confirm` flag (manual review)

```bash
# Verify Sentry PII setting:
grep -rn "send_default_pii" src/observability.py
# Expect: send_default_pii=False

# Verify a login-link token is not logged — application log and both access logs.
# Send yourself a link, open it, then search for the token itself (the part of the
# link after `#token=`): Next.js percent-encodes the address it puts into a header,
# so a search for `token=` misses it, and scanners bring a `token=` of their own.
read -rs -p 'token: ' T; echo
journalctl -u pharmacy-monitor-api -u caddy --since '-1 hour' -o cat | grep -c -F -- "$T"
unset T
# Expect 0. On prod 2026-10-10, before the fix, a count by the word `token=` over 30 days
# gave 49, all from the Caddy access log: 41 entries held the token of the one link opened
# since September — in the Referer, the request URI, a redirect Location and the
# Next-Router-State-Tree header — and 8 were probes of a path this app does not serve.
# None came from the API journal; why, was not established.
```

## Dependencies

- [ ] `pip-audit` / `uv pip audit` reports 0 critical CVEs
- [ ] `npm audit` reports 0 high/critical
- [ ] Renovate / Dependabot enabled on the repo

```bash
# Backend:
.venv/bin/uv pip audit
# Frontend:
cd frontend && pnpm audit --prod
```

## Final smoke tests

- [ ] `curl https://your-domain.com/health` → 200
- [ ] Login flow works in incognito browser → no errors
- [ ] Logout actually clears `pm_session` cookie (verify in DevTools)
- [ ] `/api/v1/dash/comparison` without cookie → 401
- [ ] `/metrics` reachable from Prometheus VM only (firewall rule)

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Engineer | | | |
| Reviewer | | | |
| Client (PO) | | | |

**Audit performed**: `_____________________________`
**Next audit due**: 6 months from sign-off
