# Pharmacy Monitor — Working Context for Claude

This file is auto-loaded in every Claude Code session. Read it first.

## Current production state (last updated: 2026-05-08)

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

**Just finished (2026-05-07)**: HTTPS migration. `leaddrive.cloud` (Namecheap) → 46.225.149.52, Caddy with Let's Encrypt cert. Cookie host-only (no Domain attr) — same-origin, CORS not needed.

**Just finished (2026-05-07)**: AI crawler CLI command `pharmacy-monitor ai-crawl --site <s> --max-urls N [--dry-run] [--budget-usd N]` registered. Config via env: `ANTHROPIC_API_KEY`, `AI_CRAWL_PROVIDER=anthropic`, `AI_CRAWL_MODEL=claude-haiku-4-5`. Deployed on prod. **Sitemap discovery working — aloe returns 19,319 URLs.**

**Just finished (2026-05-07)**: aloe.az extraction fix. Added `parse_next_rsc_jsonld()` in [src/scrapers/ai_crawler.py](src/scrapers/ai_crawler.py) (Layer 1.5 between standard JSON-LD and LLM fallback) — decodes Next.js RSC chunks (`self.__next_f.push([1, "..."])`), unescapes the `__html` value from `dangerouslySetInnerHTML`, parses schema.org Product. Smoke test (50 URLs, dry-run): **25 products extracted, $0 LLM cost**. In Playwright mode the standard `parse_jsonld_product` actually catches it post-hydration (Next.js injects the `<script type="application/ld+json">` after JS runs); RSC parser is a safety net for raw HTML / failed hydration / future HTTP-only fetching. 7 unit tests in [tests/test_ai_crawler.py](tests/test_ai_crawler.py).

**Important corrections to prior architecture notes** (CLAUDE.md was inaccurate):
- **aloe.az is Next.js 13+ App Router with RSC streaming**, not Meteor.
- **pharmonline.az is server-rendered Bootstrap + jQuery**, not Meteor. Existing Playwright DOM scraper [src/scrapers/pharmonline.py](src/scrapers/pharmonline.py) works correctly; no hydration parser needed.

**Just finished (2026-05-08)**: bulk-persist refactor в [src/main.py:293 `persist_results`](src/main.py#L293). Первый Mac launchd прогон (run_id=40) растянулся на **~5 часов** для **98 536 продуктов** — N+1 SELECT + per-row `session.flush()` через SSH-туннель к prod Postgres. Фикс: pre-fetch existing одной SELECT на категорию (`.in_(external_ids)` чанками по 500), batch `session.add_all()` + единый `flush()` для новых, bulk add snapshots, **commit per result** (bounds транзакции, разгружает WAL). 8 регрессионных тестов в [tests/test_persist_results.py](tests/test_persist_results.py), включая anti-N+1 счётчик SELECT'ов. Развёрнуто на проде. Ожидаемая скорость: 5–10 мин на полный full scrape вместо 5 часов.

**Just finished (2026-05-07)**: aptekonline JSON API rewrite + split runtime (Mac + prod).
- Aptekonline.az ввёл reCAPTCHA на любые headless-браузеры (даже с Baku-IP). Reverse-engineered backend endpoint `GET https://www.aptekonline.az/shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator, 100 items/page) — endpoint найден в `https://www.aptekonline.az/assets/js/main.js?v=35`, работает с заголовком `checkus: $2y$10$...` (статичный bcrypt-токен).
- [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py) полностью переписан: Playwright → httpx (~250 строк). 0.23s/page вместо 30+s. 12 unit-тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py).
- Hetzner-IP **забанен и на JSON API** aptekonline тоже (HTTP 403 от прода). Поэтому **aptekonline тоже скрейпится с Mac**.

### Runtime layout (split prod + Mac, обновлено 2026-05-08)

Гибрид: aloe + pharmonline на проде через systemd, aptekonline с Mac через launchd (ScraperAPI default pool возвращает 403 на aptekonline). Mac также дублирует pharmonline как failsafe.

| Сайт | Где | Чем | Расписание |
|---|---|---|---|
| **aloe.az** | Hetzner prod | Playwright DOM (см. [src/scrapers/aloe.py](src/scrapers/aloe.py)) | systemd timer `pharmacy-monitor-scrape@aloe` 03:00 UTC, direct |
| **pharmonline.az** | Hetzner prod (+ Mac failsafe) | Playwright DOM (см. [src/scrapers/pharmonline.py](src/scrapers/pharmonline.py)) | systemd timer 01:00 UTC через ScraperAPI ([base.py](src/scrapers/base.py)). Дублируется Mac launchd 18:00 Asia/Baku — пишет в ту же прод-БД через SSH-туннель. |
| **aptekonline.az** | **Mac launchd только** | httpx JSON API (см. [src/scrapers/aptekonline.py](src/scrapers/aptekonline.py)) | Mac launchd `com.pharmacy-monitor.scrape` 18:00 Asia/Baku. **Прод-таймер отключён 2026-05-08** (`systemctl disable --now pharmacy-monitor-scrape@aptekonline.timer`) — ScraperAPI default pool отдаёт HTTP 403, нужен residential (Hobby $49/мес). Endpoint: `GET /shop/productList?categoryId[]=N&lang=az&page=N` (Laravel paginator), header `checkus: $2y$10$...` из `main.js`. 12 тестов в [tests/test_aptekonline_api.py](tests/test_aptekonline_api.py). |

Systemd unit на проде: `/etc/systemd/system/pharmacy-monitor-scrape@.service`, ExecStart=`pharmacy-monitor run --site %i --mode category`. Активны таймеры **pharmonline + aloe** (aptekonline отключён 2026-05-08).

Mac launchd: [infra/local/com.pharmacy-monitor.scrape.plist](infra/local/com.pharmacy-monitor.scrape.plist) → [infra/local/run-scrape.sh](infra/local/run-scrape.sh). `DEFAULT_ARGS=--site pharmonline --site aptekonline --mode category --no-alerts`. Открывает SSH-туннель Mac:5433 → prod:5432, тянет PG_PASS из Keychain (`security add-generic-password -a pm -s pharmacy-monitor-db -w '<pwd>'`). Логи: `~/Library/Logs/pharmacy-monitor.log`. Управление: `launchctl load|unload|start ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist`.

ScraperAPI: `SCRAPER_API_KEY`, `SCRAPER_API_SITES=pharmonline,aptekonline` в `/etc/pharmacy-monitor/env` (для aptekonline теперь не используется — таймер выключен, но env-переменная безвредна). Free trial 5K credits/мес. Для возврата aptekonline на прод нужен **Hobby $49/мес (residential pool)**.

## Known issues / NOT done

- Resend SMTP not configured (email magic-link silently no-ops, password login works)
- Telegram bot token not configured (alerts only in DB)
- Sentry DSN not configured (errors only in journald)
- ~~22317 AZN bug in aptekonline price parser~~ FIXED 2026-05-07. Root cause: aptekonline's Angular template `'<del>' + price + 'AZN </del>' + p.discount_price + ' AZN '` renders with no separator, so `inner_text` of `.new-price` returns e.g. `"22AZN 317 AZN"` for a discounted product. Old `parse_price` stripped non-digits → `"22317"`. Fix: extract only the FIRST digit-run-with-dots/commas via regex. Existing bad rows перезатираются следующим aptekonline-прогоном (Mac launchd 18:00 Asia/Baku ежедневно); для немедленной очистки: `DELETE FROM price_snapshots WHERE site='aptekonline' AND price > 5000;`
- pharmonline.az has NO `/sitemap.xml` (returns SPA HTML); aptekonline returns empty `<urlset>` — both need BFS fallback (regular Playwright scrapers continue to work via category pages)
- **Hetzner DE IP banned by pharmonline.az + aptekonline.az** (since ~2026-04-29). MITIGATED: pharmonline через ScraperAPI default pool (работает), aptekonline с Mac launchd (Baku-IP, ScraperAPI default pool отдаёт 403). См. "Runtime layout" выше.
- 7 false matches in matcher (Friso 3 Gold ↔ Friso Prematures etc) — needs manual reject via UI on /comparison
- `admin off` in `/etc/caddy/Caddyfile` — `systemctl reload caddy` fails, use `restart` instead
- Caddy backup config: `/etc/caddy/Caddyfile.bak.20260506-2038` (pre-HTTPS)

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
[✓] Nightly     Hybrid runtime: aloe+pharmonline на проде, aptekonline с Mac launchd 18:00 Baku (2026-05-08)
[ ] Next        SMTP (Resend), Telegram bot token, Sentry DSN, ScraperAPI Hobby ($49) для возврата aptekonline на прод, BFS fallback для pharmonline/aptekonline
```

### Out of scope (decided 2026-05-07 by client)

- **ERP интеграция клиента** — не требуется. Не предлагать в будущих сессиях.
- **PWA / native mobile app** — не требуется. Текущий Next.js mobile-first дашборд достаточен.

## How to continue work

1. Read this file (you already are)
2. Check `docs/PRODUCTION_OVERVIEW.md` for full architecture
3. Check `docs/GO_LIVE.md` for deployment runbook
4. Check `~/.claude/plans/users-rashadrahimov-documents-compariso-swirling-dream.md` for last plan
5. Run `git log` if there are commits (currently no git initialized)
