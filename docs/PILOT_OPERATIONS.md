# Pilot Operations Manual

Краткий гайд по работе с pharmacy-monitor pilot. Ничего автоматического кроме cron — всё через CLI или дашборд.

## TL;DR

- **Дашборд**: `uv run streamlit run src/dashboard.py` → http://localhost:8501
- **Daily scrape**: cron в 12:00 (см. `crontab -l`)
- **Логи**: `logs/cron.log`, `logs/health.log`
- **БД**: `data/db.sqlite` (SQLite)

## Структура категорий

В таблице `categories` ~357 строк (50 pharmonline + 300 aptekonline + 3 aloe).
Каждая row — одна категория НА ОДНОМ сайте. Маппинг между сайтами не настроен
(нет уверенности что одна и та же категория на разных сайтах).

Для cross-site сравнения цен matcher работает по `name + brand + pack_size`,
а не по category. Так что отсутствие маппинга не блокирует сравнение цен.

## Добавить новую категорию вручную

```bash
./.venv/bin/pharmacy-monitor category add \
  --key my_category \
  --label-ru "Витамины" \
  --pharmonline-slug vitamin-ve-mineral-kompleks \
  --aptekonline-id 117 \
  --aloe-slug bad
```

Или через дашборд: вкладка «🗂️ Категории».

## Добавить новый бренд в catalog

[src/brand_catalog.py](src/brand_catalog.py):
```python
BRANDS = {
    ...
    "MyBrand": ["mybrand", "my brand", "май бренд"],
}
```

После правки **выполнить backfill для существующих продуктов:**
```bash
./.venv/bin/python -c "
from src.storage import init_db, make_session, Product
from src.brand_catalog import extract_brand
from sqlalchemy import select
init_db()
S = make_session(); session = S()
for p in session.scalars(select(Product).where(Product.brand.is_(None))):
    p.brand = extract_brand(p.name)
session.commit()
"
```

## Запустить scrape вручную

```bash
# Все активные категории всех сайтов:
./.venv/bin/pharmacy-monitor run

# Только один сайт (все его активные категории):
./.venv/bin/pharmacy-monitor run --site pharmonline

# Одна конкретная категория:
./.venv/bin/pharmacy-monitor run --mode category --category-id 12

# Без email (если SMTP не настроен — всё равно skipped gracefully):
./.venv/bin/pharmacy-monitor run --dry-run
```

## Cron

Текущая настройка:
```
0 12 * * * cd "..." && .venv/bin/pharmacy-monitor run --mode category --category-id 4 >> logs/cron.log 2>&1
```

Это только baby food. Чтобы расширить на все категории → см. план в `~/.claude/plans/`.

**Остановить cron:**
```bash
crontab -l | grep -v pharmacy-monitor | crontab -
```

## Алерты

- Все события в БД таблица `alert_events` (видны в дашборде «🔔 Алерты»)
- 5 настроенных правил: undercut_threshold, price_drop_pct, new_product, promo_started, price_raise_opportunity
- + 1 системное: `site_drop_smoke` (записывается автоматически если scrape собрал <50% от среднего)
- Email НЕ настроен (SMTP_HOST в .env пустой). Алерты только в БД.

## Health check

```bash
./.venv/bin/pharmacy-monitor health-check
# exit code: 0=ok, 1=warning, 2=critical
```

Проверяет: stale (>26ч), failed runs, empty scrape, site_drop по 7-дневному медиане.

## Severity алертов

| Severity | Что значит | Реакция |
|---|---|---|
| `critical` | Конкурент дешевле на ≥10% / scrape failed | Реакция в течение часа |
| `warning` | Конкурент дешевле на ≥5% / coverage drop ≥50% | В течение дня |
| `info` | Новый товар / можно поднять цену | Когда удобно |

## Логи

- `logs/cron.log` — stdout/stderr cron-задачи pharmacy-monitor run
- `logs/health.log` — health-check (если на cron)
- `logs/backup.log` — резервные копии БД
- `logs/app.jsonl` — структурированные events приложения (часто пустой если direct CLI)

## Бэкап БД

Перед серьёзными изменениями:
```bash
cp data/db.sqlite "data/db.sqlite.bak-$(date +%F)"
```

## Дашборд: что в каких вкладках

| Вкладка | Что показывает |
|---|---|
| 🔍 Сравнение цен | Cross-site matched товары с ценами. Default Min.сайтов=2 |
| 📊 Обзор | KPI + сегодняшние ROI-действия |
| 📈 Аналитика | Match quality, brand share, price index, forecast |
| 🔔 Алерты | События + правила |
| 📦 Склад | Маржа (требует загруженные supplier prices) |
| 🗂️ Категории | CRUD категорий |
| 📋 Watchlist | CRUD watchlist товаров |
| 📧 Получатели | Email recipients (для отчётов когда SMTP включён) |
| ⚙️ Настройки | Multi-tenant config |

## Известные ограничения pilot

1. **aloe собирает мало** — 84 SKU vs aptekonline 1423 (категория baby food). Для других категорий тоже скудно.
2. **3-сторонние matches редкие** — потому aloe не покрывает большую долю.
3. ~~**Скрейпер aptekonline иногда возвращает абсурдные цены** (22317 AZN на Nutrilon) — bug в parse_price для дробных.~~ **Исправлено 2026-05-07** в `src/normalize.py:202` — `parse_price` теперь извлекает первый price-token, а не всю склеенную строку. Старые "22317-style" записи перезатрутся следующим nightly scrape (02:00 UTC).
4. **Brand parser использует словарь + ALL-CAPS fallback** — для редких брендов может пропустить. Пополнять каталог по мере необходимости.
5. **Cross-site mapping категорий не настроен** — каждая row в `categories` это категория ОДНОГО сайта.

## Scrape robustness (W10)

### Anti-detection

Скрейпер автоматически применяет:
- 9 разных User-Agent'ов (Chrome / Firefox / Safari, macOS / Windows / Linux), ротация при каждой сессии
- Случайный realistic viewport (1280×800 .. 1920×1080) с ±10px jitter
- Stealth-патчи: `navigator.webdriver = undefined`, fake plugins, languages, WebGL renderer
- `Accept-Language: az-AZ,az;q=0.9,ru-RU;q=0.8,...` headers
- timezone_id `Asia/Baku`

### Captcha detection

Если pharmonline / aptekonline / aloe внезапно поставит Cloudflare, reCAPTCHA, hCaptcha или Datadome —
скрейпер увидит индикатор в DOM или текст «Checking your browser» / «Подтвердите...» / «Robot olduğunuzu...»,
выкинет `CaptchaDetected`, залогирует `category_captcha_blocked` и **скипнет страницу не убивая весь run**.

В summary:
```
site_scrape_summary site=pharmonline products=231 categories=50 captcha_hits=2 failures=0
```

Что делать если captcha_hits растёт:
1. Проверь логи `journalctl -u pharmacy-monitor-scrape@pharmonline | grep captcha`
2. Поставь residential proxy (см. ниже)
3. Уменьши частоту: SCRAPE_RATE_LIMIT_SEC=5

### Proxy support

Если IP заблокировали (pharmonline 403'ит каждый запрос), используй residential proxy:

```bash
# В /etc/pharmacy-monitor/env
HTTP_PROXY=http://user:pass@proxy.brightdata.com:8080
# Или альтернативный env-var (то же самое):
SCRAPE_PROXY=http://user:pass@proxy.smartproxy.com:7000
```

Перезапусти scrape unit:
```bash
systemctl restart pharmacy-monitor-scrape@pharmonline.service
```

Проверь что proxy работает:
```bash
journalctl -u pharmacy-monitor-scrape@pharmonline | grep "scrape_using_proxy"
# Ожидаешь: scrape_using_proxy proxy=http://***@proxy.brightdata.com:8080
```

Бюджет: BrightData ≈ $50/mo за 1GB residential, Smartproxy ≈ $40/mo. Хватит на месячный pilot.

### Smart retry

Скрейпер использует exponential backoff с tenacity:
- Retry на: TimeoutError, RuntimeError (включая HTTP 5xx, HTTP 429, HTTP 403)
- Backoff: 2s, 4s, 8s, ..., max 60s между попытками
- Max попыток: `SCRAPE_MAX_RETRIES` (default 3)
- HTTP 429 (rate-limited): дополнительный sleep 60-90s перед retry

Captcha НЕ retried — это сигнал что страница реально заблокирована.

### Метрики после run

В JSON-логе каждого scrape видны:
- `total_cards_found` — всего карточек на всех страницах
- `total_yielded` — успешно сохранено в БД
- `total_card_failures` — сбоев parsing
- `captcha_hits` — категорий заблокированных captcha
- `failures` — категорий упавших с другими ошибками

## Откат

Если что-то пойдёт не так — backup:
```bash
cp data/db.sqlite.bak-2026-04-29-pre-cleanup data/db.sqlite
```
