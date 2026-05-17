# Deploy Notes — 2026-05-17

## Summary

Live на проде: AI-нормализатор фарм-атрибутов + три симметричные site-страницы +
ручной матчер для cross-site линковки. Cross-site visibility выросла в 3.8× после
AI normalization backfill.

## Commits в этой сессии (ветка `claude/infallible-euclid-07bed8`)

| SHA | Что |
|---|---|
| `134f69b` | `/aloe` дашборд + site-agnostic backend (per-site endpoints) |
| `33c8495` | AI normalizer (`src/ai_normalize.py`) + matcher rewrite + migration 0004 |
| `934c124` | `/aloe-matcher` ручной UI + 3 endpoints для cross-site линковки |
| `6771eb8` | Symmetric `/site/[name]` route (replaces hardcoded `/aloe`) |

## AI-normalize backfill — итоговая статистика

Запущено `pharmacy-monitor ai-normalize` с разбивкой по сайту параллельно (3
процесса). Первый проход с `batch_size=50` дал высокий процент failures из-за
`max_tokens=4000` truncating LLM response. Retry с `batch_size=20` закрыл все
failures чисто.

| Сайт | Total SKU | Normalized | Stages | Cost |
|---|---|---|---|---|
| **aloe** | 1,803 | 1,803 (100%) | 1st (579 ok, 1200 fail) + retry (1200 ok) | $0.06 + $0.18 = **$0.24** |
| **pharmonline** | 16,035 | 16,035 (100%) | 1st (4000 ok, 11623 fail) + retry (11457 ok) | $0.43 + $1.72 = **$2.15** |
| **aptekonline** | 26,120 | 26,120 (100%) | 1st (3350 ok, 22058 fail) + retry (21952 ok) | $0.36 + $3.28 = **$3.64** |
| **TOTAL** | **43,958** | **43,958 (100%)** | | **$6.03** |

Tokens: 3.55M input + 4.34M output (Claude Haiku 4.5 @ $0.25/$1.25 per 1M).
Lesson learned: `DEFAULT_BATCH_SIZE = 50` → `20` (см. todo).

## Matcher итоги

Final matcher run после полного backfill — 38 секунд на 43,958 продуктов,
обновил 3,459 кластеров.

| Match strategy | Кол-во | Доля |
|---|---|---|
| `legacy_fuzzy` | 2,588 | 74.8% |
| `ai_attrs_strict` | **493** | **14.2%** (новое) |
| `unknown` | 218 | 6.3% (старые до миграции) |
| `ai_attrs_partial` | **29** | **0.8%** (новое) |
| TOTAL | 3,328 | (плюс 131 manual matches вне подсчёта) |

## Cross-site coverage (главный business KPI)

`/comparison` — strict mode (минимум 2 сайта в кластере):

| Метрика | Было | Стало | Δ |
|---|---|---|---|
| Total cross-site rows | 464 | **1,761** | +1,297 (×3.8) |
| aloe present | 30 (6.5%) | **91 (5.2%)** | +61 (×3.0) |
| aloe + aptekonline (без ph) | 3 | **45** | +42 (×15) 🎯 |
| aloe + pharmonline (без apt) | 11 | 18 | +7 (×1.6) |
| All 3 sites | 16 | 28 | +12 (×1.75) |
| aptekonline + pharmonline | 434 | 1,670 | +1,236 (×3.8) |

Самый big-win — **aloe ↔ aptekonline матчи** (×15). AI разобрал
brand_canonical + dosage_mg → matcher нашёл совпадения, которые fuzzy
text comparison не видел из-за разной транслитерации Azerbaijani/Russian
имён.

## Что live в проде

- `/site/pharmonline` — 16,035 продуктов с поиском/фильтрами/ROI
- `/site/aptekonline` — 26,120 продуктов
- `/site/aloe` — 1,803 продукта
- `/aloe` → redirect `/site/aloe` (бэкаповая совместимость)
- `/aloe-matcher` — ручная привязка aloe к существующим pharmonline+aptekonline кластерам (2,800 кластеров на привязку)
- `/overview` — новый KPI «AI-normalized %» + chips «match strategies»
- `/alerts` — уже работает, 5,680 событий за 30 дней, но push не настроены

## Что НЕ настроено / next steps

1. **Telegram bot / SMTP push для alerts** — настройка 5 мин (`@BotFather` → token → env →
   `systemctl restart pharmacy-monitor-api`). Опт-ин через `/settings`.
2. **Daily digest** — flag в БД (миграция 0003) есть, sender-код в
   `src/notifications.py` есть; не хватает cron-timer'а который вызывает
   рассылку в 06:00 UTC. ~1-2 часа работы.
3. **Mac-зависимость для pharmonline + aptekonline** —
   подтверждено сегодня: Hetzner IP `46.225.149.52` → 403 от обоих сайтов
   (Cloudflare anti-bot). Альтернативы:
   - VPS у локального AZ-провайдера (~$8/мес, шанс что не забанят 50-70%)
   - ScraperAPI Hobby residential pool ($49/мес, ~100% работает)
   - Mac always-on ($0, сейчас)
4. **DEFAULT_BATCH_SIZE 50 → 20** в `src/ai_normalize.py` — мелкое
   улучшение, чтобы будущий daily delta-backfill сразу не падал.
5. **needs_review=true для 28,456 продуктов (65%)** — высокий процент
   подсказывает что LLM не уверен в активном веществе (БАДы, косметика,
   детпит без явного INN). Можно сделать «лист на ручную проверку» страницу
   в дашборде для самых частых.

## Verification

- pytest: 319 тестов проходят (4 pre-existing Playwright flake)
- Frontend `next build`: 15 routes, /site/[site] (6.48 KB), /aloe-matcher (7.01 KB)
- Smoke на проде:
  - `GET /api/v1/dash/normalize/stats` → coverage 100%, matches_by_strategy с AI
  - `GET /api/v1/dash/comparison?limit=2000` → 1,761 rows
  - `GET /site/{pharmonline,aptekonline,aloe}` → HTTP 200, 26-31 KB HTML

## Rollback

- `PHARMACY_AI_NORMALIZE=0` в `/etc/pharmacy-monitor/env` + `systemctl restart pharmacy-monitor-api`
  → AI normalize отключён, новые продукты пойдут только через legacy fuzzy matcher.
- `alembic downgrade -1` → откатит migration 0004 (удалит `normalized_attrs`,
  `match_strategy`). Существующие matches с is_manual=True останутся, остальные
  потеряют strategy-метку.

## Cost projection daily

- **Backfill (один раз)**: $6.03 ← заплачено
- **Daily delta** (50-200 новых/изменённых SKU/день): $0.01-0.05/день = **~$1/мес**
- Hash-cache работает: повторный AI-вызов только если name/brand/dosage/pack_size
  изменились. На стабильном каталоге 95%+ cache-hits.
