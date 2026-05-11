# Local Mac runtime для pharmonline scrape

Pharmonline.az **И** aptekonline.az оба забанили Hetzner DE IP в конце апреля
2026 (HTTP 403 даже на JSON API). Только **aloe** продолжает крутиться на проде
(systemd-таймер `pharmacy-monitor-scrape@aloe`). **Pharmonline + aptekonline
скрейпятся с MacBook через ежедневный launchd job** — твой Baku-IP не банен.

## Архитектура

```
MacBook (Asia/Baku)                       Hetzner prod (46.225.149.52)
┌──────────────────────────────┐          ┌─────────────────────────┐
│ launchd 18:00 ежедневно      │          │ systemd timer:          │
│   └─ run-scrape.sh           │          │   aloe @ 03:00 UTC      │
│      ├─ ssh -L 5433:5432 ────────────▶  │                         │
│      └─ pharmacy-monitor run │          │ PostgreSQL :5432  ◀─────┘
│         --site pharmonline   │          │ ↑ tunneled via SSH
│         --site aptekonline   │          │
│         --mode category      │          │
└──────────────────────────────┘          └─────────────────────────┘
```

## Установка (один раз)

### 1. SSH-ключ

`~/.ssh/id_ed25519` уже должен пускать тебя на prod как root. Проверь:
```bash
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52 'echo ok'
```

### 2. DB-пароль в Keychain

Возьми Postgres-пароль для пользователя `pm` из `data/.prod-secrets-DO-NOT-COMMIT`
(поле `PG_PASS=...`) и положи в macOS Keychain — он будет доступен скрипту без
ввода пароля при login сессии:

```bash
# одним движением — заменит существующий item если уже добавлял раньше
security add-generic-password -U \
  -a pm \
  -s pharmacy-monitor-db \
  -w '<paste PG_PASS here>'
```

Проверь:
```bash
security find-generic-password -s pharmacy-monitor-db -w
# должен напечатать пароль
```

### 3. Тестовый запуск без launchd

Прогон вручную, видишь все логи в терминале:
```bash
cd "/Users/rashadrahimov/Documents/comparison products"
bash infra/local/run-scrape.sh --site pharmonline --mode category --limit 5 --no-alerts
tail -50 ~/Library/Logs/pharmacy-monitor.log
```

Должен:
- Открыть SSH-туннель Mac:5433 → prod:5432
- Запустить `pharmacy-monitor run`, который пройдётся по pharmonline-категориям
- Записать price_snapshots в Postgres на проде
- Закрыть туннель

Проверь в проде что новые snapshots появились:
```bash
ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52 \
  "sudo -u pm psql -d pharmacy_monitor -c \
  \"SELECT COUNT(*) FROM price_snapshots WHERE created_at > NOW() - INTERVAL '5 min';\""
```

### 4. Установить launchd unit

```bash
cp infra/local/com.pharmacy-monitor.scrape.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist
launchctl list | grep pharmacy-monitor
# ожидание: пара (-, 0, com.pharmacy-monitor.scrape) — ещё не запускался
```

`launchctl load -w` сохранит unit между ребутами Mac.

### 5. Запустить вручную (без ожидания расписания)

```bash
launchctl start com.pharmacy-monitor.scrape
sleep 30
tail -50 ~/Library/Logs/pharmacy-monitor.log
```

## Расписание

`StartCalendarInterval` в .plist — 18:00 **локального** времени Mac
(Asia/Baku = UTC+4). При закрытом ноуте launchd не firing'ит, но запустит
job при следующем wake-up если время уже прошло (`StartCalendarInterval`
догоняет пропущенные firing-времена при `RunAtLoad=false` тоже,
лучше set `RunAtLoad=true` если этого не достаточно).

Менять время — отредактируй `<key>Hour</key>...<key>Minute</key>` в .plist,
потом:
```bash
launchctl unload -w ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist
cp infra/local/com.pharmacy-monitor.scrape.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist
```

## Удаление

```bash
launchctl unload -w ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist
rm ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist
security delete-generic-password -s pharmacy-monitor-db
```

## Troubleshooting

| Симптом | Что проверить |
|---|---|
| `keychain item not found` | Шаг 2: `security add-generic-password ...` выполнен с правильным `-s` именем |
| `tunnel did not come up after 10s` | `ssh -i ~/.ssh/id_ed25519 -l root 46.225.149.52` пускает? Может 22 порт fw'нулся |
| `psycopg.OperationalError: password authentication failed` | Пароль в Keychain устарел. Перезапиши значением из `data/.prod-secrets-DO-NOT-COMMIT` |
| `category_failed: HTTP 403` всё ещё | Pharmonline вкатил Baku-IP-бан. Поищи через VPN, либо вернись к ScraperAPI — env-vars в base.py уже подтянут конфиг |
| launchd запускает но скрейп пустой | `launchctl list` → проверь Last Exit Status; `tail ~/Library/Logs/pharmacy-monitor-launchd.log` для bootstrap-ошибок |

## Связанные файлы

- [run-scrape.sh](run-scrape.sh) — сам wrapper
- [com.pharmacy-monitor.scrape.plist](com.pharmacy-monitor.scrape.plist) — launchd config
- [../../CLAUDE.md](../../CLAUDE.md) — runtime layout верхнего уровня
- [../../src/scrapers/aptekonline.py](../../src/scrapers/aptekonline.py) — JSON API скрейпер (на проде, не сюда)
