# Pharmacy Monitor — Architecture (Phase 6.2)

Last updated: 2026-05-28

Comprehensive overview за после Phase 6.1 (URL-based i18n) и установки 9 MCP stack. Целевая аудитория: новый разработчик за 30 мин понимает систему до уровня "могу безопасно изменять компоненты".

---

## 1. System diagram

```
                                ┌─────────────────────────────┐
                                │   3 AZ pharmacies (target)   │
                                │  pharmonline / aptekonline  │
                                │             / aloe          │
                                └──────────────┬──────────────┘
                                               │ HTTP/WSS
                  ┌─────────────────────────────┼─────────────────────────────┐
                  │                             │                             │
                  ▼                             ▼                             ▼
       Hetzner cx33 prod (DE)        Mac dev/diagnostics            User browsers
       ┌──────────────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
       │  pharmonline DDP     │      │  aptekonline httpx   │      │  Chrome / Safari     │
       │  (Meteor WebSocket)  │      │  (no scrape runtime) │      │  (RU / AZ / EN)      │
       │  via IPRoyal proxy   │      │                      │      └──────────┬───────────┘
       │  → 01:00 + intraday  │      │  DB tunnel only      │                 │ HTTPS
       │                      │      │                      │                 │
       │  aloe RSC/HTTP       │      │  scrape launchd      │                 │
       │  direct connection   │      │  retired/disabled    │                 ▼
       │  → 03:00 + intraday  │      │                      │      ┌──────────────────────┐
       │                      │      └──────────┬───────────┘      │  Namecheap DNS       │
       │  AI crawler          │                 │ SSH tunnel        │  leaddrive.cloud → A │
       │  (Claude API fallback)│                │ for diagnostics   │                      │
       └──────────┬───────────┘                 │                   └──────────┬───────────┘
                  │                             ▼                              │
                  │                  ┌──────────────────────┐                  ▼
                  │                  │  prod Postgres :5432 │      ┌──────────────────────┐
                  │ asyncio          │  (DATABASE_URL)      │      │  Caddy :443 (TLS)    │
                  ▼                  └──────────────────────┘      │  reverse proxy        │
       ┌──────────────────────┐                  ▲                  └──────────┬───────────┘
       │  persist_results     │                  │                             │
       │  (diff-only,         │                  │ port 5432                   │
       │   chunked, batched)  │──────────────────┘                             │
       │  → Run / Product /   │                                                ▼
       │   PriceSnapshot      │                                  ┌─────────────────────────┐
       │   tables             │                                  │ Next.js :3000 (standalone)│
       └──────────┬───────────┘                                  │ output: 'standalone'     │
                  │                                              │ /[locale]/* routes       │
                  ▼                                              │ NO middleware (Phase 6.1)│
       ┌──────────────────────┐                                  └──────────┬───────────────┘
       │  matcher.py v2       │                                             │
       │  - barcode pass      │                                             │ /api/* /auth/*
       │  - secondary brand+  │                                             ▼
       │    pack              │                                  ┌─────────────────────────┐
       │  - tertiary brand+   │                                  │ FastAPI :8080            │
       │    dosage            │                                  │ JWT cookie auth          │
       │  - quaternary auto-  │                                  │ /api/v1/dash/*           │
       │    brand frequency   │                                  └──────────┬───────────────┘
       │  → Match cluster     │                                             │
       └──────────┬───────────┘                                             │
                  │                                                          ▼
                  ▼                                              ┌─────────────────────────┐
       ┌──────────────────────┐                                  │ Postgres 16              │
       │  analyzer + roi      │                                  │ - products (53k)         │
       │  - price_changes     │                                  │ - matches (cross-site)   │
       │  - new_products      │                                  │ - price_snapshots (M+)   │
       │  - undercuts (incl.  │                                  │ - alert_events           │
       │    margin-aware)     │                                  │ - tracked_products       │
       │  - price_raise       │                                  │ - pricing_config         │
       │  - MAP violations    │                                  └──────────────────────────┘
       │    (Phase 4.5)       │                                             ▲
       │  - assortment_gaps   │                                             │
       │  - promo_responses   │                                  ┌──────────┴───────────────┐
       │  → AlertEvents       │                                  │ Redis 7                  │
       │  → ROI cache         │                                  │ - intraday:rotation:idx  │
       └──────────┬───────────┘                                  │ - intraday:lock:site:*   │
                  │                                              │ - rate-limit (future)    │
                  ▼                                              └──────────────────────────┘
       ┌──────────────────────┐
       │  report.py           │
       │  → reports/*.html    │
       │  → reports/*.xlsx    │
       │  → SMTP (Resend) ✗   │
       │     not configured   │
       └──────────────────────┘
```

---

## 2. Component responsibilities

### Scrapers (`src/scrapers/`)

| Scraper | File | Approach | Why |
|---|---|---|---|
| pharmonline | `pharmonline_ddp.py` | Meteor DDP over WebSocket | Cloudflare blocks Playwright; DDP has different fingerprint |
| pharmonline (legacy) | `pharmonline.py` | Playwright DOM | DR fallback, обычно отключён через `PHARMONLINE_USE_DDP=1` |
| aptekonline | `aptekonline.py` | httpx JSON API | Reverse-engineered backend `/shop/productList?categoryId[]=N` |
| aloe | `aloe.py` | Playwright DOM, Next.js RSC parser | Next.js SPA, JSON-LD product schema in RSC chunks |
| ai_crawler | `ai_crawler.py` | Anthropic Claude API | Universal fallback when primary <50% baseline |

All scrapers return `ScrapedProduct` dataclass (`src/scrapers/base.py`):
```python
@dataclass
class ScrapedProduct:
    site: str
    external_id: str
    url: str
    name: str
    barcode: str | None  # Phase 2.1
    brand, manufacturer, category, dosage, pack_size, image_url, description
    price, discount_price, discount_percent, is_on_sale, promo_label
```

Proxy chain (`src/scrapers/base.py` + `aptekonline.py`):
1. **IPRoyal residential** (`geo.iproyal.com:12321`) — primary для pharmonline
2. **Bright Data Web Unlocker** — secondary
3. **ScraperAPI default pool** — 3rd
4. **Direct** — fallback

### Persist (`src/main.py:persist_results`)

Diff-only, chunked (200 продуктов в chunk), commit-per-result. Key behaviors:
- Pre-fetch existing products одним SELECT с `external_id.in_(...)` чанками
- Update existing: name, brand, manufacturer, url (Phase 4 fix), barcode (conditional), last_seen_at
- Insert new: full constructor
- Snapshot создаётся **только** если цена/discount/promo реально изменились (`_snapshot_payload_changed`)

Регрессионные тесты в `tests/test_persist_results.py` (15 tests).

### Matcher v2 (`src/matcher.py`)

Cross-site product clustering, 4 passes:
1. **Barcode** (Phase 2): exact match by `Product.barcode` → confidence=1.0
2. **Secondary**: name normalized + brand + pack_size buckets → fuzzy ratio threshold
3. **Tertiary**: brand + dosage buckets → broader fuzzy
4. **Quaternary**: auto-detected brand frequency (high-confidence brand groups)

Output: `Match` rows linking 2-3 `Product` rows across sites with `canonical_name`, `confidence`.

Respect `MatchRejection` table (manual user "this is wrong" feedback).

### ROI / Actions (`src/roi.py`)

5 action types для каждого tenant (Phase 4):
1. `price_raise` — клиент дешевле всех конкурентов, можно поднять
2. `undercut` — конкурент опустил ниже, нужна реакция (с margin-aware Phase 4.4)
3. `assortment_gap` — товар у конкурента, нет у клиента
4. `promo_response` — конкурент запустил акцию
5. `map_violation` (Phase 4.5) — клиент дешевле brand-floor (30d window, multi-source)

Persistent кэш в `roi_actions_cache` table (UNIQUE per tenant+client_site), invalidate on scrape success.

PricingConfig (Phase 4.1) — per-tenant thresholds: `raise_threshold_pct`, `undercut_threshold_pct`, `max_spread_pct`, `min_margin_pct`, `max_per_type`. Default seed: 5/3/80/10/10.

### Analyzer + alerts (`src/analyzer.py`, `src/alerts.py`)

After every scrape run:
- `_detect_price_changes` — diff vs previous run, threshold ±5% → AlertEvent
- `_detect_new_products` — products without prior snapshot
- `_detect_undercuts` — competitor cheaper than client (analyzer-level, separate from ROI)
- `_detect_price_drop` — significant price drops (alerts.py)
- `_detect_new_product` — first-time products

AlertEvents accumulate in DB. Dispatcher sends to email/Telegram if configured (Phase 5).

### Health (`src/health.py`)

`pharmacy-monitor health-check` CLI — hourly via systemd timer:
- `_check_site_drops` — product count drop > 50% от medians
- `_check_brand_coverage_drop` — каждый бренд должен быть на ≥2 сайтах
- `_check_zero_prices` — anomaly detection
- `_check_site_silence` — site не scraped > 30h

Returns `HealthReport` with severity-tiered issues. /health endpoint в API exposes summary.

### API (`src/api.py`)

FastAPI, JWT cookie auth. Endpoint families:
- `/api/v1/dash/*` — frontend (admin)
- `/api/v1/<resource>` — future ERP integration
- `/auth/*` — login/verify/password
- `/health`, `/metrics` — observability
- `/api/v1/dash/settings/pricing` GET/PUT — Phase 4.6 config
- `/api/v1/dash/settings/costs/import` POST — Phase 4.3 CSV upload
- `/api/v1/dash/matches/suggestions` — Phase 2.5 review queue

Middleware (in order):
1. CORS
2. Request-ID (Phase 0.5) — UUID4 per request, `X-Request-ID` header, structlog binding
3. Auth — JWT cookie decode

### Frontend (`frontend/`)

Next.js 14.2.18 App Router, output: 'standalone', deployed via systemd.

URL routing (Phase 6.1):
```
app/
├── layout.tsx           ← minimal passthrough <html><body>
├── page.tsx             ← / → /ru (default locale redirect)
├── globals.css
├── api/                 ← Next.js API routes (none currently)
└── [locale]/
    ├── layout.tsx       ← validate locale + setRequestLocale + NextIntlClientProvider
    ├── page.tsx         ← /<locale> → /<locale>/overview
    ├── login/
    ├── auth/verify/
    └── (dashboard)/     ← route group, shared dashboard layout
        ├── layout.tsx
        ├── overview/
        ├── comparison/
        ├── analytics/
        ├── matcher/
        ├── matches/review/
        ├── settings/(pricing/)
        ├── site/[site]/
        ├── watchlist/
        ├── alerts/
        └── categories/
```

NO middleware.ts — backward compat для `/comparison` → `/ru/comparison` через `next.config.mjs` `redirects()` (framework-level HTTP 307, не subject to standalone middleware rewrite recursion bug).

i18n: cookie fallback в `i18n/request.ts` для migration window, primary signal — URL via `requestLocale`.

State management: React Query (TanStack). Cache invalidation после mutations.

---

## 3. Data flow — typical day

### Nightly scrape pipeline (01:00-04:00 UTC)

```
01:00 UTC ─ pharmacy-monitor-scrape@pharmonline.timer fires
  ├── pharmacy-monitor run --site pharmonline --mode category
  ├── PharmonlineDDPScraper: connect via IPRoyal residential
  │     → categoryList → product list per category
  │     → ~25k unique product visits, ~272k page hits (1 product может быть в N категориях)
  ├── persist_results: diff-only, 5-15k snapshots written (depends on price changes)
  ├── matcher.v2 (barcode + secondary + tertiary + quaternary passes)
  ├── analyzer (price_changes, new_products, undercuts)
  ├── alerts.dispatcher (AlertEvent table writes, send to channels if configured)
  ├── report (HTML + XLSX в reports/)
  └── SMTP send (Resend) — silent no-op если не configured

02:00 UTC ─ pharmacy-monitor-scrape@aptekonline.timer fires
  └── server-side httpx JSON scrape via Decodo AZ residential proxy

03:00 UTC ─ pharmacy-monitor-scrape@aloe.timer fires
  └── server-side RSC/HTTP scrape, direct connection

04:00 UTC ─ pharmacy-monitor-backup.timer fires
  └── pg_dump → /var/backups/pharmacy-monitor/*.sql.gz.gpg (GPG encrypted)
```

### Intraday rotation (05:00–17:00 по времени хоста)

Часы в `OnCalendar` — местные для сервера. Прод стоит в Europe/Berlin, поэтому
летом тики идут в 03–15 UTC.

```
Every hour 05-17 ─ pharmacy-monitor-intraday.timer fires (Phase 5.1c)
  ├── pharmacy-monitor intraday-tick
  ├── src/intraday.py:top_volatile_categories(sites=INTRADAY_SITES)
  │     → SELECT count(snapshots) per category per site за 7d
  │     → только категории с разделом на сайте из INTRADAY_SITES
  │     → Top-30 by volatility
  ├── Redis GET "intraday:rotation:idx" → cats[i % len] (чья очередь)
  ├── Intraday site (INTRADAY_SITES = ['aloe']):
  │     - SETNX "intraday:lock:site:aloe" с TTL=2h
  │     - Pharmonline исключён: его Meteor WebSocket через residential proxy
  │       обслуживается только полным недельным прогоном
  │     - Если locked → skip tick, log "intraday_skipped reason=site_rate_limited",
  │       очередь остаётся у той же категории
  ├── Прогон взят → Redis INCR "intraday:rotation:idx"
  ├── pharmacy-monitor scrape --site aloe --category-id N --limit 600
  │     (только сбор: матчер, журнал алертов и ROI тик не трогает)
  └── alerts.local_price_alerts_for_partial_run → письмо admin/owner
        (падения цены, которые тик увидел сам; в базе не хранятся)
```

Result: 5–7 supplemental Aloe scrapes/day (13 тиков при лимите один прогон на
сайт в 2 часа); proxy-зависимые сайты не получают лишних intraday-подключений.

### User dashboard hit

```
User GET https://leaddrive.cloud/az/comparison
  → Caddy (TLS termination, HSTS)
  → Next.js :3000
  → middleware? нет (Phase 6.1)
  → app/[locale]/(dashboard)/comparison/page.tsx
  → useTranslations + React Query → /api/v1/dash/comparison?locale=az
  → Caddy /api/* path matcher
  → FastAPI :8080
  → JWT cookie decode → user context
  → Query latest_snapshots_per_product
  → translate_action(action, locale="az") применяет AZ строки
  → JSON response
  → React renders product table
```

---

## 4. Deployment topology

### Prod box (Hetzner cx33, Falkenstein DE)

```
Hardware: 4 vCPU, 8 GB RAM, 80 GB SSD · €7.99/мес · IP 46.225.149.52
OS: Ubuntu 22.04 LTS

Services (all systemd):
- pharmacy-monitor-api.service          → FastAPI :8080 (uvicorn, 2 workers)
- pharmacy-monitor-frontend.service     → Next.js :3000 (standalone)
- pharmacy-monitor-scrape@<site>.timer  → daily scrape
- pharmacy-monitor-scrape@<site>.service → triggered by timer
- pharmacy-monitor-intraday.timer       → hourly 05–17 по времени хоста (Phase 5.1c)
- pharmacy-monitor-intraday.service     → intraday-tick
- pharmacy-monitor-health.timer         → hourly
- pharmacy-monitor-backup.timer         → daily 04:00 UTC
- pharmacy-monitor-rematch.timer        → weekly Monday 05:00 UTC
- pharmacy-monitor-digest@daily.timer   → daily 05:00 UTC

Network:
- Caddy :80/:443 (auto-HTTPS via Let's Encrypt, HSTS preload)
- Postgres 16 :5432 (local-only, no external)
- Redis 7 :6379 (local-only)

Persistent state:
- /opt/pharmacy-monitor/ — app code + .venv
- /var/lib/postgresql/16/main/ — DB
- /var/backups/pharmacy-monitor/ — daily backups (~30d retention)
- /etc/pharmacy-monitor/env — secrets (DATABASE_URL, API_KEYS, JWT_SECRET)
```

### Mac developer machine

```
- pharmacy-monitor git clone (development)
- Persistent SSH tunnel Mac:5433 → prod:5432 (launchd, see infra/local/com.pharmacy-monitor.db-tunnel.plist)
- Mac scrape launchd jobs are retired and should stay unloaded/disabled. Local
  scrape scripts are fail-closed unless `PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1`
  is set for explicit DR.
- ~/.claude.json — 9 MCP servers user-scope
- Local Postgres optional (for dev DB)
```

### MCP integration

| MCP | Purpose | Cost |
|---|---|---|
| `perplexity-ask` | Anti-hallucination primary | $50 prepaid |
| `firecrawl` | Web scraping fallback, Cloudflare bypass | 1000 free/мес |
| `brave-search` | Independent search (cross-check) | 2000 free/мес |
| `openrouter-sonar` | Same Sonar models, OpenRouter | exhausted, need top-up |
| `memory` | Cross-session knowledge graph | local file |
| `postgres` | DB queries без SSH | via persistent tunnel |
| `github` | Native PR/issue/code-search | free PAT |
| `playwright` | Cross-browser E2E automation | local Chromium |
| `chrome-devtools` | Debug live Chrome | local |

---

## 5. Key invariants & gotchas

### Data layer

- **`Product.barcode` ≠ EAN-13 для pharmonline** — это 9-digit internal article number (pharmacy SKU). Cross-site matching через barcode не работает для AZ pharmacies в принципе. Matcher v2 priority-0 эффективен только когда несколько сайтов отдают тот же barcode (rare).
- **Diff-only persist** — snapshot создаётся только при price change. `Product.last_seen_at` обновляется КАЖДЫЙ run (canonical "видели сегодня").
- **`Run.products_scraped`** = page hits, не unique products. Уникальных по `external_id` будет меньше.
- **Scrape concurrency** — server watcher skips when any `pharmacy-monitor run`,
  `scrape`, `intraday-tick`, or `rematch` is active; matcher/rematch uses a
  Postgres advisory lock for `canonical_id` writes.

### Scraping

- **Pharmonline DDP**: WebSocket keep-alive фейлится во время persist phase (event loop blocked). Reconnect-on-close (Phase 1c.4) восстанавливает session.
- **Aloe scraper** — server-side RSC/HTTP parser reads Next flight data and uses
  product detail slugs; production run #382 verified `dermanlar` 480/480 pages.
- **Aptekonline** — server-side through Decodo AZ residential proxy; Mac launchd
  is no longer a runtime dependency.

### Frontend

- **NO middleware.ts** в standalone build (Phase 6.1) — иначе recursion bug. Backward compat исключительно через `next.config.mjs` `redirects()`.
- **Locale из URL params**, не cookie. Cookie остался как fallback для migration window — удалить через 30 дней (2026-06-28).
- **`<html lang="">`** = `defaultLocale` (ru) на root layout. `[locale]/layout.tsx` не может перезаписать — это cosmetic issue для SEO.

### Production state

- **`pm` user не имеет passwordless sudo** — для systemctl/sudo используй root через SSH.
- **`admin off`** в Caddyfile → `systemctl reload caddy` фейлится, use `restart`.
- **Backup retention** — 30 days в /var/backups/pharmacy-monitor/, не offsite (B2 user declined).
- **Sentry / Telegram / SMTP не configured** — alerts только в DB. Phase 5.4 заблокирован на alert channel setup.

---

## 6. Test coverage

```
~280 unit tests passing (pytest):
- test_persist_results.py (15) — diff-only, URL update bug regression, barcode
- test_matcher.py — barcode + name normalization clustering
- test_aloe.py, test_pharmonline_ddp.py, test_aptekonline_api.py — scraper unit
- test_roi.py (22) — actions, MAP violations, translation
- test_intraday.py (19) — rotation, Redis state, fail-open
- test_analytics.py, test_alerts.py, test_health.py — analysis
- test_api.py — endpoint smoke tests

Pre-existing failures (not blocking):
- test_scrapers_snapshot.py (4) — нужны network fixtures для pharmonline/aloe HTTP-snapshots
```

E2E coverage: **0 tests** — Phase 6.3 pending. С новым Playwright MCP можно драйвить через AI agent interactively сначала, потом codify как Playwright tests.

---

## 7. Roadmap state (post-2026-05-28)

### Completed
- Phase 0: defuse time bombs (request_id, deep /health, false-match cleanup)
- Phase 1: proxy chain (IPRoyal + Bright Data + ScraperAPI), DDP scraper, AI fallback
- Phase 1c: pharmonline DDP reverse-engineered + reconnect logic
- Phase 2: barcode + matcher v2 (limited utility — internal SKUs only)
- Phase 4: PricingConfig + cost CSV + MAP violations + settings UI
- Phase 5.1c: intraday category rotation (hourly business hours)
- Phase 5.5: frontend X-Request-ID propagation
- Phase 5.6: per-site staleness panel
- Phase 6.1: URL-based i18n (layout-based, no middleware)
- Phase 6.5: RUNBOOK updates

### Blocked
- Phase 5.4: Prometheus alerting — needs Prometheus install + alert channel (Telegram/SMTP)
- Phase 5.3: Grafana dashboards — needs Grafana install
- Phase 3: HA replica — needs 2nd Hetzner cx22 ($8/мес user decision)

### Pending
- Phase 6.3: Playwright E2E (5 scenarios)
- Phase 6.4: CI integration test (Postgres + Redis в docker)
- Task #33: Fix aloe scraper URL bug (production user-facing)
- Long-term: ERP integration (out of scope per client)

---

## 8. Quick decision tree — "where should this change go?"

```
                              Is this a new feature?
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
            data-related              UI-related              ops/infra
              │                        │                        │
          ┌───┴───┐               ┌────┴────┐              ┌────┴────┐
          │       │               │         │              │         │
       New      Existing       New page  Update existing  Systemd   Code
       SKU      SKU                                       timer     deploy
       field    field
          │       │               │         │              │         │
          ▼       ▼               ▼         ▼              ▼         ▼
       Alembic   Modify        app/        Modify       infra/      Add to
       migration scraper +     [locale]/   relevant     systemd/   src/
       + add to  persist       new/page.   page.tsx     *.service  + add
       Product   (update       tsx                                  test
       model     existing.X)
```

---

## 9. References

- [RUNBOOK.md](RUNBOOK.md) — operational procedures
- [DEPLOY.md](DEPLOY.md) — full deployment guide
- [PRODUCTION_OVERVIEW.md](PRODUCTION_OVERVIEW.md) — 12-week roadmap original
- [MATCHING-GUIDE.md](MATCHING-GUIDE.md) — matcher algorithms detail
- [ADR-001-multi-tenant.md](ADR-001-multi-tenant.md) — multi-tenancy decisions
- [SECURITY.md](SECURITY.md) — auth + secrets handling

External:
- next-intl docs: https://next-intl.dev/docs/routing
- next-intl issue #524: https://github.com/amannn/next-intl/issues/524 (Phase 6.1 retry rationale)
- Firecrawl API: https://www.firecrawl.dev/
- IPRoyal residential: https://iproyal.com/residential-proxies/
