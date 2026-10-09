# Pharmacy Monitor — Production Overview

12-week roadmap delivered. This is the master index of what's where.

## Architecture (final)

```
                            Internet (HTTPS)
                                 │
                          [Caddy:443] auto-SSL
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
   Next.js 14            FastAPI:8080         Telegram bot (long-poll)
   (mobile-first SSR)    REST + JWT cookie    Magic-link bind, /alerts
        │                        │                         │
        └────────────────────────┼─────────────────────────┘
                                 ▼
                         PostgreSQL 16 (RLS-ready)
                              + Redis
                                 ▲
                                 │
                       Scraper (systemd timers)
                       Playwright + proxy + stealth
                                 │
                                 ▼
                    Sentry · Prometheus · Grafana
                       (errors)  (metrics)  (dash)
                                 │
                          Uptime-Kuma (ext probe)
```

## Roadmap status

| Week | Deliverable | Tests |
|---|---|---|
| **W1** | Hetzner deploy infra: docker-compose, Alembic, sqlite_to_pg, 6 systemd units, Caddyfile, CI/CD, runbook | alembic chain ✓ |
| **W2** | FastAPI: 31 routes, JWT cookie auth, magic-link, Pydantic validation, dashboard data endpoints | 19 ✓ |
| **W3** | Multi-tenant code: `tenant_id` in 5 models, `tenancy.py` (ContextVar + scoped queries), Alembic 0002 | 7 ✓ |
| **W4** | Next.js scaffold: App Router, TS, Tailwind+shadcn, mobile-first nav, auth pages | (skeleton) |
| **W5** | Comparison page: search debounce, reject UI, optimistic update, e2e Playwright | (frontend) |
| **W6** | Watchlist CRUD page | (frontend) |
| **W7** | Analytics 4 sections: match quality, brand share (Recharts), price index, forecast | (frontend) |
| **W8** | Alerts feed + Categories + Settings + logout | (frontend) |
| **W9** | Notifications: Telegram bind, dispatch (severity+quiet hours), email digest CLI+systemd, Settings UI prefs, Alembic 0003 | 19 ✓ |
| **W10** | Scrape robustness: proxy support, UA rotation (9), stealth patches, captcha detection, smart retry | 18 ✓ |
| **W11** | Monitoring: Sentry init+filters, Prometheus 9 metrics + /metrics endpoint, structured logging, Grafana dashboard JSON | 17 ✓ |
| **W12** | i18n (RU/AZ/EN cookie-based + LocaleSwitcher), k6 load test, Security audit checklist, Go-live runbook | (next) |

**Total backend tests: 80 passing.**

## Files added across 12 weeks

### Infrastructure
```
docker-compose.yml                              W1
.env.example                                    W1
alembic.ini                                     W1
migrations/env.py + script.py.mako              W1
migrations/versions/0001_initial.py             W1
migrations/versions/0002_tenant_id.py           W3
migrations/versions/0003_notif_prefs.py         W9
scripts/sqlite_to_pg.py                         W1
infra/Caddyfile                                 W1
infra/deploy.sh                                 W1
infra/scripts/backup.sh                         W1
infra/scripts/initial_server_setup.sh           W1
infra/systemd/pharmacy-monitor-api.service      W1
infra/systemd/pharmacy-monitor-scrape@.service  W1
infra/systemd/pharmacy-monitor-scrape@.timer    W1
infra/systemd/pharmacy-monitor-health.service   W1
infra/systemd/pharmacy-monitor-health.timer     W1
infra/systemd/pharmacy-monitor-backup.service   W1
infra/systemd/pharmacy-monitor-backup.timer     W1
infra/systemd/pharmacy-monitor-digest@.service  W9
infra/systemd/pharmacy-monitor-digest@daily.timer  W9
infra/systemd/pharmacy-monitor-digest@weekly.timer W9
infra/systemd/pharmacy-monitor-telegram-bot.service W9
infra/grafana/dashboard.json                    W11
infra/prometheus.yml                            W11
.github/workflows/ci.yml                        W1
.github/workflows/deploy.yml                    W1
```

### Backend
```
src/api.py                  rewritten — 31 routes, JWT, CORS, /metrics, prefs   W2/W11
src/storage.py              + tenant_id in 5 models + 6 notif_prefs columns      W3/W9
src/tenancy.py              ContextVar + scoped() + assert_same_tenant            W3
src/notifications.py        dispatch_event + digests                              W9
src/observability.py        Sentry + Prometheus + helpers                          W11
src/logging_setup.py        structured JSON logs + file rotation                   W11
src/scrapers/base.py        + proxy + stealth + captcha + smart retry              W10
src/scrapers/anti_detection.py  9 UAs + viewport jitter + stealth_js               W10
src/scrapers/captcha.py     captcha detection (DOM + text patterns)                W10
src/telegram_bot.py         /start <code> binds the chat (code from the dashboard) W9
src/telegram_binding.py     one-time bind codes, attempt limit                     2026-10
src/main.py                 + notify digest CLI + smoke-test in run_cmd            W9
src/matcher.py              + metrics injection                                    W11
src/alerts.py               + metrics injection                                    W11
src/notifier.py             graceful skip without SMTP                             pilot
```

### Frontend (Next.js)
```
frontend/package.json                                                   W4
frontend/tsconfig.json, next.config.mjs, tailwind.config.ts             W4/W12
frontend/i18n.ts                                                        W12
frontend/messages/{ru,az,en}.json                                       W12
frontend/src/i18n/{config,request}.ts                                   W12
frontend/src/lib/{api,utils,use-debounce}.ts                            W4/W5
frontend/src/components/{nav,providers,locale-switcher}.tsx             W4/W12
frontend/src/app/layout.tsx + page.tsx                                  W4/W12
frontend/src/app/login/page.tsx                                         W4
frontend/src/app/auth/verify/page.tsx                                   W4
frontend/src/app/api/locale/route.ts                                    W12
frontend/src/app/(dashboard)/layout.tsx                                 W4
frontend/src/app/(dashboard)/comparison/page.tsx                        W5
frontend/src/app/(dashboard)/overview/page.tsx                          W4
frontend/src/app/(dashboard)/watchlist/page.tsx                         W6
frontend/src/app/(dashboard)/analytics/page.tsx                         W7
frontend/src/app/(dashboard)/alerts/page.tsx                            W8
frontend/src/app/(dashboard)/categories/page.tsx                        W8
frontend/src/app/(dashboard)/settings/page.tsx                          W8/W9/W12
frontend/e2e/smoke.spec.ts + README.md                                  W5
frontend/playwright.config.ts                                           W5
frontend/README.md                                                      W4
```

### Tests
```
tests/test_api.py              19 tests   W2
tests/test_tenancy.py           7 tests   W3
tests/test_notifications.py    19 tests   W9
tests/test_scraper_robustness.py 18 tests W10
tests/test_observability.py    17 tests   W11
                              ──────────
                              80 / 80 ✓
```

### Docs
```
docs/PILOT_OPERATIONS.md       pilot ops + scrape robustness        pilot/W10
docs/DEPLOY.md                 Hetzner deployment runbook           W1
docs/MONITORING.md             Sentry/Prometheus/Grafana/Uptime     W11
docs/SECURITY.md               Pre-go-live audit checklist          W12
docs/GO_LIVE.md                Cutover runbook + rollback           W12
docs/PRODUCTION_OVERVIEW.md    This file                            W12
load-test/api.k6.js + README   k6 scenarios + thresholds            W12
```

## Stack (frozen for v1.0)

| Layer | Tech | Version |
|---|---|---|
| Backend framework | FastAPI | 0.136+ |
| ORM | SQLAlchemy | 2.0 |
| Migrations | Alembic | 1.13+ |
| DB | PostgreSQL | 16 |
| Cache / rate-limit | Redis | 7+ |
| Scraper | Playwright | 1.48+ |
| Auth | python-jose (JWT) + magic-link | latest |
| Email | SMTP via Resend (default) / Gmail | — |
| Notifications | python-telegram-bot via raw HTTP | — |
| Frontend | Next.js | 14 (App Router) |
| UI | Tailwind + shadcn/ui + Radix primitives | latest |
| Charts | Recharts | 2.13+ |
| State / data | TanStack Query | 5.59+ |
| i18n | next-intl | 3.25+ |
| Errors | Sentry SDK | 2+ |
| Metrics | prometheus-client | 0.21+ |
| CI/CD | GitHub Actions → SSH deploy | — |
| Hosting | Hetzner CX22 (4 vCPU, 8GB, ~6€/mo) | — |
| TLS / Proxy | Caddy | 2 |
| Process | systemd | — |

## Where to start when picking this up next

1. **Check pre-flight**: `docs/SECURITY.md` and `docs/GO_LIVE.md`
2. **Run tests**: `.venv/bin/pytest -q && cd frontend && pnpm test`
3. **Local dev**: `docker compose up -d && uvicorn src.api:app --reload &  cd frontend && pnpm dev`
4. **Production deploy**: `bash infra/scripts/initial_server_setup.sh` on Hetzner, then `bash infra/deploy.sh`

## What's NOT done (out of scope for v1.0)

- ERP integration (client API spec needed) — deferred to Phase 4
- Native mobile apps (PWA suffices)
- Marketplace integrations (Wildberries / Amazon)
- AI-powered recommendations (need 6+ months of data first)
- Alertmanager rules in Prometheus (Sentry covers errors, this is for metric thresholds)

These are all explicitly deferred. The system as-is can run in production for at least
6 months without architectural changes.
