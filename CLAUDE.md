# Pharmacy Monitor — Working Context for Claude

This file is auto-loaded in every Claude Code session. Read it first.

## Verification Protocol (CRITICAL)

Before starting any task that involves code changes, you MUST:

1. **Analyze the request** and provide a brief summary of what you understood.
2. **Define a Verification Plan**: List a specific checklist of how you (and the user) will verify the changes.
3. **Wait for implicit or explicit confirmation** (or proceed if the plan is exhaustive).

If you cannot define a clear checklist for verification, **stop and ask for clarification**. Inability to define a checklist is a signal that the task requirements are ambiguous.

### Checklist Requirements

The Verification Plan must enumerate:

- **Unit tests** to run or write (e.g., `pytest tests/test_<module>.py -v`).
- **Manual verification steps** the user can perform (e.g., "open https://leaddrive.cloud/comparison and check prices show on all 3 sites").
- **Edge cases** to consider (e.g., empty input, network failures, concurrent runs, diff-only vs full-persist data).
- **Production impact** if any (DB migrations, systemd restarts, schema changes).
- **Rollback plan** for irreversible operations.

### When to skip

This protocol applies to tasks that change code. Trivial read-only operations (status checks, file reads, exploration) don't need a Verification Plan. Trivial doc updates (typos, minor wording) don't need one either, but anything affecting prod systems or business logic does.

### Visual verification — no screenshot lies

When confirming a UI change via screenshot (browser MCP, computer-use, screenshots, photos):

- **NEVER** assert «вот, X виден» without identifiable evidence — name the exact pixel/glyph/coordinate and describe what makes it identifiable
- If the element is <16px or visually ambiguous — **zoom tighter** before asserting
- Cross-check via accessibility tree: `find` returns `title` / `aria-label` attributes that survive in the DOM even when the visual glyph is too small to read
- If still unsure — say «не вижу — проверю иначе», NEVER produce a confident-but-wrong claim
- If a marker reads ambiguously at small sizes — **fix the rendering** (filled dot + ring beats a 9px emoji), do not claim it's «already visible»
- **Reason**: 2026-05-27 in another project Claude saw a 1-letter macro provenance marker and confidently called it the new ⏳ hourglass marker. User trusted the false positive until manually zooming and discovering nothing was there. False positives erode trust faster than missing features. A confident wrong answer is worse than «I can't clearly see X».

## Current production state (last updated: 2026-05-27)

**Live URL**: https://leaddrive.cloud (also www.leaddrive.cloud) — TLS via Let's Encrypt, auto-renew (cert valid until 2026-08-04)
**Login**: `admin` / `pharmacy2026`
**Server**: Hetzner cx33 (Falkenstein DE), 4 vCPU / 8GB / 80GB · €7.99/mo · IP `46.225.149.52`
**SSH**: `ssh -i ~/.ssh/id_ed25519 root@46.225.149.52` (root + pm users active; pm home is `/opt/pharmacy-monitor`, NOT `/home/pm`; `pm` does not have passwordless sudo — use root for systemctl/sudo ops)
**DNS**: leaddrive.cloud at Namecheap; A `@` and `www` → 46.225.149.52

### What's running
| Service | Status | Notes |
|---|---|---|
| Caddy :80/:443 | active | auto-HTTPS, HSTS preload, reverse proxy → /api/\* + /docs + /openapi.json + /auth/\* → :8080, else → :3000. `admin off` set → use `systemctl restart caddy` (reload fails). |
| FastAPI :8080 | active | uvicorn 2 workers, JWT auth via password endpoint |
| Next.js :3000 | active | standalone build at `/opt/pharmacy-monitor/frontend/.next/standalone` |
| Postgres 16 | active | DB `pharmacy_monitor`, user `pm` |
| Redis 7 | active | cache + future rate-limit |
| systemd timers | enabled | scrape@pharmonline 01:00, @aptekonline 02:00, @aloe 03:00, health hourly, backup 04:00 |

### Auth
- Password login via `POST /auth/login {login, password}` → JWT cookie `pm_session` (httpOnly)
- bcrypt hash stored in `/etc/pharmacy-monitor/env` as `ADMIN_PASSWORD_HASH`
- Magic-link flow (`/auth/request` + `/auth/verify`) still works as alternative

### Data
- Pilot data migrated: 2087 products, 83 cross-site matches, 14469 price snapshots, 357 categories
- Manual scrape works: `pharmacy-monitor run --mode category --category-id N`
- Auto cron-scrape starts daily 01:00 UTC

### Local secrets backup (NOT in git)
`data/.prod-secrets-DO-NOT-COMMIT` — has PG_PASS, JWT_SECRET, API_KEY (mode 0600)

## Active work-in-progress

**Just finished (2026-05-28 morning)**: **9 MCPs всё user-scope + persistent Postgres tunnel + overnight pipeline green + 4 commits pushed**.

### MCP stack (`~/.claude.json`, user-scope, во всех проектах):

| MCP | Purpose | Cost | Tools | Notes |
|---|---|---|---|---|
| `openrouter-sonar` | Existing Sonar (4 tools) | OpenRouter credits exhausted — нужен top-up | 4 (sonar_ask/reason/research/search) | Backup для cross-check, сейчас 402 |
| `perplexity-ask` | Anti-hallucination primary | $50 prepaid, ~$0.005-0.02/req | 1 (perplexity_ask) | Sonar Pro native API |
| `firecrawl` | Web scraping + extraction | Free 1000/мес (988 left) | ~24 (scrape, crawl, map, search, extract, monitor, agent, interact, browser_*) | **Cloudflare bypass работает для pharmonline** ✓ |
| `memory` | Persistent cross-session knowledge graph | Free local | 9 (create/search/delete entities, relations, observations) | JSON-граф local |
| `postgres` | Direct DB queries без SSH | Free local | 19 (pg_execute_query/sql/mutation, pg_manage_*, pg_monitor) | Через persistent SSH tunnel `:5433` |
| `github` | PRs/issues/code search natively | Free | ~40+ (create_pr, search_code, get_file_contents, push_files, list_*, manage reviews) | PAT, repo `rashadrahimov/pharmacy-monitor` |
| `brave-search` | Independent search engine для cross-check | Free 2000/мес | 6 (web/news/image/video/local/llm_context_search) | Заменяет dead openrouter-sonar |
| `playwright` | Cross-browser automation (E2E) | Free local (300MB Chromium) | 23 (browser_navigate/click/fill/snapshot/screenshot/evaluate, тaby/dialog/network) | Microsoft official |
| `chrome-devtools` | Debug live Chrome (Network/Console/Perf) | Free local | 30+ (navigate, click, list_network_requests, performance_*, lighthouse, take_heapsnapshot) | Google Chrome team official |

### Persistent SSH tunnel for Postgres MCP

- launchd plist: `~/Library/LaunchAgents/com.pharmacy-monitor.db-tunnel.plist`
- Tracked в проекте: `infra/local/com.pharmacy-monitor.db-tunnel.plist`
- Logs: `~/Library/Logs/pharmacy-monitor-db-tunnel.log`
- Auto-start при логине, auto-restart если SSH упадёт, ThrottleInterval=30s
- Management: `launchctl bootout|bootstrap gui/$UID/com.pharmacy-monitor.db-tunnel`

### Overnight pipeline verified 2026-05-28 (все 5 runs green):

| Run | UTC | Site | Dur | Products | Type |
|---|---|---|---|---|---|
| 110 | 01:02 | pharmonline | 28m | 261,519 | nightly timer |
| 111 | 02:04 | aptekonline | 65m | 89,375 | Mac launchd |
| 112 | 03:04 | aloe | 27m | 1,874 | nightly timer |
| 113 | 05:00 | pharmonline | 7m | 10,000 | **Phase 5.1 intraday ✓** |
| 114 | 06:00 | aloe | 5m | 64 | **Phase 5.1 intraday ✓** |

Backup 04:02 UTC ✓ (GPG encrypted, 10MB). /health up, db 0.97ms, redis 3.03ms, all sites < 4h fresh.

### Exposed API keys (rotate when convenient):
- Perplexity: `pplx-9JgqRvCJ...` — perplexity.ai/settings/api → Regenerate
- Firecrawl: `fc-d0f71c2c...` — firecrawl.dev/dashboard → API Keys → Regenerate
- Brave: `BSAMo35R...` — api-dashboard.search.brave.com/app/keys → Revoke + new
- GitHub PAT: `github_pat_11BZ7WQWY00X...` — github.com/settings/personal-access-tokens → revoke

After rotation, re-register via `claude mcp remove <name> -s user && claude mcp add ...`.

---

**Just installed (2026-05-28 0:00)**: **Perplexity MCP + Firecrawl MCP user-scope** для cross-check anti-hallucination и web scraping fallback.

**Perplexity MCP** (`perplexity-ask`):
- Config: `/Users/rashadrahimov/.claude.json` → `npx -y server-perplexity-ask`
- Tool `mcp__perplexity-ask__perplexity_ask` (после рестарта Claude Code)
- Дополняет существующий `mcp__openrouter-sonar__*` (4 tool'а через OpenRouter, но **credits exhausted — top up если нужен dual cross-check**). 
- $50 кредитов на Perplexity API. Стоимость ~$0.005-0.02/запрос.
- **First win**: за 2 запроса ($0.04) распарсили root cause Phase 6.1 i18n блокера — это [next-intl issue #524](https://github.com/amannn/next-intl/issues/524). См. task #31.

**Firecrawl MCP** (`firecrawl`):
- Config: `/Users/rashadrahimov/.claude.json` → `npx -y firecrawl-mcp`
- Free tier 1000 credits/мес (~1 credit per scrape, ~2 per extract). Billing 2026-05-28 → 2026-06-28.
- Tools после рестарта: `mcp__firecrawl__firecrawl_scrape/batch_scrape/crawl/map/search/extract/agent/interact`.
- **CRITICAL FINDING — Cloudflare bypass для pharmonline.az работает**. Real test: `korinfar-10-mg-100-heb-pliva-almaniya?lng=en` → status 200, full Markdown, LLM-prompt extracted `{name, priceAZN, manufacturer, barcode}` корректно. Это потенциальная альтернатива DDP/IPRoyal для конкретных use cases.
- **Применимо у нас для**:
  - AI crawler fallback (замена Anthropic API extraction — Firecrawl дешевле для этого)
  - Barcode backfill (25k+ pharmonline products без barcode)
  - Quality-control re-scrape подозрительных match'ей
- **НЕ применимо**: full daily scrape (25k+9k+1.8k products × 30 days = 1M+ credits, не влезет даже в Growth план).

**Attempted (2026-05-27 night, ROLLED BACK)**: **Phase 6.1 URL-based i18n** — full migration to `app/[locale]/(dashboard)/...` structure, next-intl middleware с `localePrefix: 'as-needed'`, locale-aware Router + Link через `createNavigation`.

- **Code passed local + prod build** (42 routes, middleware 39.2 KB)
- **Production rendered HTTP 500**: middleware emits `x-middleware-rewrite: http://localhost:3000/<path>` header → Next.js standalone тратит этот URL как HTTP proxy fetch к самому себе → infinite recursion / ECONNRESET. Root cause: `output: 'standalone'` build обрабатывает middleware rewrite через actual HTTP request к localhost:3000 вместо internal dispatch.
- **Attempted workarounds**: `alternateLinks: false` (не помог), `NODE_OPTIONS=--dns-result-order=ipv4first` (изменил ECONNREFUSED→ECONNRESET, recursion остался).
- **Rolled back**: восстановлен tar backup `frontend-src-pre-i18n-20260527-195718.tgz`, cookie-based i18n работает. Локальные изменения в `git stash`: `Phase 6.1 i18n attempt — recursive proxy in standalone build`. Branch `wip/phase6.1-i18n-attempt` создан.
- **Для retry**: попробовать (1) `localePrefix: 'always'` (не уверен поможет, та же middleware mechanic), (2) убрать `output: 'standalone'` и развернуть полный `.next/`, (3) подождать апстрим-фикса в Next.js / next-intl для standalone + middleware rewrite.

**Just finished (2026-05-27 evening)**: **URL fix + Phase 4.5 MAP + Phase 5.5 X-Request-ID + Phase 5.1 intraday** (commits `4765620`, `7e5a541`, `1090db5`, `a95f1ca`):

- **URL update bug в `persist_results` (commit `4765620`)**: `existing.url` НИКОГДА не обновлялся → 6238 pharmonline-продуктов застряли на `https://pharmonline.az/product/True` (артефакт старого `postQuery` бага в DDP). Plus: `ScrapedProduct` dataclass даже не имел поля `barcode` (task #12 был неполон). Фикс: `existing.url = sp.url or existing.url` + `existing.barcode` conditional fill + добавлен `barcode` в `ScrapedProduct`. **Verified on prod: 6238 → 2** (99.97% fix rate, оставшиеся 2 — orphan products not re-scraped). +4 регрессионных теста в [tests/test_persist_results.py](tests/test_persist_results.py). Run 108 показал: 272k products в 51 минуту, persist завершил все URL'ы корректно.
- **Phase 4.5 — MAP violation detection (commit `7e5a541`)**: новый ROI action type `map_violation` — трекает min цены конкурентов per brand за 30д. Если клиент дешевле этого floor более чем на 5% — флагуем opportunity. Severity buckets: critical (≥20%), warning (≥10%), info (≥5%). Min 3 snapshot-семпла per бренд для валидного floor. Отличается от `_price_raise_opportunities` тем, что агрегат по БРЕНДУ (не один матч), 30-дневное окно (стабильнее). +5 регрессионных тестов в [tests/test_roi.py](tests/test_roi.py). Bonus: фикс pre-existing NameError в [src/analytics.py:75](src/analytics.py) (`by_brand[brand][site]` → `[site_name]`).
- **Phase 5.5 — Frontend X-Request-ID propagation (commit `1090db5`)**: каждый fetch генерит UUID v4 (crypto.randomUUID, Math.random fallback), отправляет как `X-Request-ID` header, backend echoes back, ApiError несёт `requestId` для support correlation. Новая helper `getRequestId(err)` для consumers. Round-trip verified: `curl -H "X-Request-ID: test" /health` возвращает тот же header.
- **Orphan runs cleanup**: run 97 (вчера Mac, 102k products, orphan "running") → marked `ok`; run 99 (утренний empty test) → marked `failed`. Дашборд status'ы теперь чистые.

Production state: run 108 OK (272018 products, 11 alerts, GPG-encrypted backup ~32MB). 25 726 pharmonline products с правильными URLs. /health: db_ping 0.62ms, redis_ping 2.62ms, all sites < 24h fresh.

**Just finished (2026-05-27)**: **Phase 2 — matcher v2 with barcode** (commit `97b7526`):

- **`products.barcode` column** (varchar(40), indexed) — Alembic migration `0007_product_barcode` применена на проде. Унифицирует EAN/GTIN/UPC в один canonical field. `ix_products_barcode` btree index для O(log n) lookup.
- **Barcode extraction** в 2 из 3 scraper'ов:
  - **aloe.py** — JSON-LD `gtin13`/`gtin14`/`gtin12`/`gtin8`/`gtin` priority order через shared helper `_extract_barcode_from_jsonld` в [src/scrapers/ai_crawler.py](src/scrapers/ai_crawler.py)
  - **pharmonline.py** — JSON-LD первым, HTML table regex fallback (`Ştrix kod`, `Штрих-код`, `Barcode`, `EAN`, `GTIN` patterns)
  - **aptekonline.py** — **SKIPPED**: listing JSON не содержит barcode, detail page удвоит Bright Data расходы. Когда нужно — отдельная batch-harvest job.
- **Matcher v2 priority-0 pass** ([src/matcher.py](src/matcher.py)): группирует продукты по barcode → cluster cross-site → confidence=1.0. Skip name heuristics (modifier conflicts, series numbers — barcode trumps name). Respect `MatchRejection` + per-unit price sanity. Logged: `matcher_barcode_pass unique_barcodes=N clusters_created=M`.
- **`scripts/rematch_with_barcode.py`** — bulk-break low-confidence (< 0.85, not `is_manual`) clusters когда their members disagree on barcode. Idempotent, dry-run by default, `--apply` коммитит и re-runs matcher для подбора новых pairs.
- **persist_results** ([src/main.py](src/main.py)) — копирует `sp.barcode` в `Product`, never overwrites existing non-null value (защита от scrape runs которые временно не имеют field'а).
- **26 unit tests**: 11 для barcode extraction, 7 для matcher barcode pass, 8 для rematch decision logic. Full suite: **386 pass**, 9 pre-existing failures без изменений.

**Что отложено**: Phase 2.5 (UI suggestion queue для unmatched borderline cases) — отдельная frontend сессия.

**Just finished (2026-05-27)**: **Phase 1 — scraper resilience**:

- **Bright Data Web Unlocker** ($1.50/CPM) — zone `pharmacy_unlocker`, native proxy mode (`brd.superproxy.io:33335`). Allowed IP whitelist: 46.225.149.52 (prod). Phase 1.2 код в [src/scrapers/base.py](src/scrapers/base.py) (`_brightdata_proxy_for`) + [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py) (`_brightdata_httpx_proxy_for`) подхватывает env `BRIGHTDATA_USERNAME/PASSWORD/SITES/HOST`.
- **aptekonline переехал на прод**: live test — 2264+431+111 продуктов из 3 категорий, все HTTP 200, ~30 сек. Лог: `aptekonline_using_brightdata`. Таймер `pharmacy-monitor-scrape@aptekonline.timer` enabled+started.
- **pharmonline остался на Mac**: Web Unlocker даёт HTTP 502 для pharmonline.az независимо от country (az/tr/ru tested). Прямой curl с Baku-IP даёт 200 → значит pharmonline блочит residential pool Bright Data. Open question: escalate в BD support или попробовать Smartproxy/Oxylabs. Pragmatic решение — Mac launchd для pharmonline остаётся.
- **AI crawler fallback** ([src/main.py](src/main.py)) — wired как опт-ин через `AI_FALLBACK_ENABLED=1` env. Срабатывает когда primary scraper отдаёт < 50% от baseline. Disabled by default — burns Claude tokens.
- **ScraperAPI default pool** (`SCRAPER_API_KEY` + `SCRAPER_API_SITES`) — устарел, остаётся как 3rd priority в chain.
- **Старый ISP zone `pharmacy_monitor`** — оставлен active в Bright Data (фикс $2/мес). Не удалён на случай если понадобится для будущих экспериментов. **TODO**: удалить через UI если решим что Web Unlocker достаточно.

**Just finished (2026-05-27)**: **Phase 0 — defuse time bombs** (commit `af63932`, roadmap ~/.claude/plans/rosy-launching-lamport.md):

- **`APTEKONLINE_CHECKUS` env-var** ([src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)) — захардкоженный bcrypt-токен из `main.js` перенесён в env с back-compat default. Fail-fast только когда переменная *задана но пустая* (защита от .env-опечатки). Default — текущее зафиксированное значение (2026-05-07). На проде env ещё не задан → fallback на default, всё работает. 5 unit-тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py).
- **Request-ID middleware** ([src/api.py](src/api.py)) — UUID4 per-request, bind в `structlog.contextvars`, response header `X-Request-ID`, Sentry tag через новый `sentry_set_request_id()` в [src/observability.py](src/observability.py). Принимает клиентский `X-Request-ID` если он soft-sanitized (128 ASCII chars max), иначе генерит свежий. 4 unit-теста.
- **Deep `/health` endpoint** ([src/api.py:HealthOut](src/api.py)) — добавлены `db_ping_ms` (`SELECT 1`), `redis_ping_ms` (`PING` если REDIS_URL задан), `sites[]` с `hours_since` per site через `max(Product.last_seen_at)`, `staleness_warning=true` если любой сайт > 30h. Status flips на `"degraded"` при DB unreachable / stale. Backward compat сохранён (старые поля на месте). 3 unit-теста + проверено на проде: aloe=16.6h, aptekonline=5.0h, pharmonline=5.15h.
- **`scripts/cleanup_false_matches.py`** — bulk-rejection 7 known false matches из CSV (`match_id, detach_product_id, reason`). Idempotent через `match_actions.break_match`. 5 unit-тестов. Запускать: `DATABASE_URL=... python -m scripts.cleanup_false_matches data/false_matches.csv --apply` (CSV ещё не подготовлен, нужно идентифицировать 7 пар через UI или SQL).
- **`infra/scripts/cleanup_caddy_backups.sh`** — portable bash (без GNU `find -printf` / `mapfile`), сортирует `.bak.*` по имени (filename = timestamp), удаляет старше N (default 3). Dry-run by default, `--apply` коммитит. Cron suggestion: `0 3 * * 0 ... --apply >> /var/log/caddy-cleanup.log`.

48/48 новых тестов проходят. 9 pre-existing failures в `test_roi.py` (compute_actions signature drift), `test_analytics.py` (NameError), `test_scrapers_snapshot.py` (snapshot fixture out of date) — не от Phase 0, отметить как backlog.

**Just finished (2026-05-26)**: **Full i18n audit + ROI translation**.

- **Locale switcher в сайдбаре**: `LocaleSwitcher` добавлен в `SideNav` ([frontend/src/components/nav.tsx](frontend/src/components/nav.tsx)) над кнопкой logout. Sidebar исправлен на `sticky top-0 h-screen self-start` + `min-h-0` на middle-секции — switcher теперь всегда видим при любой длине меню.
- **AZ locale не переключался**: `POST /api/locale` уходил к FastAPI (Caddy роутит `/api/*` → :8080 → 404). Роут перенесён в `/locale` ([frontend/src/app/locale/route.ts](frontend/src/app/locale/route.ts)). LocaleSwitcher обновлён. Куки устанавливаются через `response.cookies.set()` (не `cookieStore.set()` — ненадёжен в route handlers Next.js 14).
- **71 непереведённая строка** — полный аудит и фикс по всем страницам:
  - `overview/page.tsx` — KPI labels, duration strings (`formatDuration` принимает `t`)
  - `analytics/page.tsx` — все table headers, card titles, empty states, forecast description
  - `site/[site]/page.tsx` — table headers, pagination (prev/next/range), error/empty states, open-link title
  - `comparison/page.tsx` — th_name, th_spread, `{t("sites_spread", {n})}`
  - `notifications-banner.tsx` — все 5 строк через `useTranslations("banner")`
  - `alerts/page.tsx` — tooltip strings
  - Новые ключи добавлены в [frontend/messages/ru.json](frontend/messages/ru.json), [az.json](frontend/messages/az.json), [en.json](frontend/messages/en.json): пространства `analytics`, `banner`, `site`, `overview` (duration), `comparison` (sites_spread), `alerts` (tooltips)
- **ROI action recommendations переведены** (тексты шли из Python backend с hardcoded RU):
  - `src/roi.py`: добавлен `ALL_SITES`, `_STRINGS` (шаблоны title+detail для 4 типов: undercut/price_raise/assortment_gap/promo_response на AZ+EN), функция `translate_action(action, locale)`
  - `src/api.py`: `/api/v1/dash/roi/actions` принимает `?locale=ru|az|en`, применяет `translate_action` при отдаче, кеш `roi_actions_cache` не инвалидируется
  - `frontend/src/lib/api.ts`: `roiActions(client_site?, locale?)` передаёт `?locale=`
  - `overview/page.tsx` + `site/[site]/page.tsx`: `useLocale()` → locale в queryKey + queryFn

**Just finished (2026-05-07)**: HTTPS migration. `leaddrive.cloud` (Namecheap) → 46.225.149.52, Caddy with Let's Encrypt cert. Cookie host-only (no Domain attr) — same-origin, CORS not needed.

**Just finished (2026-05-07)**: AI crawler CLI command `pharmacy-monitor ai-crawl --site <s> --max-urls N [--dry-run] [--budget-usd N]` registered. Config via env: `ANTHROPIC_API_KEY`, `AI_CRAWL_PROVIDER=anthropic`, `AI_CRAWL_MODEL=claude-haiku-4-5`. Deployed on prod. **Sitemap discovery working — aloe returns 19,319 URLs.**

**Just finished (2026-05-07)**: aloe.az extraction fix. Added `parse_next_rsc_jsonld()` in [src/scrapers/ai_crawler.py](src/scrapers/ai_crawler.py) (Layer 1.5 between standard JSON-LD and LLM fallback) — decodes Next.js RSC chunks (`self.__next_f.push([1, "..."])`), unescapes the `__html` value from `dangerouslySetInnerHTML`, parses schema.org Product. Smoke test (50 URLs, dry-run): **25 products extracted, $0 LLM cost**. In Playwright mode the standard `parse_jsonld_product` actually catches it post-hydration (Next.js injects the `<script type="application/ld+json">` after JS runs); RSC parser is a safety net for raw HTML / failed hydration / future HTTP-only fetching. 7 unit tests in [tests/test_ai_crawler.py](tests/test_ai_crawler.py).

**Important corrections to prior architecture notes** (CLAUDE.md was inaccurate):
- **aloe.az is Next.js 13+ App Router with RSC streaming**, not Meteor.
- **pharmonline.az is server-rendered Bootstrap + jQuery**, not Meteor. Existing Playwright DOM scraper [src/scrapers/pharmonline.py](src/scrapers/pharmonline.py) works correctly; no hydration parser needed.

**Just finished (2026-05-09 → 2026-05-11)**: **Diff-only persist + analyzer refactor**. После того как первый Mac прогон (run_45) длился 2ч45 с full persist (100066 snapshots) и потом отчёт ronyalsya N+1 lazy-load'ом, переехали на:

- **`src/main.py:persist_results`** — chunked (`_PERSIST_CHUNK=200`) + diff-only через `_snapshot_payload_changed(last, sp)`. Snapshot пишется **только если цена/discount/promo реально изменились**. `Product.last_seen_at` обновляется всегда — это и есть «видели в этом прогоне».
- **`src/storage.py`** — два общих query helper'а:
  - `latest_snapshots_per_product(session, product_ids) → dict[int, PriceSnapshot]`: текущая цена per product, не привязанная к run_id. Замена `WHERE run_id == last_run` после diff-only.
  - `curr_and_prev_snapshots_for_run(session, current_run) → (curr_snaps, prev_by_product)`: стандартный паттерн для diff-детекторов. Фильтр `Run.started_at < current.started_at` устойчив к близким timestamp'ам.
- **`src/analyzer.py`** — `_detect_price_changes`, `_detect_new_products`, `_detect_undercuts` переписаны под `curr_and_prev_snapshots_for_run` и `latest_snapshots_per_product`. «Новый продукт» = нет snapshot до `run.started_at`. «Текущая цена» = latest globally.
- **Consumers обновлены** под ту же семантику: `src/api.py` (`/dash/comparison`, `/comparisons`), `src/alerts.py` (`_detect_price_drop`, `_detect_new_product`, `_prices_for_match`), `src/roi.py` (`_preload_snapshots`, `_assortment_gaps`), `src/health.py` (`_check_site_drops`, `_check_brand_coverage_drop` — через `Product.last_seen_at`).
- **`src/main.py:_smoke_test_per_site_coverage`** — baseline через `Run.products_scraped` (стабильный счётчик независимо от persist-режима), не `COUNT(snapshots) per run` (после diff-only давал ложные site_drop). Фикс orphan-баг: `AlertEvent` не имеет поля `run_id` — `dedup_key` уже содержит `run={run.id}`.
- **`src/alerts.py:DETECTORS`** — добавлен `"site_drop_smoke": _noop_detector` (events эмитятся из smoke_test напрямую, dispatcher просто пропускает).

Результат на live проде (run_48, 2026-05-09): **1965 snapshots vs 100066 (51× экономия)**, total прогон **1ч15 vs 2ч45**, report_saved без crash, dashboard `/comparison` отрисовывает цены через `latest_snapshots_per_product`. Daily Mac launchd (14:00 UTC) автономно отрабатывает, последние 3 ok-runs подтверждены.

8 diff-only regression-тестов в [tests/test_persist_results.py](tests/test_persist_results.py) + [tests/test_analyzer.py](tests/test_analyzer.py) — **252 теста проходят**.

**Just finished (2026-05-08)**: bulk-persist refactor в [src/main.py `persist_results`](src/main.py). Первый Mac launchd прогон (run_id=40) растянулся на **~5 часов** для **98 536 продуктов** — N+1 SELECT + per-row `session.flush()` через SSH-туннель к prod Postgres. Фикс: pre-fetch existing одной SELECT на категорию (`.in_(external_ids)` чанками по 500), batch `session.add_all()` + единый `flush()` для новых, bulk add snapshots, **commit per result** (bounds транзакции, разгружает WAL). 8 регрессионных тестов в [tests/test_persist_results.py](tests/test_persist_results.py), включая anti-N+1 счётчик SELECT'ов. Развёрнуто на проде. **Этот фикс был промежуточным шагом перед diff-only выше.**

**Just finished (2026-05-07)**: aptekonline JSON API rewrite + split runtime (Mac + prod).
- Aptekonline.az ввёл reCAPTCHA на любые headless-браузеры (даже с Baku-IP). Reverse-engineered backend endpoint `GET https://www.aptekonline.az/shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator, 100 items/page) — endpoint найден в `https://www.aptekonline.az/assets/js/main.js?v=35`, работает с заголовком `checkus: $2y$10$...` (статичный bcrypt-токен).
- [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py) полностью переписан: Playwright → httpx (~250 строк). 0.23s/page вместо 30+s. 12 unit-тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py).
- Hetzner-IP **забанен и на JSON API** aptekonline тоже (HTTP 403 от прода). Поэтому **aptekonline тоже скрейпится с Mac**.

### Runtime layout (split prod + Mac, обновлено 2026-05-11)

Гибрид: **aloe** на проде (direct, 03:00 UTC), **pharmonline + aptekonline** с Mac launchd (14:00 UTC = 18:00 Asia/Baku). Прод-таймер pharmonline отключён 2026-05-11 (ScraperAPI default pool стабильно отдавал 0 продуктов — фантомные runs шумели в логах). Aptekonline-таймер на проде отключён 2026-05-08 (HTTP 403 от ScraperAPI default pool — нужен residential = Hobby $49/мес).

| Сайт | Где | Чем | Расписание |
|---|---|---|---|
| **aloe.az** | Hetzner prod | Playwright DOM (см. [src/scrapers/aloe.py](src/scrapers/aloe.py)) | systemd timer `pharmacy-monitor-scrape@aloe` 03:00 UTC, direct |
| **pharmonline.az** | **Прод (Hetzner) — DDP + IPRoyal** | Meteor DDP WebSocket (см. [src/scrapers/pharmonline_ddp.py](src/scrapers/pharmonline_ddp.py)) — pierces Cloudflare без браузера, через IPRoyal residential proxy ($1.75/GB). | systemd timer `pharmacy-monitor-scrape@pharmonline` 01:00 UTC. Активирован 2026-05-27 после run 104=ok с 266,718 products. Reconnect-on-close logic выживает persist phase. Mac launchd теперь DR-fallback только: `bash infra/local/run-scrape.sh --site pharmonline --site aptekonline`. |
| **aptekonline.az** | **Mac launchd только** | httpx JSON API (см. [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)) | Mac launchd `com.pharmacy-monitor.scrape` 18:00 Asia/Baku. **Прод-таймер отключён 2026-05-08** (`systemctl disable --now pharmacy-monitor-scrape@aptekonline.timer`) — ScraperAPI default pool отдаёт HTTP 403, нужен residential (Hobby $49/мес). Endpoint: `GET /shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator), header `checkus: $2y$10$...` из `main.js`. 12 тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py). |

Systemd unit на проде: `/etc/systemd/system/pharmacy-monitor-scrape@.service`, ExecStart=`pharmacy-monitor run --site %i --mode category`. Активны таймеры **pharmonline + aloe** (aptekonline отключён 2026-05-08).

Mac launchd: [infra/local/com.pharmacy-monitor.scrape.plist](infra/local/com.pharmacy-monitor.scrape.plist) → [infra/local/run-scrape.sh](infra/local/run-scrape.sh). `DEFAULT_ARGS=--site pharmonline --site aptekonline --mode category --no-alerts`. Открывает SSH-туннель Mac:5433 → prod:5432, тянет PG_PASS из Keychain (`security add-generic-password -a pm -s pharmacy-monitor-db -w '<pwd>'`). Логи: `~/Library/Logs/pharmacy-monitor.log`. Управление: `launchctl load|unload|start ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist`.

ScraperAPI: `SCRAPER_API_KEY`, `SCRAPER_API_SITES=pharmonline,aptekonline` в `/etc/pharmacy-monitor/env` (для aptekonline теперь не используется — таймер выключен, но env-переменная безвредна). Free trial 5K credits/мес. Для возврата aptekonline на прод нужен **Hobby $49/мес (residential pool)**.

## Known issues / NOT done

- ~~Resend SMTP not configured~~ **CONFIGURED & WORKING** (2026-05-29 verified): `SMTP_HOST=smtp.resend.com`, verified domain `digest@leaddrive.cloud`. Daily digest email delivered to `EMAIL_TO` (rashadrahimov@gmail.com) — see journald `smtp_sent_ok` / `digest_sent` (digest@daily.timer 05:00 UTC).
- Telegram bot token NOT configured (`TELEGRAM_BOT_TOKEN` empty) — push-alerts off; email digest covers delivery. Activate via `configure-integrations.sh` → @BotFather token + chat_id.
- ~~Sentry DSN not configured~~ **CONFIGURED & WORKING** (2026-05-29 verified): `SENTRY_DSN` set, `init_sentry()` runs on startup (journald `sentry_initialized env=production`). FastAPI+SQLAlchemy integrations.
- NOTE: `pharmacy-monitor notify test` для smoke-теста доставки запускать с загруженным env (systemd EnvironmentFile НЕ грузится при ручном CLI): `set -a; source /etc/pharmacy-monitor/env; .venv/bin/pharmacy-monitor notify test`.
- ~~22317 AZN bug in aptekonline price parser~~ FIXED 2026-05-07. Root cause: aptekonline's Angular template `'<del>' + price + 'AZN </del>' + p.discount_price + ' AZN '` renders with no separator, so `inner_text` of `.new-price` returns e.g. `"22AZN 317 AZN"` for a discounted product. Old `parse_price` stripped non-digits → `"22317"`. Fix: extract only the FIRST digit-run-with-dots/commas via regex. Existing bad rows перезатираются следующим aptekonline-прогоном (Mac launchd 18:00 Asia/Baku ежедневно); для немедленной очистки: `DELETE FROM price_snapshots WHERE site='aptekonline' AND price > 5000;`
- pharmonline.az has NO `/sitemap.xml` (returns SPA HTML); aptekonline returns empty `<urlset>` — both need BFS fallback (regular Playwright scrapers continue to work via category pages)
- **Hetzner DE IP banned by pharmonline.az + aptekonline.az** (since ~2026-04-29). MITIGATED 2026-05-11: оба сайта переехали на Mac launchd 18:00 Asia/Baku (Baku-IP не банится). Прод-таймеры pharmonline/aptekonline disabled. Aloe остался на проде (direct работает). См. "Runtime layout" выше.
- Project under git с 2026-05-11. Initial commit `c7fde84` зафиксировал diff-only state. **Remote**: `origin` = `https://github.com/rashadrahimov/pharmacy-monitor.git`. Auth работает через cached creds (`git push origin main` без проблем).
- ~~forecast.py не рефакторен под diff-only~~ **DONE 2026-05-28**: `compute_trend` имеет Case A/B/C для sparse data (0 snaps в окне → latest globally; 1-2 snaps same price → stable). `top_movers` имеет pre-cutoff lookup для single-snapshot products. 3 diff-only regression теста в `tests/test_forecast.py` (`test_compute_trend_diff_only_sparse_active_pricing`, `test_predict_competitor_moves_diff_only_skips_truly_stable`, `test_top_movers_diff_only_sparse_change`). 18/18 forecast тестов проходят.
- 7 false matches in matcher (Friso 3 Gold ↔ Friso Prematures etc) — needs manual reject via UI on /comparison
- `admin off` in `/etc/caddy/Caddyfile` — `systemctl reload caddy` fails, use `restart` instead
- Caddy backup config: `/etc/caddy/Caddyfile.bak.20260506-2038` (pre-HTTPS)
- ~~**Next.js standalone deploy gotcha (2026-05-11)**: после `pnpm build` руками копировать static в standalone~~ **FIXED 2026-05-11**: `ExecStartPre` в `/etc/systemd/system/pharmacy-monitor-frontend.service` теперь автоматически копирует `.next/static/` → `.next/standalone/.next/static/` при каждом restart. Деплой свёлся к: `tar czf - <files> | ssh ... 'tar xzf -' && ssh ... 'cd frontend && pnpm build && systemctl restart pharmacy-monitor-frontend'`. **Важно: владелец `.next/` должен быть `pm:pm`** (chown'нили 2026-05-11) — иначе ExecStartPre упадёт на `Permission denied`.

## Useful commands

```bash
# Local dev
docker compose up -d                        # Postgres + Redis
.venv/bin/uvicorn src.api:app --reload      # API
cd frontend && pnpm dev                     # Next.js
.venv/bin/pytest -q                         # 80 unit tests

# Deploy
rsync -avz -e "ssh -i ~/.ssh/id_ed25519" \
  --exclude='.venv/' --exclude='node_modules/' --exclude='.next/' \
  --exclude='__pycache__/' --exclude='.git/' --exclude='data/db.sqlite*' \
  ./ "pm@46.225.149.52:/opt/pharmacy-monitor/"

# Sync deps after pyproject.toml change (server has no `uv` and no `pip` in venv —
# use ensurepip + python -m pip):
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52 \
  'cd /opt/pharmacy-monitor && .venv/bin/python -m ensurepip --upgrade && \
   .venv/bin/python -m pip install -q -e .'

# Server ops (root user — pm has no passwordless sudo)
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52
systemctl restart pharmacy-monitor-api       # restart backend
systemctl restart pharmacy-monitor-frontend  # restart frontend
systemctl restart caddy                      # restart proxy (reload fails — admin off)
systemctl start pharmacy-monitor-scrape@pharmonline  # manual scrape

# DB inspection (root)
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52
sudo -u postgres psql pharmacy_monitor
\dt
SELECT COUNT(*) FROM products;
```

## Code style preferences

- Python 3.12+ syntax (T | None, not Optional[T])
- Russian comments allowed for business logic, English for infra/migrations
- Tests live in `tests/`, named `test_<module>.py`
- Avoid `print()` in src/ — use `structlog`
- New endpoints under `/api/v1/dash/*` for frontend, `/api/v1/<x>` for ERP

## Important context I should not forget

- User is in Baku (Asia/Baku timezone, UTC+4)
- User speaks Russian primarily, prefers concise communication
- User trusts auto mode — minimize confirmation prompts
- Hetzner project ID: `14487088` (`pharmacy-monitor`)
- API token revoked after deploy — to create another: console.hetzner.com → Security → API Tokens

## Roadmap state (12-week plan, all in docs/PRODUCTION_OVERVIEW.md)

```
[✓] W1-W11      All foundation + monitoring + i18n done
[✓] W12         i18n + load test + go-live runbook  
[✓] Deployed    Hetzner production live with 17288 rows of pilot data
[✓] HTTPS       leaddrive.cloud + www, Let's Encrypt auto-renew (2026-05-07)
[✓] AI crawler  CLI shipped, sitemap works, aloe extraction working (RSC JSON-LD + Playwright JSON-LD), 25/50 products on dry-run smoke
[✓] Nightly     Hybrid runtime: aloe на проде (03:00 UTC), pharmonline+aptekonline с Mac launchd 18:00 Baku
[✓] Diff-only   persist + analyzer + 5 consumers под новую семантику (2026-05-09 → 11). 51× экономия snapshots, 2× быстрее прогон.
[✓] Git         project under VCS с 2026-05-11 (initial commit c7fde84) + remote github.com/rashadrahimov/pharmacy-monitor
[✓] i18n full   locale switcher в nav, AZ/EN fix (route /locale вместо /api/locale), 71 строка переведена, ROI actions переведены (2026-05-26)
[✓] Phase 0     defuse time bombs: env-checkus, request_id, deep /health, cleanup tools (2026-05-27, commit af63932)
[~] Phase 1     scraper resilience: BD Web Unlocker для aptekonline ✓, pharmonline даёт 502 от BD → остался на Mac. AI fallback wired (opt-in)
[✓] Phase 1c    DDP scraper для pharmonline (commit 6969e96+beac3ca). Reverse-engineered Meteor `products` method, pierces Cloudflare без браузера через IPRoyal residential. Reconnect-on-close logic — выживает persist phase pause. **Run 104: status=ok, 266,718 products** (vs 599 без reconnect = 445× прирост).
[✓] Cutover     pharmonline переведён на прод-таймер (01:00 UTC = 05:00 Baku). Mac launchd теперь скрейпит только aptekonline. Mac DR-fallback для pharmonline остался (`bash run-scrape.sh --site pharmonline`).
[✓] Phase 2.5   Match suggestion review UI (commit 82922ad). GET /api/v1/dash/matches/suggestions + POST /confirm. Page /matches/review с confidence slider + needs_review filter + confirm/reject buttons. 8 unit tests pass. i18n ru/az/en.
[✓] Phase 3.3   Local backup восстановлен (commit 437dfc9 — фикс `$2: unbound variable` regression от 2026-05-23) + GPG encryption. Manual offsite через `infra/local/fetch-backup.sh` на Mac. B2 cloud отложен по решению клиента.
[✓] Phase 2     matcher v2 с barcode (2.1-2.4): migration 0007, extraction в aloe+pharmonline, priority-0 pass, rematch script. UI 2.5 deferred.
[ ] Phase 3     HA & backups: Postgres replica + B2 offsite backup
[ ] Next        Phase 2.5 (UI suggestion queue), Phase 3, либо real-data barcode coverage analysis после нескольких daily scrape
```

### Out of scope (decided 2026-05-07 by client)

- **ERP интеграция клиента** — не требуется. Не предлагать в будущих сессиях.
- **PWA / native mobile app** — не требуется. Текущий Next.js mobile-first дашборд достаточен.

## Integration setup (one-shot)

Для настройки SMTP / Telegram / Sentry / GitHub remote / ScraperAPI Hobby:

```bash
bash scripts/configure-integrations.sh
```

Интерактивный скрипт — 5 блоков, каждый можно пропустить. Обновляет
`/etc/pharmacy-monitor/env` на проде, рестартит `pharmacy-monitor-api`.
Ссылки на signup-страницы каждого сервиса встроены в подсказки.

## How to continue work

1. Read this file (you already are)
2. Check `docs/PRODUCTION_OVERVIEW.md` for full architecture
3. Check `docs/GO_LIVE.md` for deployment runbook
4. Check `~/.claude/plans/buzzing-greeting-wilkes.md` — audit от 2026-05-11
5. `git log --oneline` — история коммитов (initial snapshot `c7fde84` 2026-05-11)
