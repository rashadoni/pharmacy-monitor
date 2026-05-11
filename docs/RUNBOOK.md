# Pharmacy Monitor — Production Runbook

Operational manual для VPS-инсталляции. Используется когда что-то ломается.

## 🚀 Начальная настройка

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

## 🔍 Диагностика

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

## 🔧 Типовые проблемы и решения

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

- **Когда:** ежедневно в 02:00 UTC (`pharmacy-monitor-backup.timer`)
- **Где:** `/opt/pharmacy-monitor/data/backups/db-YYYY-MM-DD.sqlite.gz`
- **Срок хранения:** 90 дней
- **Размер:** ~200KB на ~1MB БД (gzip −80%)

### Восстановление из бэкапа

```bash
# 1. Остановить процессы которые пишут в БД
sudo systemctl stop pharmacy-monitor-dashboard
sudo systemctl stop pharmacy-monitor-telegram
sudo systemctl stop pharmacy-monitor-run.timer

# 2. Сделать копию текущей БД (на всякий случай)
sudo -u pharmacy cp /opt/pharmacy-monitor/data/db.sqlite \
  /opt/pharmacy-monitor/data/db.sqlite.before-restore

# 3. Распаковать выбранный бэкап
sudo -u pharmacy gunzip -c /opt/pharmacy-monitor/data/backups/db-2026-04-29.sqlite.gz \
  > /opt/pharmacy-monitor/data/db.sqlite

# 4. Проверить integrity
sudo -u pharmacy sqlite3 /opt/pharmacy-monitor/data/db.sqlite "PRAGMA integrity_check;"

# 5. Запустить процессы обратно
sudo systemctl start pharmacy-monitor-dashboard
sudo systemctl start pharmacy-monitor-telegram
sudo systemctl start pharmacy-monitor-run.timer
```

### Сделать бэкап вручную

```bash
sudo -u pharmacy bash /opt/pharmacy-monitor/scripts/backup.sh
```

---

## 🚨 Аварийные сценарии

### Полная потеря VPS

1. Поднять новый VPS
2. Запустить `provision_vps.sh` (см. Начальная настройка)
3. Если есть бэкап с предыдущего сервера (S3 / отдельный диск):
   - Загрузить .sqlite.gz в `/opt/pharmacy-monitor/data/backups/`
   - Восстановить (см. выше)

**Recommendation:** настроить off-site бэкап (S3, Backblaze B2, dropbox).

### БД повреждена

```bash
sudo -u pharmacy sqlite3 /opt/pharmacy-monitor/data/db.sqlite "PRAGMA integrity_check;"
# Если "ok" — БД целая. Иначе → восстановить из бэкапа.
```

### Скрейперы внезапно перестали работать

Это случается когда сайты меняют HTML. Сценарий:

1. `pharmacy-monitor-health.timer` отправит email-алерт (`site_drop` или `brand_coverage_loss`)
2. Войти на VPS → `git pull` → проверить если кто-то уже коммитнул фикс
3. Если нет — открыть страницу сайта в браузере с DevTools, найти новые селекторы
4. Локально запустить `scripts/probe.py` для дебага
5. Обновить `src/scrapers/<site>.py`, `git push`, на VPS `git pull` + `systemctl restart`

### Disk full

```bash
df -h /opt
# Что съедает:
sudo du -sh /opt/pharmacy-monitor/{logs,data,reports}/*

# Чистка старых отчётов
sudo find /opt/pharmacy-monitor/reports/ -mtime +30 -delete

# Чистка старых бэкапов
sudo find /opt/pharmacy-monitor/data/backups/ -mtime +90 -delete
```

---

## 🔄 Обновления / деплой нового кода

```bash
# На VPS
cd /opt/pharmacy-monitor
sudo -u pharmacy git pull origin main
sudo -u pharmacy /home/pharmacy/.local/bin/uv sync --no-dev
# Применить миграции (lightweight, ALTER TABLE)
sudo -u pharmacy /home/pharmacy/.local/bin/uv run pharmacy-monitor init-db

# Перезапустить сервисы
sudo systemctl restart pharmacy-monitor-dashboard
sudo systemctl restart pharmacy-monitor-telegram

# Проверка
sudo journalctl -u pharmacy-monitor-dashboard -n 20
```

---

## 📊 Регулярные проверки

Раз в неделю:
- [ ] `health-check` без issues
- [ ] Email-отчёты приходят клиенту
- [ ] Backups появляются в `/data/backups/`
- [ ] Логи в `/logs/` ротируются

Раз в месяц:
- [ ] Disk usage не растёт неконтролируемо
- [ ] Streamlit & Telegram bot uptime ≥99%
- [ ] Случайный sample отчёта — данные осмысленные

---

## 📞 Контакты для эскалации

- Скрейпер сломался → разработчик (auto-alert через email)
- VPS недоступен → хостинг (Hetzner/DigitalOcean)
- Email не идёт → Gmail support / новый App Password
- Telegram молчит → @BotFather
