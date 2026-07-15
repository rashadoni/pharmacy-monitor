# Pharmacy Monitor — Production Runbook

Operational manual для VPS-инсталляции. Используется когда что-то ломается.

> **CURRENT OVERRIDE (2026-07-10):** активный runtime — PostgreSQL + FastAPI +
> Next.js на `46.225.149.52`, пользователь `pm`, URL `https://leaddrive.cloud`.
> Раздел legacy ниже сохранён только как история и **не является инструкцией к
> выполнению**. Актуальные процедуры начинаются с «Active runtime» и «Бэкапы».

## Active runtime

```bash
ssh -i ~/.ssh/id_ed25519 root@46.225.149.52

# Основные сервисы и расписание
systemctl status caddy pharmacy-monitor-api pharmacy-monitor-frontend \
  postgresql redis-server --no-pager
systemctl list-timers --all 'pharmacy-monitor-*'

# API и health
curl -fsS http://127.0.0.1:8080/health
journalctl -u pharmacy-monitor-api -n 100 --no-pager
journalctl -u 'pharmacy-monitor-scrape@*' -n 100 --no-pager

# Запустить сайт вручную только через systemd под pm
systemctl start pharmacy-monitor-scrape@aloe.service
systemctl status pharmacy-monitor-scrape@aloe.service --no-pager -l
```

Production не является git checkout. Не использовать на сервере `git pull`,
`git reset` или legacy `pharmacy-monitor-dashboard/run/telegram` units. Код
разворачивается контролируемым rsync/release-процессом; DB-схема — Alembic.

## 🗄️ Архив legacy SQLite/Streamlit runtime — НЕ ВЫПОЛНЯТЬ

Секция до следующего заголовка `## 💾 Бэкапы` описывает старую инсталляцию с
пользователем `pharmacy`, SQLite, Streamlit/nginx и удалёнными systemd units.
Она оставлена только для разбора истории проекта.

### 🚀 Начальная настройка (архив)

### Запуск с нуля на чистой Ubuntu 22.04+

```bash
# 1. На локальной машине: запушить репозиторий в git
git push origin main

# 2. На свежем VPS:
ssh root@<vps-ip>

# 3. Запустить provision script
bash <(curl -fsSL https://raw.githubusercontent.com/USER/REPO/main/scripts/provision_vps.sh) \
  --repo https://github.com/USER/REPO.git \
  --domain monitor.pharmonline.az \
  --email admin@pharmonline.az \
  --dashboard-pass 'STRONG_RANDOM_PASS_16_CHARS_PLEASE'

# 4. Дописать .env (SMTP, Telegram, API key)
sudo -u pharmacy nano /opt/pharmacy-monitor/.env

# 5. Тест-прогон
sudo systemctl start pharmacy-monitor-run.service
sudo journalctl -u pharmacy-monitor-run -f

# 6. Проверка дашборда
# Открыть https://monitor.pharmonline.az в браузере с логином из шага 3
```

### .env требования

```ini
# SMTP (Gmail App Password)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-account@gmail.com
SMTP_PASSWORD=xxxxxxxxxxxxxxxx
SMTP_FROM=Pharmacy Monitor <your-account@gmail.com>

# REST API (любой случайный токен)
PHARMACY_API_KEY=secret-32-char-token

# Telegram (от @BotFather, опционально)
TELEGRAM_BOT_TOKEN=12345:ABC...

# Database (default — SQLite)
DATABASE_URL=sqlite:///data/db.sqlite
```

---

### 🔍 Диагностика (архив)

### Проверить состояние всех сервисов

```bash
systemctl list-timers 'pharmacy-monitor-*'
systemctl status pharmacy-monitor-dashboard.service
systemctl status pharmacy-monitor-telegram.service
```

### Проверить здоровье системы

```bash
sudo -u pharmacy /home/pharmacy/.local/bin/uv run \
  --directory /opt/pharmacy-monitor pharmacy-monitor health-check
```

Exit-code:
- `0` — OK
- `1` — warning
- `2` — critical (требуется вмешательство)

### Посмотреть логи

```bash
# Текущий прогон
sudo journalctl -u pharmacy-monitor-run -f

# Telegram bot
sudo journalctl -u pharmacy-monitor-telegram -n 50

# Дашборд
sudo journalctl -u pharmacy-monitor-dashboard -n 50

# Файловые логи (logrotate раз в неделю)
ls -lah /opt/pharmacy-monitor/logs/
```

---

### 🔧 Типовые проблемы и решения (архив)

### `health-check` показывает `stale_run`

**Симптом:** последний прогон был >26 часов назад.

**Причины:**
1. Cron timer выключен → `systemctl status pharmacy-monitor-run.timer`
2. Сам прогон висит → `systemctl status pharmacy-monitor-run.service`
3. VPS перезагрузился → таймер должен подняться сам через systemd

**Действия:**
```bash
# Перезапустить timer
sudo systemctl restart pharmacy-monitor-run.timer

# Запустить ручной прогон
sudo systemctl start pharmacy-monitor-run.service
sudo journalctl -u pharmacy-monitor-run -f
```

### `health-check` показывает `site_drop` или `brand_coverage_loss`

**Симптом:** один из сайтов сменил вёрстку, скрейпер собирает <20% от обычного объёма.

**Действия:**
1. Открыть сайт в браузере, проверить — изменилась ли структура
2. Запустить probe-скрипт:
   ```bash
   sudo -u pharmacy uv run python scripts/probe.py
   ```
3. Сравнить новые селекторы с теми что в `src/scrapers/<site>.py`
4. Обновить селекторы → push в git → `git pull` на VPS → `systemctl restart pharmacy-monitor-dashboard`

### `health-check` показывает `zero_prices`

**Симптом:** >50% цен в последнем прогоне = NULL/0. Сломан `parse_price`.

**Действия:**
1. Проверить probe-скрипт ту же страницу — что там в DOM рядом с ценой
2. Возможно сайт сменил формат (например, цена теперь в `data-price` атрибуте)
3. Обновить регекс / селектор в `src/scrapers/<site>.py:_parse_card`

### Email не приходит

**Симптом:** клиент не получает ежедневный отчёт.

**Действия:**
```bash
# 1. Проверить SMTP credentials
sudo -u pharmacy uv run --directory /opt/pharmacy-monitor python -c "
from src import notifier
notifier.send_email('test', '<p>Hi</p>', to=['admin@pharmonline.az'])
"

# 2. Получатель в БД?
sudo -u pharmacy uv run --directory /opt/pharmacy-monitor pharmacy-monitor recipient list

# 3. Gmail App Password rotated? Создать новый: https://myaccount.google.com/apppasswords
```

### Email — слишком много писем (volume controls)

**Симптом:** клиент жалуется, что писем приходит больше, чем ожидает (напр.
«поставил дайджест раз в неделю, а капает по несколько раз в день»).

**Контекст:** на `EMAIL_TO`/recipients идут НЕСКОЛЬКО независимых потоков, и
per-user тумблеры daily/weekly (`tenant_users`, страница «Пользователи»)
управляют ТОЛЬКО per-user дайджестом. Остальные — отдельные механизмы:

| Поток | Источник | Как выключить | Как включить обратно |
|---|---|---|---|
| Мгновенные undercut-алерты (`[CRITICAL] … дешевле на %`) | `run` → `evaluate_rules` → `dispatch_event` (в конце каждого прогона) | `--no-alerts` в override сервиса скрейпа (гасит ВСЕ алерты сайта) | убрать `--no-alerts` |
| Полный отчёт скрейпа (HTML+Excel, тема «Pharmacy Monitor DD.MM.YYYY — N undercuts») | `run_cmd` в конце КАЖДОГО не-hourly прогона — nightly **и intraday-tick** (`hourly=False`, до ~13/день 05–17 UTC) | `SCRAPE_REPORT_EMAIL=0` в `/etc/pharmacy-monitor/env` (next scrape) | `SCRAPE_REPORT_EMAIL=1` или убрать строку |
| Daily-дайджест (тема «Дайджест за сегодня», топ-N за 24ч) | `digest@daily.timer` → `pharmacy-monitor digest` → `src/digest.py` | `systemctl disable --now pharmacy-monitor-digest@daily.timer` | `systemctl enable --now …` |
| Недельный per-user дайджест | `digest-weekly.timer` Пн 06:00 UTC → `notify digest weekly` → `src/notifications.py` (`tenant_users.weekly_digest`) | per-user тумблер в UI / `systemctl disable …` | UI / `systemctl enable …` |

**Важно — два разных тумблера, два разных отката:** отчёт-письмо гасится
**env-флагом**, daily-дайджест — **состоянием systemd-юнита**. При откате нужно
вернуть ОБА (легко забыть один).

```bash
# Текущее состояние всех email-потоков
ssh root@46.225.149.52 '
  grep "^SCRAPE_REPORT_EMAIL=" /etc/pharmacy-monitor/env || echo "(report email: ON — флаг не задан)"
  systemctl is-enabled pharmacy-monitor-digest@daily.timer pharmacy-monitor-digest-weekly.timer
  systemctl list-timers --all | grep -i digest
'
# NB: есть templated-юнит pharmacy-monitor-digest@weekly.timer (слал бы 24ч-дайджест
# на EMAIL_TO еженедельно) — он должен быть DISABLED, иначе лишний недельный блок.
```

### Дашборд не открывается

**Симптом:** `https://monitor.pharmonline.az` → timeout / 502 / 503.

**Действия:**
```bash
# Streamlit жив?
systemctl status pharmacy-monitor-dashboard
sudo journalctl -u pharmacy-monitor-dashboard -n 100

# nginx жив?
systemctl status nginx
sudo nginx -t  # синтаксис конфига

# Перезапустить
sudo systemctl restart pharmacy-monitor-dashboard
sudo systemctl reload nginx
```

### Telegram бот молчит

**Симптом:** клиент пишет `/today` боту → нет ответа.

**Действия:**
```bash
# Сервис жив?
systemctl status pharmacy-monitor-telegram
sudo journalctl -u pharmacy-monitor-telegram -n 50

# Токен правильный?
grep TELEGRAM_BOT_TOKEN /opt/pharmacy-monitor/.env

# Тест отправки вручную
sudo -u pharmacy uv run --directory /opt/pharmacy-monitor pharmacy-monitor \
  telegram send-test CHAT_ID --text "manual test"
```

---

## 💾 Бэкапы

### Текущая стратегия

- **Источник:** production PostgreSQL `pharmacy_monitor`.
- **Когда:** ежедневно в 04:00 UTC (`pharmacy-monitor-backup.timer`).
- **Где:** `/var/backups/pharmacy-monitor/pharmacy-monitor-*.sql.gz.gpg`.
- **Срок хранения на VPS:** 14 дней.
- **Шифрование:** AES-256, если задан `BACKUP_GPG_PASSPHRASE`.
- **Offsite:** B2 при наличии `B2_APPLICATION_KEY_ID/KEY`; ручная независимая
  копия на Mac — `bash infra/local/fetch-backup.sh`.

`infra/scripts/backup.sh` пишет дамп во временный файл и публикует финальное имя
только после проверки размера, `gzip -t` и GPG decrypt round-trip. При сбое
`pg_dump`/gzip/GPG временные файлы удаляются. Наличие файла с финальным именем
означает, что локальная проверка архива прошла.

Если B2 credentials заданы, а CLI/auth/upload не работает, job завершается с
ошибкой. Если credentials отсутствуют, backup остаётся только на VPS — это надо
считать незакрытым offsite-риском.

### Сделать и проверить backup вручную

```bash
sudo systemctl start pharmacy-monitor-backup.service
systemctl status pharmacy-monitor-backup.service --no-pager -l
journalctl -u pharmacy-monitor-backup.service -n 30 --no-pager
ls -lat /var/backups/pharmacy-monitor | head
```

Ожидается `status=0/SUCCESS`, `Dump verified`, `Encryption round-trip verified`
и файл порядка мегабайт, а не 20-байтный gzip header.

### Ежемесячный restore drill без изменения рабочей БД

```bash
# Выполнять под root на production. Рабочая БД не останавливается и не меняется.
set -euo pipefail
restore_db=''
gpg_home=''
cleanup_restore_drill() {
  [[ -z "$restore_db" ]] || sudo -u postgres dropdb --if-exists "$restore_db" >/dev/null || true
  [[ -z "$gpg_home" ]] || rm -rf -- "$gpg_home" || true
}
trap cleanup_restore_drill EXIT

latest=$(find /var/backups/pharmacy-monitor -maxdepth 1 -type f \
  -name 'pharmacy-monitor-*.sql.gz.gpg' -printf '%T@ %p\n' \
  | sort -nr | head -1 | cut -d' ' -f2-)
passphrase=$(grep -E '^BACKUP_GPG_PASSPHRASE=' /etc/pharmacy-monitor/env \
  | head -1 | cut -d= -f2-)
[[ -n "$latest" ]] || { echo 'Encrypted backup not found' >&2; exit 1; }
[[ -n "$passphrase" ]] || { echo 'BACKUP_GPG_PASSPHRASE is empty' >&2; exit 1; }
restore_db="pharmacy_monitor_restore_verify_$(date -u +%Y%m%d%H%M%S)"
gpg_home=$(mktemp -d /tmp/pharmacy-restore-gpg.XXXXXX)

sudo -u postgres createdb "$restore_db"
export GNUPGHOME="$gpg_home"
chmod 700 "$GNUPGHOME"
printf '%s' "$passphrase" \
  | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 \
      --decrypt "$latest" \
  | gunzip \
  | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$restore_db"

for table in products price_snapshots matches runs tenant_users; do
  source_count=$(sudo -u postgres psql -d pharmacy_monitor -X -Atc "SELECT count(*) FROM $table")
  restored_count=$(sudo -u postgres psql -d "$restore_db" -X -Atc "SELECT count(*) FROM $table")
  printf '%s source=%s restored=%s\n' "$table" "$source_count" "$restored_count"
  [[ "$source_count" == "$restored_count" ]]
done

echo 'Restore drill completed; temporary database will be removed by the trap.'
```

Trap удаляет только созданную для drill БД и временный `gpg_home`, в том числе
при ошибке decrypt/restore. Production restore выполняется отдельной процедурой:
сначала остановить все writers, сделать свежий аварийный dump текущего состояния
и только затем восстанавливать выбранную проверенную копию.

Production encryption обязательна; этот drill намеренно принимает только
`.sql.gz.gpg`. Незашифрованный архив не считается готовой production-копией.

---

## Observability (Prometheus + Grafana)

Установлено 2026-05-29 (apt, не docker). Дашборд: **https://leaddrive.cloud/grafana**

| Компонент | Порт | Что |
|---|---|---|
| Prometheus | 127.0.0.1:9090 | скрейпит `/metrics` приложения (15s) + self; alert-rules в `/etc/prometheus/alerts.yml` (7 правил) |
| Grafana | 127.0.0.1:3001 | datasource Prometheus (default), дашборд «Pharmacy Monitor» (10 панелей); за Caddy `/grafana` |
| node_exporter | — | НЕ установлен (дашборд по app-метрикам; host-метрики при желании: `apt install prometheus-node-exporter` + scrape job) |

**Доступ:** дефолтный `admin/admin` **отключён** (сброшен на случайный, нигде не сохранён — 2026-05-29, чтобы публичная Grafana не висела с дефолт-кредами). Поставь свой пароль и войди:
```bash
ssh root@46.225.149.52 'grafana cli admin reset-admin-password <YOUR_PASSWORD>'
# затем логин admin / <YOUR_PASSWORD> на https://leaddrive.cloud/grafana
```

**Конфиги:** `/etc/prometheus/prometheus.yml` (из `infra/prometheus.yml`), `/etc/prometheus/alerts.yml` (из `infra/prometheus-alerts.yml`), Grafana provisioning в `/etc/grafana/provisioning/{datasources,dashboards}/`, дашборд `/var/lib/grafana/dashboards/pharmacy.json` (из `infra/grafana/dashboard.json`).

```bash
systemctl restart prometheus grafana-server     # рестарт
curl -s http://127.0.0.1:9090/api/v1/targets     # проверить targets up
journalctl -u grafana-server -n 50               # логи Grafana
```

**Caddy:** `/grafana` роут в `/etc/caddy/Caddyfile` (`@grafana` → :3001). Бэкап перед правкой: `Caddyfile.bak.*`. `systemctl restart caddy` (reload не работает — `admin off`).

> Alertmanager НЕ поднят — alert-rules видны на Prometheus `/alerts`, но notifications не уходят (для пилота алерты идут через email-дайджест + Sentry). Поднять: `apt install prometheus-alertmanager` + раскомментировать блок в prometheus.yml.

---

## 🚨 Аварийные сценарии

### Полная потеря VPS

1. Не считать копию на потерянном VPS доступной; взять проверенный
   `pharmacy-monitor-*.sql.gz.gpg` из B2 или с Mac.
2. Поднять Ubuntu VPS, установить PostgreSQL 16, Redis, Caddy, Python и Node/pnpm.
3. Развернуть тот же release приложения и Alembic migrations.
4. Создать пустую БД `pharmacy_monitor` и роль приложения `pm`.
5. Расшифровать проверенный archive и восстановить через `psql -v ON_ERROR_STOP=1`.
6. Сверить counts ключевых таблиц и Alembic head до запуска writers.
7. Запустить API/frontend, выполнить smoke tests, затем включить scrape timers.

До автоматизации полного bare-metal restore это ручная операция. Секреты брать
из отдельного защищённого хранилища, не из репозитория.

### БД повреждена

```bash
sudo -u postgres psql -d pharmacy_monitor -X -c 'SELECT 1;'
sudo -u postgres psql -d pharmacy_monitor -X -c \
  "SELECT datname, pg_database_size(datname) FROM pg_database WHERE datname='pharmacy_monitor';"
sudo -u postgres pg_dump --schema-only --no-owner pharmacy_monitor >/dev/null
```

Не восстанавливать поверх рабочей БД вслепую. Сначала остановить все writers,
создать аварийный dump текущего состояния и доказать выбранный backup через
restore drill во временную БД.

### Скрейперы внезапно перестали работать

Это случается когда сайты меняют HTML. Сценарий:

1. `pharmacy-monitor-health.timer` отправит email-алерт (`site_drop` или `brand_coverage_loss`)
2. Проверить journal конкретного `pharmacy-monitor-scrape@<site>.service`
3. Если нет — открыть страницу сайта в браузере с DevTools, найти новые селекторы
4. Локально запустить `scripts/probe.py` для дебага
5. Обновить `src/scrapers/<site>.py`, прогнать тесты и развернуть path-scoped fix
6. Повторить только нужный сайт через systemd; Mac scraper не включать

### Disk full

```bash
df -h /opt
# Что съедает:
sudo du -sh /opt/pharmacy-monitor/{logs,data,reports}/*

# Чистка старых отчётов
sudo find /opt/pharmacy-monitor/reports/ -mtime +30 -delete

# Чистка старых бэкапов
sudo find /var/backups/pharmacy-monitor/ -name 'pharmacy-monitor-*.sql.gz*' \
  -mtime +14 -type f -delete
```

---

## 🔄 Обновления / деплой нового кода

Production `/opt/pharmacy-monitor` не является git checkout. Не выполнять там
`git pull/reset`. Перед DB migration/deploy обязательны успешный backup и restore
proof. Пока единый atomic release pipeline не реализован, деплой выполняется
только path-scoped rsync с сохранением предыдущих файлов и явным smoke-test:

```bash
systemctl status pharmacy-monitor-backup.service --no-pager -l
curl -fsS http://127.0.0.1:8080/health
curl -fsSI http://127.0.0.1:3000/login
journalctl -u pharmacy-monitor-api -n 50 --no-pager
journalctl -u pharmacy-monitor-frontend -n 50 --no-pager
```

Миграции не запускать, пока `alembic current` и `alembic heads` не показывают
одну согласованную ветку. Следующая задача roadmap — versioned release directory,
полная доставка migrations/units/manifests, smoke tests и atomic symlink switch.

---

## 📊 Регулярные проверки

Раз в неделю:
- [ ] `health-check` без issues
- [ ] Email-отчёты приходят клиенту
- [ ] Последний проверенный backup младше 26 часов и больше 1 MB
- [ ] Offsite-копия существует вне production VPS
- [ ] Логи в `/logs/` ротируются

Раз в месяц:
- [ ] Disk usage не растёт неконтролируемо
- [ ] Restore drill в отдельную PostgreSQL DB проходит со сверкой counts
- [ ] FastAPI/Next.js uptime и per-site freshness соответствуют SLO
- [ ] Случайный sample отчёта — данные осмысленные

---

## 📞 Контакты для эскалации

- Скрейпер сломался → разработчик (auto-alert через email)
- VPS недоступен → хостинг (Hetzner/DigitalOcean)
- Email не идёт → Gmail support / новый App Password
- Telegram молчит → @BotFather

---

## 🌐 Scraper proxy chain + DDP recovery (updated 2026-07-10)

Текущий runtime:

| Сайт | Где | Чем | Proxy |
|---|---|---|---|
| **pharmonline.az** | Hetzner prod | **Meteor DDP WebSocket** (`src/scrapers/pharmonline_ddp.py`) | Decodo AZ residential `az.decodo.com:30001-30010` |
| **aloe.az** | Hetzner prod | RSC/HTTP parser (`src/scrapers/aloe.py`) | Direct (no proxy needed) |
| **aptekonline.az** | Hetzner prod | httpx JSON API (`src/scrapers/aptekonline.py`) | Decodo AZ residential |

Mac scraping is retired. `com.pharmacy-monitor.scrape` and
`com.pharmacy-monitor.watch` should remain unloaded/disabled; the scripts under
`infra/local/` are fail-closed unless `PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1` is
set for an explicit disaster-recovery run.

### DDP recovery procedures

**HTTP 402 на WebSocket proxy CONNECT** (наблюдалось 2026-07-06..10):
- Это billing rejection от старого IPRoyal; prod больше не должен выбирать его.
- Проверить: `DECODO_SITES` содержит `pharmonline`, а `IPROYAL_SITES` пуст.
- В логе ожидается `pharmonline_ddp_proxy provider=decodo`.

**HTTP 522 на Decodo proxy CONNECT**:
- Отдельные sticky AZ-порты могут быть временно недоступны.
- `_decodo_proxy_factory` циклически меняет `30001-30010` на каждый DDP reconnect.
- Если все порты повторно падают: проверить баланс Decodo и состояние AZ-пула;
  Mac launchd не является штатным fallback.

**`ConnectionClosedError: no close frame received or sent`** во время persist phase:
- Это нормально — DDP server тайм-аутит ping pong когда event loop долго блокирован
- `_DDPClient` имеет reconnect-on-close (Phase 1c.4) — auto-recover, retry 1 раз с fresh session
- Если retry тоже падает → check `journalctl -u pharmacy-monitor-scrape@pharmonline`

**Меняем proxy провайдер**:
```bash
# Update /etc/pharmacy-monitor/env on prod (root only)
# DECODO_USERNAME, DECODO_PASSWORD, DECODO_HOST=az.decodo.com
# DECODO_PORTS=30001-30010
# DECODO_SITES=aptekonline,pharmonline
# IPROYAL_SITES=  (disabled; rollback only)
# BRIGHTDATA_USERNAME, BRIGHTDATA_PASSWORD, BRIGHTDATA_HOST
# SCRAPER_API_KEY (fallback)
# Restart scrape:
systemctl restart pharmacy-monitor-scrape@pharmonline
```

Priority order in `src/scrapers/base.py`:
1. Decodo residential
2. IPRoyal residential (legacy; disabled on prod)
3. Bright Data
4. Crawlbase
5. ScraperAPI
6. Generic proxy / direct connection

For Pharmonline DDP specifically, `src/scrapers/pharmonline_ddp.py` selects
Decodo first and rotates ports on reconnect; IPRoyal is only a legacy fallback.

### Firecrawl as scraper backup (Phase 6 — Firecrawl MCP)

Бэкап путь когда нативные скрейперы падают:
```python
# Один продукт через Firecrawl API ($0.005 per scrape)
curl -X POST 'https://api.firecrawl.dev/v2/scrape' \
  -H "Authorization: Bearer $FIRECRAWL_API_KEY" \
  -d '{"url":"...","formats":["markdown"]}'
```

**Cloudflare bypass работает** для pharmonline (verified 2026-05-28). Используем когда DDP блокирован.

**НЕ для daily 25k+ scrape** — free tier 1000 credits/мес, не покрывает full coverage. Только для targeted use cases:
- Quality-control re-scrape подозрительных matches
- AI crawler fallback (см. `src/scrapers/ai_crawler.py`)
- Backfill specific fields для existing products

---

## 🌍 i18n routing (Phase 6.1, 2026-05-28)

URL pattern: `/<locale>/<route>` где locale ∈ {ru, az, en}.

### Архитектура

- **No middleware** — обходит next-intl issue #524 (standalone middleware rewrite recursion)
- `app/[locale]/layout.tsx` — locale validation + setRequestLocale + NextIntlClientProvider
- `app/page.tsx` — root redirect → `/ru`
- `app/[locale]/page.tsx` — `/ru` → `/ru/overview`
- `next.config.mjs` `redirects()` — backward compat для legacy unprefixed URLs (`/comparison` → `/ru/comparison`)

### Debug recipes

**Locale переключение не работает на конкретной странице**:
```bash
# Verify locale appears in URL
curl -sSI https://leaddrive.cloud/az/comparison | grep -i location
# Should return 307 → /login (auth gate) или 200 (page renders)
```

**Получаешь 500 на /[locale]/ страницах**:
- Check journal: `journalctl -u pharmacy-monitor-frontend --since "5 min ago" | grep -i error`
- "Failed to proxy localhost:3000" → middleware был не удалён, проверь `ls frontend/src/middleware.ts`
- useSearchParams ошибка → нужен Suspense boundary в client component

**Старые bookmarks `/comparison` не работают**:
- Verify `next.config.mjs` имеет `redirects()` block
- Test: `curl -sSI https://leaddrive.cloud/comparison` → 307 → /ru/comparison
- Если 404 — `redirects()` не сработал или route не в `LEGACY_ROUTES` list

### Add new locale-aware route

1. `frontend/src/app/[locale]/<route>/page.tsx` (главная страница)
2. Внутренние `<Link>` — используй `import {Link} from "@/i18n/navigation"`
3. Для backward compat: добавь `<route>` в `LEGACY_ROUTES` массив в `next.config.mjs`

---

## 🕐 Intraday rotation (Phase 5.1c, 2026-05-28)

Hourly during business hours (05-17 UTC), rotates через top-30 volatile категорий.

### Manual trigger

```bash
# Dry-run (preview без mutation)
ssh root@46.225.149.52 'cd /opt/pharmacy-monitor && \
  sudo -u pm bash -c "set -a; source /etc/pharmacy-monitor/env; \
  .venv/bin/pharmacy-monitor intraday-tick --dry-run"'

# Real tick
ssh root@46.225.149.52 'systemctl start pharmacy-monitor-intraday.service'
```

### Inspect rotation state (Redis)

```bash
ssh root@46.225.149.52 '
  redis-cli get "intraday:rotation:idx"  # current index
  redis-cli ttl "intraday:lock:site:pharmonline"  # TTL до next tick allowed
  redis-cli ttl "intraday:lock:site:aloe"
'
```

### Reset rotation (если зависло)

```bash
ssh root@46.225.149.52 '
  redis-cli del "intraday:rotation:idx" "intraday:lock:site:pharmonline" "intraday:lock:site:aloe"
'
# Next tick перезапустится с idx=1
```

### Disable intraday (если жрёт proxy credits)

```bash
ssh root@46.225.149.52 'systemctl disable --now pharmacy-monitor-intraday.timer'
```

---

## 🔌 MCP servers — 9 stack (2026-05-28)

**Доступны во всех проектах через user-scope** (`~/.claude.json`). Когда использовать:

| MCP | Best for |
|---|---|
| `mcp__perplexity-ask__*` | Anti-hallucination, fact-check, library docs |
| `mcp__brave-search__*` | Independent search для cross-check (Perplexity backup) |
| `mcp__firecrawl__*` | Web scraping, Cloudflare bypass, schema extract |
| `mcp__postgres__*` | DB queries без SSH (через persistent tunnel :5433) |
| `mcp__github__*` | Native PR/issues/code-search |
| `mcp__memory__*` | Cross-session knowledge graph |
| `mcp__playwright__*` | Cross-browser E2E automation, login flows |
| `mcp__chrome-devtools__*` | Debug live Chrome — Network/Console/Performance |
| `mcp__openrouter-sonar__*` | Sonar models (если OpenRouter credits ok) |

### Postgres MCP — SSH tunnel auto-start

`~/Library/LaunchAgents/com.pharmacy-monitor.db-tunnel.plist` (auto-restart, persistent).

Управление:
```bash
launchctl list | grep db-tunnel  # status
launchctl bootout gui/$UID/com.pharmacy-monitor.db-tunnel  # stop
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.pharmacy-monitor.db-tunnel.plist  # start
tail -f ~/Library/Logs/pharmacy-monitor-db-tunnel.log  # tunnel logs
```

### MCP debug

```bash
# Список servers + connection status
claude mcp list

# Re-spawn конкретный server (после env update в config)
claude mcp remove <name> -s user
claude mcp add <name> -s user -e KEY=VAL -- <cmd>

# Если tool возвращает "Unauthorized" — env not propagating
# Check process args:
ps -ef | grep <server-name> | head -2
```

### MCP playbook — install per-project vs user-global

**user-scope** (`-s user`, по умолчанию у нас все 9 MCP): доступны в каждом
проекте Claude, конфиг в `~/.claude.json`. Применяй для personal tooling
(Perplexity, Brave, Firecrawl) и любых key-bearing сервисов которые не
коммитятся в репо.

**project-scope** (`-s project`): конфиг в `.claude/mcp.json` репозитория,
коммитится. Применяй для team-shared MCPs (например внутренний linear MCP
команды, custom company-specific tools).

**local-scope** (`-s local`): только для текущего проекта, в `.claude/mcp.json`
но НЕ под git (gitignored). Используй для experimental или sensitive-but-shared
configs.

```bash
# Установить (3 scope варианта):
claude mcp add <name> -s user    -e KEY=$VAL -- npx -y <package>
claude mcp add <name> -s project -e KEY=$VAL -- npx -y <package>
claude mcp add <name> -s local   -e KEY=$VAL -- npx -y <package>

# Migrate user → project (для team-shared)
claude mcp remove <name> -s user
claude mcp add <name> -s project -e KEY=$VAL -- npx -y <package>
```

### MCP installation — verify connection

После каждого `claude mcp add`:

1. **Restart Claude Code** (`Cmd+Q` → re-open) — server spawns on session start, not hot-reload.
2. `claude mcp list` — должен показывать ✓ или ⚠ next to server name.
3. В новом session: попроси Claude использовать один tool из этого MCP — например для firecrawl: "use firecrawl to fetch https://example.com" → должен вернуть HTML без ошибки.
4. Если ✗ или "Unauthorized": см. ниже **env var propagation**.

### MCP env-var propagation troubleshooting

**Симптом**: tool сразу возвращает `Unauthorized: API key is required` или `403 invalid token`, хотя key верный.

**Корень**: Claude spawn'ит MCP server'а subprocess'ом и передаёт env через `-e` флаги. Если key содержит спец-символы (`$`, `!`, `:`, backslash) — shell может их интерпретировать ДО передачи в `claude mcp add`.

**Диагностика**:

```bash
# 1. Проверь актуальный env у spawn'нутого процесса:
ps -ef | grep <server-name>          # узнать PID
ps eww <PID> | tr ' ' '\n' | grep KEY  # увидеть env vars процесса

# 2. Сравни с тем что в ~/.claude.json:
grep -A5 '"<server-name>"' ~/.claude.json
```

**Фикс**:

```bash
# Используй single-quotes для значения:
claude mcp add foo -s user -e 'FOO_KEY=fc-abc!def$ghi' -- npx -y foo-mcp

# Или экспортируй через temp env-file:
echo 'FOO_KEY=fc-abc!def$ghi' > /tmp/foo.env
claude mcp add foo -s user --env-file /tmp/foo.env -- npx -y foo-mcp
rm /tmp/foo.env
```

### MCP — test from CLI without Claude

Полезно проверить server stdio handshake до debug'а через Claude:

```bash
# Запусти MCP server в stdio mode (как сделал бы Claude):
FOO_KEY=fc-... npx -y firecrawl-mcp <<EOF
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"manual","version":"1.0"}}}
EOF
# Ожидаем JSON response с `serverInfo`, `capabilities`.
# Если ошибка про auth — env не дошёл; если ошибка про protocol —
# server либо crashed либо не stdio-MCP compatible.
```

### Per-server gotchas (verified 2026-05-28)

| Server | Gotcha | Workaround |
|---|---|---|
| firecrawl-mcp | v3.19.1: env var `FIRECRAWL_API_KEY` не propagates через npx через Claude spawn | Используй прямые `curl https://api.firecrawl.dev/v2/scrape -H "Authorization: Bearer $KEY"` через bash |
| postgres-mcp | Ожидает `POSTGRES_CONNECTION_STRING`, не `DATABASE_URL` | Set explicit env: `-e POSTGRES_CONNECTION_STRING=postgresql://...` |
| perplexity-ask | Rate limit ~60 rpm free tier | Backoff or upgrade Pro $20/mo |
| brave-search | Free 2000 req/мес | Скромный использовать только для cross-check |
| chrome-devtools | Требует Chrome --remote-debugging-port=9222 | Запусти Chrome: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-mcp` |
| github-mcp | PAT с `repo` + `read:org` scope | Используй fine-grained PAT, expires 1 year |
| memory-mcp | Knowledge graph persisted в `~/.claude/memory.jsonl` | Backup file перед major surgery |

---

## 🔑 API key rotation

Все ключи в `~/.claude.json` plain text. Ротация procedures:

### Perplexity (`pplx-...`)
1. https://www.perplexity.ai/settings/api → Regenerate
2. Update config: `claude mcp remove perplexity-ask -s user && claude mcp add perplexity-ask -s user -e PERPLEXITY_API_KEY=NEW -- npx -y server-perplexity-ask`

### Firecrawl (`fc-...`)
1. https://www.firecrawl.dev/dashboard → API Keys → Regenerate
2. Update config: `claude mcp remove firecrawl -s user && claude mcp add firecrawl -s user -e FIRECRAWL_API_KEY=NEW -- npx -y firecrawl-mcp`

### Brave (`BSA...`)
1. https://api-dashboard.search.brave.com/app/keys → Revoke + new
2. Update config: `claude mcp remove brave-search -s user && claude mcp add brave-search -s user -e BRAVE_API_KEY=NEW -- npx -y brave-search-mcp`

### GitHub PAT (`github_pat_...`)
1. https://github.com/settings/personal-access-tokens → Revoke
2. New token: see CLAUDE.md / Phase 6.1 setup
3. Update config: `claude mcp remove github -s user && claude mcp add github -s user -e GITHUB_PERSONAL_ACCESS_TOKEN=NEW -- github-mcp-server stdio`

После rotation — restart Claude Code чтобы MCP servers re-spawn с новыми env.

---

## 🧪 Frontend source rollback — verified 2026-05-27

**Recovery scenario**: восстановить frontend после failed deploy (i18n rollback experience).

```bash
# 1. Find latest pre-deploy backup
LATEST=$(ssh root@46.225.149.52 'ls -t /var/backups/pharmacy-monitor/frontend-src-pre-*.tgz | head -1')
echo "Will restore from: $LATEST"

# 2. Restore
ssh root@46.225.149.52 '
  cd /opt/pharmacy-monitor
  rm -rf frontend/src
  tar xzf '"$LATEST"'
  chown -R pm:pm frontend/src
'

# 3. Rebuild + restart
ssh root@46.225.149.52 '
  cd /opt/pharmacy-monitor/frontend
  sudo -u pm bash -c "NODE_OPTIONS=--max-old-space-size=4096 pnpm build"
  systemctl restart pharmacy-monitor-frontend
'

# 4. Verify
curl -sSI https://leaddrive.cloud/login | head -2  # should be HTTP/2 200
```

**RTO measured**: ~3-5 минут. Backups создаются автоматически перед deploy через ExecStartPre hook + manual `tar czf` step в deploy скриптах.
