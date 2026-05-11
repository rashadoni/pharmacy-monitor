# Security Audit Checklist

Run before go-live. Tick each box, paste output for evidence.

## Auth

- [ ] **JWT secret** is 32+ chars from `openssl rand -hex 32`, stored in `/etc/pharmacy-monitor/env` (mode 0600, owned by `pm`)
- [ ] **API key** (`PHARMACY_API_KEY`) is also 32+ chars random, separate value from JWT_SECRET
- [ ] Magic-link tokens expire in 30 minutes (verify in `src/tenants.py:issue_magic_token`)
- [ ] JWT cookie is `httpOnly` + `secure` (in production) + `sameSite=lax`
- [ ] Failed auth attempts are rate-limited per IP (10/min in `require_api_key`)
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
- [ ] GitHub Actions secrets (HETZNER_SSH_KEY) only used in `deploy.yml`, never echoed

```bash
# On server:
ls -la /etc/pharmacy-monitor/env
# Expect: -rw------- 1 pm pm

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
- [ ] No PII in journald logs (verify magic_token never logged in plaintext)
- [ ] File uploads (if any) — size-limited, MIME-checked
- [ ] CLI commands that delete data (`db-check --fix`, `inventory truncate`) require explicit `--confirm` flag (manual review)

```bash
# Verify Sentry PII setting:
grep -rn "send_default_pii" src/observability.py
# Expect: send_default_pii=False

# Verify magic_token not logged:
journalctl -u pharmacy-monitor-api --since "1 hour ago" | grep -i magic
# Should not show actual token values (only "magic_token_issued user=...")
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
