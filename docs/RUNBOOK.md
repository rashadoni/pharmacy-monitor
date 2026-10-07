# Pharmacy Monitor — Production Runbook

Operational manual для VPS-инсталляции. Используется когда что-то ломается.

> **Боевой сервер с 2026-09-03 — Contabo `13.140.186.143`
> (`vmi3552946.contaboserver.net`).** Прежний Hetzner-сервер удалён; адрес в
> командах ниже заменён 2026-10-07. Сами разделы тогда целиком не
> перепроверялись, и помечены как устаревшие лишь некоторые. Раздел, где
> встречаются пользователь `pharmacy`, `uv run`, `provision_vps.sh`, SQLite или
> сервисы `pharmacy-monitor-dashboard` / `-telegram` / `-run`, описывает раннюю
> версию системы, даже если пометки на нём нет. Host key сервера прошит в
> `infra/prod_known_hosts`. На машине, которая сервер ещё не знает, сначала
> добавить этот ключ (из корня репозитория):
> `grep -v '^#' infra/prod_known_hosts >> ~/.ssh/known_hosts` — и только потом
> выполнять команды ниже. На вопрос ssh «принять ключ?» не соглашаться: так
> принимается то, что предъявила сеть.

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

Health-email работает как состояние инцидента, даже если проверка запускается
каждый час:

- новая проблема или изменение набора/уровня проблем отправляется сразу;
- неизменившийся инцидент напоминается не чаще одного раза в 24 часа
  (`--alert-cooldown-hours` меняет интервал);
- первый успешный health-check после активного инцидента отправляет одно письмо
  `Pharmacy Monitor — RECOVERED`; следующие успешные проверки молчат;
- состояние хранится атомарно в `data/health_alert_state.json` (или в
  `HEALTH_ALERT_STATE_FILE`) и обновляется только после подтверждённой SMTP-отправки.

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

### Email — слишком много писем (volume controls)

**Симптом:** клиент жалуется, что писем приходит больше, чем ожидает (напр.
«поставил дайджест раз в неделю, а капает по несколько раз в день»).

**Контекст:** на `EMAIL_TO`/recipients идут НЕСКОЛЬКО независимых потоков, и
per-user тумблеры daily/weekly (`tenant_users`, страница «Пользователи»)
управляют ТОЛЬКО per-user дайджестом. Остальные — отдельные механизмы:

| Поток | Источник | Как выключить | Как включить обратно |
|---|---|---|---|
| Мгновенные undercut/price-change алерты | Верифицированный полный `run` → `evaluate_rules` → `dispatch_events_batch` | отключить правило или получателя в UI; расписание не должно добавлять `--no-alerts` | включить правило/получателя и применить `install_systemd_schedule.sh` |
| Priority watchlist price-change | `watchlist-tick` каждые 3ч по confirmed URL; только локальная цена того же SKU | `systemctl disable --now pharmacy-monitor-watchlist.timer` | добавить confirmed URL и включить timer |
| Полный отчёт скрейпа (HTML+Excel, тема «Pharmacy Monitor DD.MM.YYYY — N undercuts») | `run_cmd` в конце КАЖДОГО не-hourly прогона — nightly **и intraday-tick** (`hourly=False`, до ~13/день 05–17 UTC) | `SCRAPE_REPORT_EMAIL=0` в `/etc/pharmacy-monitor/env` (next scrape) | `SCRAPE_REPORT_EMAIL=1` или убрать строку |
| Daily-дайджест (тема «Дайджест за сегодня», топ-N за 24ч) | `digest@daily.timer` → `pharmacy-monitor digest` → `src/digest.py` | `systemctl disable --now pharmacy-monitor-digest@daily.timer` | `systemctl enable --now …` |
| Недельный per-user дайджест | `digest-weekly.timer` Пн 06:00 UTC → `notify digest weekly` → `src/notifications.py` (`tenant_users.weekly_digest`) | per-user тумблер в UI / `systemctl disable …` | UI / `systemctl enable …` |

**Важно — два разных тумблера, два разных отката:** отчёт-письмо гасится
**env-флагом**, daily-дайджест — **состоянием systemd-юнита**. При откате нужно
вернуть ОБА (легко забыть один).

`price_change_pct` сообщает и о росте, и о снижении цены; `price_drop_pct`
нужен только когда снижение требует отдельной политики. Не включайте оба
правила на один и тот же сайт/порог, если не хотите два сообщения о снижении.

**Per-user дайджест — сводка, а не строка на событие (с 2026-10-07).** Письмо
`notify digest daily|weekly` (и кнопка «отправить дайджест» в Quick Actions)
собирает `_render_digest_email` в `src/notifications.py`:

- наверху — сводка: по строке на тип события с числом, разбивкой по важности и
  по сайтам;
- ниже — события строками, не больше `_DIGEST_TYPE_CAP` (30) на тип и
  `_DIGEST_ROW_BUDGET` (60) на письмо. Строки раздаются по важности: сначала
  критичные всех типов, потом предупреждения, потом информационные; внутри одной
  важности — поровну между типами, внутри типа — самые крупные по проценту;
- информационный тип (новые товары, «можно поднять цену») идёт строками, только
  если помещается целиком, иначе остаётся числом;
- события ниже `email_severity_min` получателя строками не идут никогда —
  только числом в сводке. Поэтому у получателей с разным порогом письма разные;
- всё, что не попало в строки, открывается по ссылке на `/alerts` с тем же
  окном и фильтром по типу. Страница открывается на «Входящих», поэтому
  прочитанные и отложенные события там не видны, и число может не совпасть.

До этого письмо клало строку на каждое событие за окно: 2026-10-05 оно ушло на
1 229 строк и 626 КБ (Gmail обрезает письмо после 102 КБ). Если клиент спросит,
куда делся список новых товаров, — он в дашборде, в письме осталось число.
Письмо о прогоне (`dispatch_events_batch`) потолка по-прежнему не имеет.

Посмотреть письмо на живых данных, не отправляя его клиенту:

```bash
# на сервере, под pm, с загруженным env (systemd-EnvironmentFile вручную не грузится)
set -a; source /etc/pharmacy-monitor/env; set +a
# ничего не шлёт: тема, размер и число строк на каждого получателя — в логе
.venv/bin/pharmacy-monitor notify digest weekly --dry-run
# шлёт одному адресу из тех, у кого дайджест включён
.venv/bin/pharmacy-monitor notify digest weekly --only admin@example.com
```

### Firecrawl fallback для Pharmonline

Decodo остаётся основным источником. Автоматический workflow переходит на
Firecrawl **только** после трёх неуспешных свежих попыток Decodo и лишь при
явном включении в `/etc/pharmacy-monitor/env`:

```bash
FIRECRAWL_API_KEY=fc-...  # секрет, не коммитить
PHARMONLINE_PUBLIC_API_FIRECRAWL_FALLBACK=required
PHARMONLINE_FIRECRAWL_MAX_REQUESTS=140
```

Fallback читает `rawHtml` API/ sitemap c `maxAge=0`, `storeInCache=false` и
`proxy=basic`. Каждый ответ обязан быть HTTP 200, basic и ровно 1 credit;
изменение тарифа, cache/advanced-proxy или неполный sitemap отклоняют весь
прогон без обновления каталога. На текущем каталоге ожидается до 131 запроса
за полный проход; лимит 140 — предохранитель, а не цель для расхода.

```bash
# Текущее состояние всех email-потоков
ssh root@13.140.186.143 '
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

### Поиск на странице сравнения не находит товар / находит не сразу

Поиск, подсказки и выгрузка в Excel на `/comparison` работают через индекс
названий в памяти процесса API (`src/catalog_search.py`), а не через SQL.

- **У каждого воркера свой индекс.** Он собирается при старте воркера в фоне
  (в журнале `catalog_search_index_built products=… seconds=…`) и обновляется
  тоже в фоне: когда завершился прогон или появились новые товары, но не чаще
  раза в минуту, и в любом случае раз в 30 минут. Запрос обновление не ждёт —
  до его конца отдаётся прежний индекс.
- **Товар только что появился в базе, а поиск его не видит** — это нормально в
  пределах минуты. Дольше 30 минут — смотреть журнал на
  `catalog_search_index_refresh_failed`. Сбросить индекс принудительно можно
  только рестартом `pharmacy-monitor-api`: `catalog_search.reset_cache()`
  действует на тот процесс, где вызван, скрипт обслуживания воркеры не сбросит.
- **В индексе только названия и бренды.** Цены, пары и наличие читаются из базы
  на каждый запрос, поэтому устаревший индекс не может показать неверную цену
  или чужого арендатора — только не найти новый товар.
- **Товар найден, но в блоке «найдено на сайтах, но не в сравнении»** — у него
  нет пары на другом сайте (или пара скрыта фильтром «на N сайтах»). Это вопрос
  сопоставления, а не сбора: связать вручную можно на странице «Подбор матчей».
- **Память:** около 50 МБ на воркер при 45 тыс. товаров, вдвое больше на время
  пересборки.

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

> ⚠️ Список ниже и раздел «Восстановление из бэкапа» — времён SQLite, устарели
> (помечено 2026-10-07). Сейчас: Postgres, `pg_dump` каждую ночь в 04:00 по
> времени сервера, `/var/backups/pharmacy-monitor/`, хранение 14 дней — см.
> «Сделать бэкап вручную» и «Копия бэкапа вне сервера».

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
# Postgres pg_dump → /var/backups/pharmacy-monitor/ (+ GPG если задан BACKUP_GPG_PASSPHRASE,
# + B2 offsite если заданы B2_APPLICATION_KEY_ID/KEY):
sudo systemctl start pharmacy-monitor-backup.service
# или напрямую: sudo bash /opt/pharmacy-monitor/infra/scripts/backup.sh
```

### Копия бэкапа вне сервера

Сервер хранит 14 дней дампов только на собственном диске. B2 не настроен
(`B2 upload skipped` в journald сервиса), других заданий, уносящих дамп наружу,
на сервере нет. **На 2026-10-07 копии вне сервера нет.** Наружу дамп уходит
только вручную:

```bash
bash infra/local/fetch-backup.sh --list   # что лежит на сервере
bash infra/local/fetch-backup.sh          # забрать свежий в ~/Backups/pharmacy/
```

Запускать с машины, чей SSH-ключ принимает сервер (сейчас это dev-бокс). Он
тоже стоит у Contabo: копия там переживёт потерю машины, но не аккаунта —
настоящая внешняя копия должна лежать ещё где-то. Скрипт предупредит, если
ночной бэкап пропустил хотя бы одну ночь (самый свежий файл на сервере —
позавчерашний или старше): однажды он уже месяц молча падал.
Дамп зашифрован `BACKUP_GPG_PASSPHRASE` из `/etc/pharmacy-monitor/env`: без
копии этого пароля вне сервера забранный файл не расшифровать. Проверять
расшифровкой до конца, а не наличием файла (команда молча ждёт пароль на stdin:
ввести его и нажать Enter; обрезанный файл даст ошибку, а не «дамп цел»):

```bash
gpg --batch --yes --passphrase-fd 0 -d <файл>.sql.gz.gpg | gunzip -t && echo "дамп цел"
```

Проверка паролем, прочитанным с сервера, доказывает только целость дампа. Что
пароль есть и вне сервера, она не доказывает — а на 2026-10-07 это не
установлено.

Восстановление — в отдельную пустую базу, не в боевую. На этом сервере оно ни
разу не проверялось, команды ниже собраны по тому, как устроен дамп:

```bash
# НЕ направлять в pharmacy_monitor: дамп сделан с --clean и начинается с удаления
# всех таблиц, а без ON_ERROR_STOP psql пройдёт мимо ошибок и оставит смесь
# старого и нового.
sudo -u postgres createdb -O pm pharmacy_monitor_restore
gpg --batch --yes --passphrase-fd 0 -d <файл>.sql.gz.gpg | gunzip > restore.sql
# Подключаться как pm, а не как postgres: дамп снят с --no-owner, и таблицы
# достанутся тому, кто восстанавливает.
psql -v ON_ERROR_STOP=1 --single-transaction \
  "postgresql://pm:<пароль>@localhost:5432/pharmacy_monitor_restore" < restore.sql
```

Переключение приложения на восстановленную базу (остановить API и таймеры,
подменить базу) здесь не описано — такого учения не было.

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
ssh root@13.140.186.143 'grafana cli admin reset-admin-password <YOUR_PASSWORD>'
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

> ⚠️ Шаги ниже — времён SQLite (помечено 2026-10-07): `.sqlite.gz` больше не
> существует, «восстановить, см. выше» ведёт в устаревший раздел. Сейчас база —
> Postgres, дамп лежит только на самом сервере; что есть и чего нет вне его —
> «Копия бэкапа вне сервера».

1. Поднять новый VPS
2. Запустить `provision_vps.sh` (см. Начальная настройка)
3. Если есть бэкап с предыдущего сервера (S3 / отдельный диск):
   - Загрузить .sqlite.gz в `/opt/pharmacy-monitor/data/backups/`
   - Восстановить (см. выше)

**Recommendation:** настроить off-site бэкап (S3, Backblaze B2, dropbox).

### БД повреждена

> ⚠️ Устарело (помечено 2026-10-07): команда для SQLite, база давно Postgres.

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

> ⚠️ Устарело (помечено 2026-10-07): прод — не git-чекаут, а rsync-снимок, и
> сервисы называются иначе (`pharmacy-monitor-api`,
> `pharmacy-monitor-frontend`). Выкладка — workflow `deploy.yml`
> (`gh workflow run deploy.yml --ref main -f apply_migrations=false`) после
> зелёного CI.

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
- VPS недоступен → хостинг (Contabo)
- Email не идёт → Gmail support / новый App Password
- Telegram молчит → @BotFather

---

## 🌐 Scraper proxy chain + DDP recovery (2026-05-28)

Текущий runtime:

| Сайт | Где | Чем | Proxy |
|---|---|---|---|
| **pharmonline.az** | прод (Contabo) | **Meteor DDP WebSocket** (`src/scrapers/pharmonline_ddp.py`) | Decodo AZ residential (IPRoyal only if Decodo is disabled) |
| **aloe.az** | прод (Contabo) | RSC/HTTP parser (`src/scrapers/aloe.py`) | Direct (no proxy needed) |
| **aptekonline.az** | прод (Contabo) | httpx JSON API (`src/scrapers/aptekonline.py`) | Decodo AZ residential |

Колонка «Где» обновлена 2026-10-07. Способы сбора и прокси в таблице тогда не
перепроверялись; действующее расписание — CLAUDE.md, «Расписание сбора».

Mac scraping is retired. `com.pharmacy-monitor.scrape` and
`com.pharmacy-monitor.watch` should remain unloaded/disabled; the scripts under
`infra/local/` are fail-closed unless `PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1` is
set for an explicit disaster-recovery run.

### DDP recovery procedures

**`catalog_verification_reason=pending` + failed full run**:
- прогон оборвался до проверки каталога. Само значение `pending` не отличает
  DDP startup от более позднего исключения до verification; `site_drop` при
  этом является следствием старых данных, а не причиной падения;
- для run `#656` публичный `/health` не содержит `error_message`, поэтому точную
  причину брать только из строки Run или journal. Исторический run `#629` падал
  на `timed out during opening handshake`, но это лишь диагностическая гипотеза
  для `#656`, пока не прочитан его собственный error;
- сначала посмотреть сохранённый `Run.error_message` и
  `journalctl -u pharmacy-monitor-scrape@pharmonline.service --since '9 days ago'`;
- проверить только безопасные признаки конфигурации: что `pharmonline` входит в
  `DECODO_SITES`, заданы оба credential-поля, доступны все порты из
  `DECODO_PORTS`, а в аккаунте Decodo есть баланс. Значения credentials не
  печатать и не передавать в issue/log;
- HTTP 402/407 означает баланс/credentials: исправить аккаунт/config и выполнить
  один штатный full run. Таймаут после перебора всех sticky-портов означает
  проблему WebSocket tunnel/exit pool; не маскировать её повышением health-порога;
- восстановление подтверждено только когда full run имеет `status=ok`,
  `catalog_verified=true`, а `MAX(products.last_seen_at)` для pharmonline свежий.

Startup timeout/OSError/402/407 теперь превращается в sanitized
`SiteScrapeFatalError: DDP startup failed: ...` без исходной exception-chain
(`raise ... from None`). Это важно: proxy-библиотека может включить сырой URL с
credentials в исключение, а `run_cmd` печатает traceback через `log.exception`.
`scrape_site` перехватывает этот тип и возвращает структурированный
`ScrapeResult(site_fatal=true)`, поэтому параллельный успешный сайт не отменяется
через `asyncio.gather`. Первая безопасная причина сохраняется в `run_quality` и
`Run.error_message` в ограниченном размере.

Классификация выполняется до full-catalog verification: если все запрошенные
сайты (в том числе единственный Pharmonline) завершились `site_fatal`, run имеет
статус `failed`; если хотя бы один другой сайт отдал пригодный результат, общий
run имеет статус `degraded`, сохраняет этот результат и затем fail-closed
останавливается на проверке полного каталога. Не выбрасывать из `__aenter__`
другой startup-тип, который обойдёт `scrape_site`: он отменит peer-задачи и
потеряет уже полученные здоровые результаты.

**HTTP 403 на WebSocket handshake** (наблюдалось 2026-05-27):
- Cloudflare/IPRoyal session ban после high-volume scrape
- Wait 5-10 мин, retry — IPRoyal session rotation помогает
- Если повторяется: `systemctl restart pharmacy-monitor-scrape@pharmonline`
- Если упорно: проверить баланс/доступ paid proxy и DDP reconnect logs; Mac
  launchd не является штатным fallback.

**`ConnectionClosedError: no close frame received or sent`** во время persist phase:
- Это нормально — DDP server тайм-аутит ping pong когда event loop долго блокирован
- `_DDPClient` имеет reconnect-on-close (Phase 1c.4) — auto-recover, retry 1 раз с fresh session
- Если retry тоже падает → check `journalctl -u pharmacy-monitor-scrape@pharmonline`

**Меняем proxy провайдер**:
```bash
# Update /etc/pharmacy-monitor/env on prod (root only)
# IPROYAL_USERNAME, IPROYAL_PASSWORD, IPROYAL_HOST=geo.iproyal.com:12321
# BRIGHTDATA_USERNAME, BRIGHTDATA_PASSWORD, BRIGHTDATA_HOST
# SCRAPER_API_KEY (fallback)
# Restart scrape:
systemctl restart pharmacy-monitor-scrape@pharmonline
```

Priority order in `src/scrapers/base.py`:
1. IPRoyal residential (primary для pharmonline)
2. Bright Data Web Unlocker (secondary, для aptekonline)
3. ScraperAPI default pool (3rd-priority, free tier)
4. Direct connection (fallback)

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
ssh root@13.140.186.143 'cd /opt/pharmacy-monitor && \
  sudo -u pm bash -c "set -a; source /etc/pharmacy-monitor/env; \
  .venv/bin/pharmacy-monitor intraday-tick --dry-run"'

# Real tick
ssh root@13.140.186.143 'systemctl start pharmacy-monitor-intraday.service'
```

### Inspect rotation state (Redis)

```bash
ssh root@13.140.186.143 '
  redis-cli get "intraday:rotation:idx"  # current index
  redis-cli ttl "intraday:lock:site:pharmonline"  # TTL до next tick allowed
  redis-cli ttl "intraday:lock:site:aloe"
'
```

### Reset rotation (если зависло)

```bash
ssh root@13.140.186.143 '
  redis-cli del "intraday:rotation:idx" "intraday:lock:site:pharmonline" "intraday:lock:site:aloe"
'
# Next tick перезапустится с idx=1
```

### Disable intraday (если жрёт proxy credits)

```bash
ssh root@13.140.186.143 'systemctl disable --now pharmacy-monitor-intraday.timer'
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

> ⚠️ **Устарело с 2026-09-03 (помечено 2026-10-07).** Агент на Маке смотрит на
> удалённый Hetzner-сервер. Из команд ниже нужна одна — `bootout` (выгрузить),
> и сразу за ней `rm ~/Library/LaunchAgents/com.pharmacy-monitor.db-tunnel.plist`:
> без удаления файла агент загрузится снова при следующем входе в систему.
> `bootstrap` не выполнять. Копия plist в репозитории обезврежена, см.
> `infra/local/README.md`.

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

## 🧪 Restore drill — verified 2026-05-27

**Recovery scenario**: восстановить frontend после failed deploy (i18n rollback experience).

> ⚠️ **Устарело — на текущем сервере не выполнять (помечено 2026-10-07).**
> Учение проверено только на прежнем Hetzner-сервере. На Contabo архивов
> `frontend-src-pre-*.tgz` нет: шаг 2 сначала удаляет `frontend/src`, а
> распаковывать после этого нечего. Адрес в командах заменён заглушкой
> `<server>` намеренно. Отдельного отката одного фронтенда сейчас нет:
> `deploy.yml` выкладывает бэкенд и фронтенд вместе и откажется работать, если
> ревизии базы прода нет в выбранном коммите. Сломанный фронтенд чинится новым
> коммитом через PR и обычной выкладкой.

```bash
# 1. Find latest pre-deploy backup
LATEST=$(ssh root@<server> 'ls -t /var/backups/pharmacy-monitor/frontend-src-pre-*.tgz | head -1')
echo "Will restore from: $LATEST"

# 2. Restore
ssh root@<server> '
  cd /opt/pharmacy-monitor
  rm -rf frontend/src
  tar xzf '"$LATEST"'
  chown -R pm:pm frontend/src
'

# 3. Rebuild + restart
ssh root@<server> '
  cd /opt/pharmacy-monitor/frontend
  sudo -u pm bash -c "NODE_OPTIONS=--max-old-space-size=4096 pnpm build"
  systemctl restart pharmacy-monitor-frontend
'

# 4. Verify
curl -sSI https://leaddrive.cloud/login | head -2  # should be HTTP/2 200
```

**RTO measured**: ~3-5 минут. Backups создаются автоматически перед deploy через ExecStartPre hook + manual `tar czf` step в deploy скриптах.
