#!/usr/bin/env bash
# Pharmacy Monitor — VPS provisioning script
#
# Один раз запускается на свежей Ubuntu 22.04 / 24.04 (Hetzner CX11 / DigitalOcean Basic
# хватит за глаза). Делает:
#   1. Обновляет систему, ставит зависимости
#   2. Создаёт пользователя `pharmacy` (без sudo)
#   3. Клонит проект в /opt/pharmacy-monitor
#   4. Ставит uv + Python + Playwright chromium
#   5. Создаёт systemd-таймер для ежедневного прогона + почасовой health-check
#   6. Настраивает nginx + basic-auth для дашборда
#   7. Опционально — Let's Encrypt (если задан DOMAIN)
#
# USAGE:
#   sudo bash provision_vps.sh \
#     --repo https://github.com/USER/comparison-products.git \
#     --domain monitor.pharmonline.az \
#     --email admin@pharmonline.az \
#     --dashboard-user admin \
#     --dashboard-pass 'StrongPassword123'
#
# Все флаги опциональны кроме --repo.
# После завершения скрипт напечатает что нужно дописать в /opt/pharmacy-monitor/.env
# (SMTP-креды и пр.) и команду для теста.

set -euo pipefail

# ============================================================================
# Аргументы
# ============================================================================
REPO_URL=""
DOMAIN=""
LETSENCRYPT_EMAIL=""
DASHBOARD_USER="admin"
DASHBOARD_PASS=""
INSTALL_DIR="/opt/pharmacy-monitor"
SERVICE_USER="pharmacy"
RUN_HOUR="${RUN_HOUR:-6}"  # AZT по умолчанию

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO_URL="$2"; shift 2;;
        --domain) DOMAIN="$2"; shift 2;;
        --email) LETSENCRYPT_EMAIL="$2"; shift 2;;
        --dashboard-user) DASHBOARD_USER="$2"; shift 2;;
        --dashboard-pass) DASHBOARD_PASS="$2"; shift 2;;
        --install-dir) INSTALL_DIR="$2"; shift 2;;
        --user) SERVICE_USER="$2"; shift 2;;
        --run-hour) RUN_HOUR="$2"; shift 2;;
        *) echo "Unknown flag: $1" >&2; exit 1;;
    esac
done

if [[ -z "$REPO_URL" ]]; then
    echo "ERROR: --repo обязателен" >&2
    echo "Пример: sudo bash $0 --repo https://github.com/user/comparison-products.git" >&2
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: запускай через sudo" >&2
    exit 1
fi

if [[ -z "$DASHBOARD_PASS" ]]; then
    DASHBOARD_PASS="$(openssl rand -base64 16 | tr -d '=+/')"
    echo "ℹ️  Dashboard пароль не задан — сгенерирован: $DASHBOARD_PASS"
    echo "    Сохраните его сейчас!"
fi

echo "================================================================"
echo "  Pharmacy Monitor — VPS provisioning"
echo "================================================================"
echo "  Repo:         $REPO_URL"
echo "  Install dir:  $INSTALL_DIR"
echo "  Service user: $SERVICE_USER"
echo "  Domain:       ${DOMAIN:-(не задан, IP only)}"
echo "  Cron hour:    ${RUN_HOUR}:00 (UTC, настройте TZ если надо)"
echo "================================================================"
sleep 2

# ============================================================================
# 1. Системные пакеты
# ============================================================================
echo ""
echo "[1/7] Обновление системы и установка пакетов..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    ca-certificates curl git \
    nginx apache2-utils \
    python3 python3-venv \
    sqlite3 logrotate cron \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64

# ============================================================================
# 2. Сервис-юзер
# ============================================================================
echo ""
echo "[2/7] Создание пользователя $SERVICE_USER..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

# ============================================================================
# 3. Клонирование репозитория
# ============================================================================
echo ""
echo "[3/7] Клонирование репозитория в $INSTALL_DIR..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "  Уже клонирован — обновляю"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull
else
    rm -rf "$INSTALL_DIR"
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ============================================================================
# 4. uv + Python deps + Playwright
# ============================================================================
echo ""
echo "[4/7] Установка uv + Python deps + Playwright chromium..."
sudo -u "$SERVICE_USER" bash -c '
    set -e
    cd '"$INSTALL_DIR"'
    if ! command -v uv &> /dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="$HOME/.local/bin:$PATH"
    uv sync --no-dev
    uv run playwright install chromium --with-deps
    mkdir -p data logs reports
    if [[ ! -f .env ]]; then
        cp .env.example .env
        echo "  ⚠️  Создан .env — отредактируйте SMTP-креды!"
    fi
    uv run pharmacy-monitor init-db
'

# ============================================================================
# 5. systemd таймеры (ежедневный run + почасовой health-check)
# ============================================================================
echo ""
echo "[5/7] Установка systemd-юнитов..."

cat > /etc/systemd/system/pharmacy-monitor-run.service <<EOF
[Unit]
Description=Pharmacy Monitor — daily scrape + report
After=network.target

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=/home/$SERVICE_USER/.local/bin:/usr/bin:/bin"
# `run` без --mode — сбор каталога всех сайтов, что бы ни лежало в watchlist.
# Закреплённые ссылки этот юнит не обновляет: для них `pharmacy-monitor
# watchlist-tick`. Строку ниже читает tests/test_cadence_guard.py.
ExecStart=/home/$SERVICE_USER/.local/bin/uv run pharmacy-monitor run
StandardOutput=append:$INSTALL_DIR/logs/run.log
StandardError=append:$INSTALL_DIR/logs/run.log
EOF

cat > /etc/systemd/system/pharmacy-monitor-run.timer <<EOF
[Unit]
Description=Pharmacy Monitor — daily ${RUN_HOUR}:00

[Timer]
OnCalendar=*-*-* ${RUN_HOUR}:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/pharmacy-monitor-health.service <<EOF
[Unit]
Description=Pharmacy Monitor — hourly health-check + alert

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=/home/$SERVICE_USER/.local/bin:/usr/bin:/bin"
ExecStart=/home/$SERVICE_USER/.local/bin/uv run pharmacy-monitor health-check --alert-email --quiet-on-ok
StandardOutput=append:$INSTALL_DIR/logs/health.log
StandardError=append:$INSTALL_DIR/logs/health.log
EOF

cat > /etc/systemd/system/pharmacy-monitor-health.timer <<EOF
[Unit]
Description=Pharmacy Monitor — health-check каждый час

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/pharmacy-monitor-dashboard.service <<EOF
[Unit]
Description=Pharmacy Monitor — Streamlit dashboard
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=/home/$SERVICE_USER/.local/bin:/usr/bin:/bin"
ExecStart=/home/$SERVICE_USER/.local/bin/uv run streamlit run src/dashboard.py \
    --server.headless true \
    --server.port 8501 \
    --server.address 127.0.0.1 \
    --client.toolbarMode viewer
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# === Telegram bot — long-polling процесс ===
cat > /etc/systemd/system/pharmacy-monitor-telegram.service <<EOF
[Unit]
Description=Pharmacy Monitor — Telegram bot polling
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=/home/$SERVICE_USER/.local/bin:/usr/bin:/bin"
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=/home/$SERVICE_USER/.local/bin/uv run pharmacy-monitor telegram run-bot
Restart=on-failure
RestartSec=10
StandardOutput=append:$INSTALL_DIR/logs/telegram.log
StandardError=append:$INSTALL_DIR/logs/telegram.log

[Install]
WantedBy=multi-user.target
EOF

# === Postgres backup — ежедневно в 04:00 UTC ===
# Use infra/scripts/backup.sh (pg_dump → gzip → optional GPG + B2 offsite).
# NB: prod мигрировал SQLite → Postgres; старый scripts/backup.sh (SQLite) удалён.
cat > /etc/systemd/system/pharmacy-monitor-backup.service <<EOF
[Unit]
Description=Pharmacy Monitor — daily Postgres backup

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="INSTALL_DIR=$INSTALL_DIR"
ExecStart=/bin/bash $INSTALL_DIR/infra/scripts/backup.sh
EOF

cat > /etc/systemd/system/pharmacy-monitor-backup.timer <<EOF
[Unit]
Description=Pharmacy Monitor — daily backup at 04:00 UTC

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pharmacy-monitor-run.timer
systemctl enable --now pharmacy-monitor-health.timer
systemctl enable --now pharmacy-monitor-dashboard.service
systemctl enable --now pharmacy-monitor-backup.timer
# Telegram bot — стартуем только если в .env есть TELEGRAM_BOT_TOKEN
if grep -q "^TELEGRAM_BOT_TOKEN=." "$INSTALL_DIR/.env" 2>/dev/null; then
    systemctl enable --now pharmacy-monitor-telegram.service
    echo "  ✓ Telegram bot service запущен"
else
    echo "  ⏸  Telegram bot НЕ запущен (нет TELEGRAM_BOT_TOKEN в .env)"
    echo "     После добавления токена: sudo systemctl enable --now pharmacy-monitor-telegram"
fi

# === Logrotate config ===
cat > /etc/logrotate.d/pharmacy-monitor <<EOF
$INSTALL_DIR/logs/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0644 $SERVICE_USER $SERVICE_USER
    sharedscripts
}
EOF

# ============================================================================
# 6. nginx + basic auth
# ============================================================================
echo ""
echo "[6/7] Настройка nginx с basic-auth..."

htpasswd -b -c /etc/nginx/.htpasswd-pharmacy "$DASHBOARD_USER" "$DASHBOARD_PASS"

SERVER_NAME="${DOMAIN:-_}"
cat > /etc/nginx/sites-available/pharmacy-monitor <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    auth_basic "Pharmacy Monitor";
    auth_basic_user_file /etc/nginx/.htpasswd-pharmacy;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }
}
EOF
ln -sf /etc/nginx/sites-available/pharmacy-monitor /etc/nginx/sites-enabled/pharmacy-monitor
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ============================================================================
# 7. Let's Encrypt (опционально)
# ============================================================================
if [[ -n "$DOMAIN" && -n "$LETSENCRYPT_EMAIL" ]]; then
    echo ""
    echo "[7/7] Установка Let's Encrypt сертификата..."
    apt-get install -y -qq certbot python3-certbot-nginx
    certbot --nginx -n --agree-tos -m "$LETSENCRYPT_EMAIL" -d "$DOMAIN" --redirect
else
    echo ""
    echo "[7/7] Let's Encrypt пропущен (нужны --domain и --email)."
fi

# ============================================================================
# Итог
# ============================================================================
PUBLIC_IP="$(curl -s -4 ifconfig.me || echo '<server-ip>')"
URL="${DOMAIN:+https://$DOMAIN}"
URL="${URL:-http://$PUBLIC_IP}"

echo ""
echo "================================================================"
echo "  ✅ ГОТОВО"
echo "================================================================"
echo ""
echo "  Дашборд:     $URL"
echo "  Логин:       $DASHBOARD_USER"
echo "  Пароль:      $DASHBOARD_PASS"
echo ""
echo "  ⚠️  ОБЯЗАТЕЛЬНО:"
echo "     1. Отредактируй SMTP-креды:"
echo "        sudo -u $SERVICE_USER nano $INSTALL_DIR/.env"
echo ""
echo "     2. Добавь получателя:"
echo "        cd $INSTALL_DIR && sudo -u $SERVICE_USER /home/$SERVICE_USER/.local/bin/uv run pharmacy-monitor recipient add EMAIL"
echo ""
echo "     3. Тестовый прогон (вручную):"
echo "        sudo systemctl start pharmacy-monitor-run.service"
echo "        sudo journalctl -u pharmacy-monitor-run -f"
echo ""
echo "  Расписание:"
echo "    • Ежедневно в ${RUN_HOUR}:00 UTC — pharmacy-monitor-run.timer"
echo "    • Каждый час — pharmacy-monitor-health.timer (алерт на email)"
echo "    • Ежедневно в 02:00 UTC — pharmacy-monitor-backup.timer (gzip + 90d retention)"
echo "    • Дашборд — pharmacy-monitor-dashboard.service (всегда онлайн)"
if grep -q "^TELEGRAM_BOT_TOKEN=." "$INSTALL_DIR/.env" 2>/dev/null; then
    echo "    • Telegram bot — pharmacy-monitor-telegram.service (всегда онлайн)"
fi
echo ""
echo "  Logrotate: /etc/logrotate.d/pharmacy-monitor (weekly, 8 weeks, gzip)"
echo ""
echo "  Состояние всех сервисов:"
echo "    systemctl list-timers 'pharmacy-monitor-*'"
echo "    systemctl status pharmacy-monitor-dashboard.service"
echo ""
echo "  Восстановление из бэкапа:"
echo "    sudo systemctl stop pharmacy-monitor-dashboard pharmacy-monitor-telegram"
echo "    sudo -u $SERVICE_USER gunzip -c $INSTALL_DIR/data/backups/db-YYYY-MM-DD.sqlite.gz > $INSTALL_DIR/data/db.sqlite"
echo "    sudo systemctl start pharmacy-monitor-dashboard pharmacy-monitor-telegram"
echo ""
