# Pharmacy Monitor

Мониторинг цен и ассортимента у конкурентов **pharmonline.az**:
- [aptekonline.az](https://www.aptekonline.az/)
- [aloe.az](https://aloe.az/)

Полный стек: scraping (Playwright) → matching (rapidfuzz) → analytics → email + Telegram alerts → web dashboard (Streamlit) + REST API (FastAPI).

## Что делает

1. **Ежедневно (06:00 AZT)** — cron-таймер запускает `pharmacy-monitor run`
2. **Скрейпит** ~700–900 товаров на 3 сайтах через Playwright (с пагинацией)
3. **Сматчивает** товары между сайтами через fuzzy-алгоритм + manual review UI
4. **Анализирует**: undercuts, новые товары, промо, тренды цен
5. **Отправляет** ежедневный HTML+Excel отчёт по email
6. **Запускает реал-тайм алерты** по 5 типам правил (email + Telegram)
7. **Каждый час** — health-check с автоматическим email при проблемах
8. **Каждый день в 02:00 UTC** — атомарный SQLite-бэкап с 90-дневной retention

## Установка (локально для разработки)

```bash
# Зависимости
uv sync

# Браузер для Playwright (~300MB, один раз)
uv run playwright install chromium

# .env с SMTP/Telegram кредами
cp .env.example .env
$EDITOR .env

# Инициализация БД
uv run pharmacy-monitor init-db

# Подложить демо-данные чтобы дашборд ожил
uv run pharmacy-monitor seed-demo --force

# Запуск дашборда
uv run streamlit run src/dashboard.py
# → http://localhost:8501
```

## Деплой на VPS (production)

См. [docs/RUNBOOK.md](docs/RUNBOOK.md) — полный operational manual.

```bash
sudo bash scripts/provision_vps.sh \
  --repo https://github.com/USER/comparison-products.git \
  --domain monitor.pharmonline.az \
  --email admin@pharmonline.az \
  --dashboard-pass 'STRONG_RANDOM_PASS'
```

Скрипт ставит на чистый Ubuntu 22.04+:
- Python + uv + Playwright chromium
- nginx + Let's Encrypt + basic auth
- 5 systemd-юнитов: `run`, `health`, `dashboard`, `backup`, `telegram` (опц.)
- logrotate (8 недель)

## CLI команды

```bash
# Прогон
pharmacy-monitor run [--dry-run] [--mode auto|watchlist|category] [--hourly]
#   auto (по умолчанию) = category: сбор каталога, содержимое watchlist на режим не влияет
#   (на проде `--site pharmonline` с маркером автономного режима уходит в public_api).
#   Закреплённые ссылки собирают только --mode watchlist, --hourly и `watchlist-tick`.
pharmacy-monitor scrape [--site SITE] [--limit N]
pharmacy-monitor report --send

# Health & maintenance
pharmacy-monitor health-check [--alert-email]
pharmacy-monitor db-check [--fix]
pharmacy-monitor seed-demo [--force]

# Получатели (email + Telegram)
pharmacy-monitor recipient add EMAIL [--name NAME]
pharmacy-monitor recipient list / remove / toggle

# Категории
pharmacy-monitor category add KEY "Название" [--pharmonline-slug X --aptekonline-slug Y --aloe-slug Z]
pharmacy-monitor category list / remove / toggle

# Watchlist (точечные SKU)
pharmacy-monitor watchlist add "Название" --pharmonline-url "..." --aptekonline-url "..." --aloe-url "..."
pharmacy-monitor watchlist import path/to/csv
pharmacy-monitor watchlist export

# Алерты
pharmacy-monitor alert add-rule TYPE [--min-pct N] [--channels email,telegram]
pharmacy-monitor alert list-rules / evaluate / recent / remove-rule

# Inventory + supplier prices
pharmacy-monitor inventory import-stock data/stock.csv
pharmacy-monitor inventory import-prices data/purchases.csv
pharmacy-monitor inventory margin-report

# Telegram bot
pharmacy-monitor telegram run-bot       # long-polling процесс
pharmacy-monitor telegram poll          # один раз — для регистрации chat_id
pharmacy-monitor telegram send-test CHAT_ID

# REST API
pharmacy-monitor api --port 8080        # для интеграции с ERP
```

## Дашборд (web UI)

Streamlit с **9 вкладками**:

| Вкладка | Содержание |
|---|---|
| 🔍 **Сравнение цен** | Side-by-side ценники по сматченным товарам, ✓/✗/🔍 ручная коррекция, batch confirm, saved views |
| 📊 **Обзор** | "🎯 Сегодняшние действия" — actionable ROI; KPI; графики; gap-анализ |
| 📈 **Аналитика** | Coverage, Match quality (auto vs manual), price index, brand share, promo history, **forecast** (linear regression) |
| 🔔 **Алерты** | Rules editor, лента 50 последних events |
| 📦 **Склад** | Stock + supplier prices (CSV upload), margin report |
| 🗂️ **Категории** | URL-парсер (paste 3 ссылок) + per-row "▶️ Прогон" |
| 📋 **Watchlist** | CRUD товаров + import/export CSV |
| 📧 **Получатели** | Email + Telegram chat_id |
| ⚙️ **Настройки** | SMTP/env config, runbook commands |

Mobile-responsive (@media <760px), Apple-style минимализм, кастомный CSS (Streamlit toolbar скрыт).

## Архитектура

```
src/
├── scrapers/            # Playwright + brand extraction + pagination
│   ├── base.py          # BaseScraper (rate-limit, retry)
│   ├── pharmonline.py   # server-rendered, .product_box_v2
│   ├── aptekonline.py   # Angular SPA, .single-product-wrap, .manufacturer
│   └── aloe.py          # Next.js SPA, [class*="productCardWrapper"]
├── storage.py           # SQLAlchemy: 13 таблиц + lightweight migrations
├── normalize.py         # Парсинг цен/дозировок/упаковок
├── matcher.py           # Fuzzy auto-match (rapidfuzz, threshold 90)
├── match_actions.py     # Manual ✓ Подтвердить / ✗ Не один / 🔍 Заменить
├── analyzer.py          # Day-over-day diff: prices, new SKUs, promos
├── analytics.py         # Brand share, promo history, price index, match quality
├── roi.py               # Actionable insights: 4 типа действий + monthly impact
├── forecast.py          # Trend detection + 7-day price forecast (linear regression)
├── alerts.py            # Real-time engine: 5 rule types + dedup
├── inventory.py         # Stock + supplier prices CSV import + margin
├── reporter.py          # HTML email + Excel + PDF
├── notifier.py          # SMTP + Telegram (raw HTTP)
├── telegram_bot.py      # Long-polling bot: /start /today /alerts /status /help
├── health.py            # 6 sanity checks для production monitoring
├── api.py               # FastAPI REST для ERP-интеграции
├── tenants.py           # Multi-tenant skeleton (dormant — нет 2-го клиента)
├── onboarding.py        # 4-step wizard на пустой БД
├── url_parser.py        # 3 site URL → (site, slug) для категорий
├── pdf_export.py        # HTML → PDF через Playwright
├── watchlist.py         # CRUD: recipients, tracked, categories, saved views
├── demo.py              # Seed реалистичных fake-данных
├── dashboard.py         # Streamlit UI (9 tabs)
└── main.py              # CLI entrypoint (40+ команд)
config/
└── categories.yaml      # Seed-конфиг категорий (мигрируется в БД при init-db)
templates/
└── report.html.j2       # Email-шаблон
docs/
├── RUNBOOK.md           # Operational manual для VPS
└── MATCHING-GUIDE.md    # Гайд для клиента: как корректировать матчи
scripts/
├── provision_vps.sh     # One-shot Ubuntu deploy (nginx + systemd + Let's Encrypt)
├── probe.py             # Live DOM inspection (для дебага скрейперов)
└── probe_pagination.py  # Пагинация-разведка
infra/scripts/
└── backup.sh            # Daily Postgres pg_dump + gzip + GPG + optional B2 offsite
tests/                   # 139 unit-тестов + 7 snapshot regression
└── fixtures/            # Сохранённые HTML страницы 3 сайтов
```

## База данных

13 таблиц (SQLite по умолчанию, поддержка PostgreSQL через `DATABASE_URL`):

- `runs` — каждый прогон скрейпинга
- `products` — товары на каждом сайте
- `price_snapshots` — цена/промо в момент прогона
- `matches` — кросс-сайтовые кластеры (canonical product)
- `match_rejections` — анти-матчи (после ✗ Не один товар)
- `categories` — категории для скрейпинга
- `tracked_products` + `tracked_product_links` — watchlist
- `recipients` — email + telegram_chat_id
- `alert_rules` + `alert_events` — правила и история событий
- `promos` — промо-кампании
- `stock_levels` + `supplier_prices` — agency-mode (CSV import)
- `saved_views` — кастомные фильтры дашборда
- `tenants` + `tenant_users` — multi-tenant skeleton (dormant)

Lightweight migrations через PRAGMA + ALTER TABLE (без Alembic).

## Тесты

```bash
uv run pytest -q                                    # 139 unit-тестов, ~3s
uv run pytest -q -W error::DeprecationWarning       # 0 warnings
uv run pytest tests/test_scrapers_snapshot.py -v    # 7 snapshot regression (~1 мин)
```

Покрытие:
- `test_normalize.py` — парсинг цен/дозировок (10 tests)
- `test_matcher.py` + `test_match_actions.py` — fuzzy match + rejection (15)
- `test_analyzer.py` — day-over-day diff (5)
- `test_reporter.py` — HTML+Excel render (3)
- `test_demo.py` — seed-data (8)
- `test_health.py` — 6 sanity checks (9)
- `test_alerts.py` — engine + dedup (9)
- `test_analytics.py` — brand/promo/index/match_quality (8)
- `test_inventory.py` — CSV + margin + stock-aware ROI (7)
- `test_forecast.py` — linear regression + trends (8)
- `test_roi.py` — actionable insights (8)
- `test_watchlist.py` — CRUD (12)
- `test_url_parser.py` — 3-site URL parsing (15)
- `test_telegram_bot.py` — bot commands routing (10)
- `test_tenants.py` — multi-tenant skeleton (8)
- `test_scrapers_snapshot.py` — DOM regression на сохранённых HTML (7)

## REST API

```bash
# В .env
PHARMACY_API_KEY=secret-token

# Запуск
uv run pharmacy-monitor api --port 8080

# Endpoints (auth: X-API-Key header)
curl -H "X-API-Key: secret-token" http://localhost:8080/api/v1/comparisons
curl -H "X-API-Key: secret-token" http://localhost:8080/api/v1/margin
curl -H "X-API-Key: secret-token" http://localhost:8080/api/v1/alerts/recent

# Push из ERP клиента
curl -X POST -H "X-API-Key: secret-token" -H "Content-Type: application/json" \
  -d '[{"sku":"PHM-12345","qty":15,"name":"Paracetamol"}]' \
  http://localhost:8080/api/v1/inventory/stock

# OpenAPI docs
open http://localhost:8080/docs
```

## Telegram бот (опционально)

```bash
# 1. @BotFather → /newbot → копируй токен
echo "TELEGRAM_BOT_TOKEN=12345:ABC..." >> .env

# 2. Запустить bot polling
uv run pharmacy-monitor telegram run-bot

# 3. Пользователь в дашборде: Настройки → Уведомления → «Получить код привязки»
# 4. Отправляет код боту: /start <код> (код действует 10 минут, один раз)
# 5. В правиле алерта указать channels=email,telegram
```

Поддерживаемые команды: `/start`, `/help`, `/today`, `/alerts`, `/status`.

## Что ИЗ КОРОБКИ работает после установки

- ✅ Скрейпинг 3 сайтов через Playwright + пагинация (aptekonline до 240+ товаров)
- ✅ Brand extraction (aptekonline + aloe)
- ✅ Auto-matcher с fallback на имя если brand пустой
- ✅ Manual UI (✓ Подтвердить / ✗ Не один товар / 🔍 Заменить)
- ✅ Batch confirm всей страницы Сравнения
- ✅ Saved views (создать / переименовать / удалить)
- ✅ ROI калькулятор + monthly impact (с условным volume)
- ✅ Stock + supplier prices CSV → margin-report
- ✅ Email + Telegram alerts (5 типов правил)
- ✅ Forecast + competitor move predictions
- ✅ Health-check (6 sanity checks) + email при проблемах
- ✅ Daily SQLite backups с 90-day retention
- ✅ Onboarding wizard на пустой БД с dismiss
- ✅ PDF export через Playwright
- ✅ REST API для ERP с X-API-Key auth
- ✅ Mobile-responsive @media <760px

## Что НЕ ДЕЛАЕМ (по согласованию)

- ❌ Multi-tenant query scope (только 1 клиент в обозримом — skeleton оставлен dormant)
- ❌ Sales history import (нет данных от клиента → ROI остаётся условным)
- ❌ ML forecasting (Prophet/ARIMA) — линейная регрессия покрывает 80%
- ❌ Brand identity / landing page (нет cold sales)
- ❌ SaaS billing (Stripe/Paddle) — решение клиента
- ❌ Регион. экспансия за пределы Азербайджана

## License

Closed-source. Все права принадлежат заказчику.
