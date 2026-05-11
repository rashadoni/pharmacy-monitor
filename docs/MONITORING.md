# Monitoring (W11)

3 уровня наблюдаемости в Pharmacy Monitor production:

1. **Errors** — Sentry (catch unhandled exceptions, send to UI with stack + breadcrumbs)
2. **Metrics** — Prometheus (`/metrics` endpoint) → Grafana dashboards
3. **Logs** — structured JSON via systemd journal → optional Better Stack / Loki shipping
4. **Uptime** — Uptime-Kuma probes `/health` from external location

## Setup

### 1. Sentry (errors)

Free tier: 5K events/month — хватит для пилота.

```bash
# 1. Создать проект на sentry.io (Python platform)
# 2. Скопировать DSN и добавить в /etc/pharmacy-monitor/env:
SENTRY_DSN=https://xxx@oXXXXX.ingest.sentry.io/XXXXXXX
SENTRY_ENVIRONMENT=production
SENTRY_TRACES_SAMPLE_RATE=0.1
SENTRY_PROFILES_SAMPLE_RATE=0.1

# 3. Перезапустить сервисы — Sentry init at module import
sudo systemctl restart pharmacy-monitor-api
```

Что отправляется:
- Все unhandled exceptions из FastAPI (через FastApiIntegration)
- DB query errors (через SqlalchemyIntegration)
- Custom captures: `from src.observability import sentry_capture; sentry_capture(exc, site="pharmonline")`
- Tag `tenant_id` автоматически на каждом authenticated request

Что фильтруется (`_sentry_before_send`):
- KeyboardInterrupt / SystemExit (CLI shutdown)
- ConnectionResetError (network noise)
- CaptchaDetected (expected, logged separately)

### 2. Prometheus (metrics)

```bash
apt install prometheus prometheus-node-exporter
cp /opt/pharmacy-monitor/infra/prometheus.yml /etc/prometheus/prometheus.yml
systemctl restart prometheus
```

Verify:
```bash
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[].health'
# Expected: ["up", "up"]

curl http://localhost:8080/metrics | head -30
# Expected output: # HELP pharmacy_scrape_products_total ...
```

### Custom metrics

| Metric | Type | Labels | What |
|---|---|---|---|
| `pharmacy_scrape_duration_seconds` | Histogram | site | Per-site scrape time (full session) |
| `pharmacy_scrape_products_total` | Counter | site | Cumulative products scraped |
| `pharmacy_scrape_failures_total` | Counter | site, reason | Scrape errors (exception, timeout) |
| `pharmacy_captcha_hits_total` | Counter | site | Captcha / bot-wall encounters |
| `pharmacy_matcher_clusters_total` | Counter | — | Cross-site clusters created |
| `pharmacy_alert_events_total` | Counter | severity, rule_type | Alert events fired |
| `pharmacy_runs_total` | Counter | site, status | Pharmacy-monitor runs (ok/failed) |
| `pharmacy_db_query_seconds` | Histogram | query_type | DB query duration |
| `pharmacy_app_info` | Info | version, env, git_sha | Static app info |

### 3. Grafana

```bash
apt install grafana
systemctl enable --now grafana-server
# UI: http://server:3000 (admin/admin)
```

Add Prometheus as data source:
- Settings → Data sources → Add → Prometheus
- URL: `http://localhost:9090`
- Save & Test

Import dashboard:
- Dashboards → Import → Upload JSON file → `infra/grafana/dashboard.json`
- Select Prometheus as data source

10 panels: Products / Captcha hits / Matcher / Critical alerts (last 24h KPIs), Scrape duration p95, Products rate, Alert events, Captcha by site, DB p95, App version.

### 4. Logs (structured JSON to journald)

Уже включено через systemd `StandardOutput=journal` в всех unit'ах. Просмотр:

```bash
# Live tail
journalctl -u pharmacy-monitor-api -f

# Filter по уровню
journalctl -u pharmacy-monitor-api -p warning

# JSON parse
journalctl -u pharmacy-monitor-scrape@pharmonline --output=cat | jq 'select(.event=="captcha_detected")'

# Export to Loki / Better Stack — настраивается через promtail (см. их docs)
```

Кастомный файл-лог (опционально):
```bash
# В /etc/pharmacy-monitor/env
LOG_FILE=/var/log/pharmacy-monitor/app.jsonl
LOG_FILE_MAX_MB=50
LOG_FILE_BACKUPS=5
```

### 5. Uptime monitoring (Uptime-Kuma)

Внешняя проверка `/health` каждую минуту, alert в Telegram если down.

```bash
# Поднять Uptime-Kuma на отдельной VM (или Docker на той же)
docker run -d --restart=always -p 3001:3001 \
    -v uptime-kuma:/app/data \
    --name uptime-kuma louislam/uptime-kuma:1

# UI: http://kuma-host:3001
```

Создать монитор:
- Type: HTTP(s)
- URL: `https://your-domain.com/health`
- Heartbeat interval: 60s
- Retry: 3
- Notification: Telegram (chat_id берётся из TELEGRAM_CHAT_ID admin'а)

### 6. Quick health-check checklist

```bash
# Все 4 backend сервиса работают:
systemctl status pharmacy-monitor-api pharmacy-monitor-telegram-bot pharmacy-monitor-scrape@pharmonline.timer

# Метрики экспортируются:
curl -s http://localhost:8080/metrics | grep -c '^pharmacy_'    # > 0

# Sentry получает события (test):
curl -X POST http://localhost:8080/api/v1/products  # 401 — должно появиться в Sentry если включен

# Алерт-цепочка работает (если SMTP / Telegram настроены):
.venv/bin/pharmacy-monitor alert evaluate --dispatch
```

## Troubleshooting

| Симптом | Причина | Fix |
|---|---|---|
| `/metrics` пустой | prometheus_client не установлен | `uv sync` |
| Sentry не получает events | DSN неверный или env_var не загружен | `journalctl -u pharmacy-monitor-api -p warning \| grep sentry` |
| Grafana shows "No data" | Prometheus не скрейпает API | Targets на :9090 health |
| Captcha hits растут | IP заблокирован | См. `docs/PILOT_OPERATIONS.md → Scrape robustness` |
| Логов нет в journal | `StandardOutput=journal` не в unit | `cat /etc/systemd/system/pharmacy-monitor-api.service` |
