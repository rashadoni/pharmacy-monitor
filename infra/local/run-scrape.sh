#!/bin/bash
#
# Local Mac wrapper для nightly pharmacy-monitor scrape.
#
# Запускается из launchd (см. com.pharmacy-monitor.scrape.plist) или вручную:
#   bash infra/local/run-scrape.sh                    # все три сайта
#   bash infra/local/run-scrape.sh --site pharmonline # только pharmonline
#
# Что делает:
#   1. Открывает SSH-туннель Mac:5433 → prod:5432 (PostgreSQL)
#   2. Тянет DB-пароль из macOS Keychain (`security find-generic-password`)
#   3. Запускает `pharmacy-monitor run --mode category` против тоннеля
#   4. Пишет в БД на проде через тоннель
#   5. Закрывает тоннель, чистит за собой
#
# Логи: ~/Library/Logs/pharmacy-monitor.log
#
# Setup один раз (см. README.md):
#   security add-generic-password -a pm -s pharmacy-monitor-db -w '<pg_password>'
#

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
PROJECT_DIR="${PROJECT_DIR:-/Users/rashadrahimov/pharmacy-monitor}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PROD_HOST="${PROD_HOST:-46.225.149.52}"
PROD_USER="${PROD_USER:-root}"
LOCAL_PG_PORT="${LOCAL_PG_PORT:-5433}"
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-pharmacy-monitor-db}"
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-pm}"
LOG_FILE="${LOG_FILE:-$HOME/Library/Logs/pharmacy-monitor.log}"

# По умолчанию скрейпим pharmonline (aloe и aptekonline крутятся на проде).
# Можно переопределить аргументами из launchd, e.g. `--site pharmonline --site aptekonline`.
DEFAULT_ARGS=(
    "--site" "pharmonline"
    "--site" "aptekonline"
    "--mode" "category"
    "--no-alerts"
)

# ── Bootstrap ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | run-scrape.sh start ====="

cd "$PROJECT_DIR"

# ── Pull DB password — Keychain (secure) или env (для ad-hoc одиночного запуска) ─
if [[ -n "${PG_PASS:-}" ]]; then
    echo "using PG_PASS from environment"
else
    PG_PASS="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
    if [[ -z "$PG_PASS" ]]; then
        echo "ERROR: PG_PASS not provided. Either:"
        echo "  - export PG_PASS='...' (one-shot)"
        echo "  - security add-generic-password -a $KEYCHAIN_ACCOUNT -s $KEYCHAIN_SERVICE -w '...' (persistent)"
        exit 1
    fi
    echo "using PG_PASS from Keychain (service=$KEYCHAIN_SERVICE)"
fi

# ── Open SSH tunnel ─────────────────────────────────────────────────────────
TUNNEL_PID=""
cleanup() {
    if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        echo "tunnel pid=$TUNNEL_PID closed"
    fi
}
trap cleanup EXIT INT TERM

ssh -i "$SSH_KEY" -N -L "$LOCAL_PG_PORT:localhost:5432" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ConnectTimeout=15 \
    "$PROD_USER@$PROD_HOST" &
TUNNEL_PID=$!
echo "tunnel pid=$TUNNEL_PID port=$LOCAL_PG_PORT"

# Дожидаемся пока порт открылся (до 10 секунд)
for i in {1..20}; do
    if nc -z localhost "$LOCAL_PG_PORT" 2>/dev/null; then
        break
    fi
    sleep 0.5
done
if ! nc -z localhost "$LOCAL_PG_PORT" 2>/dev/null; then
    echo "ERROR: tunnel did not come up after 10s"
    exit 1
fi

# ── Run scraper ─────────────────────────────────────────────────────────────
export DATABASE_URL="postgresql+psycopg://pm:${PG_PASS}@localhost:${LOCAL_PG_PORT}/pharmacy_monitor"
# Не expose PG_PASS дальше, env-форвард через SSH туннель только.

ARGS=("$@")
if [[ ${#ARGS[@]} -eq 0 ]]; then
    ARGS=("${DEFAULT_ARGS[@]}")
fi

echo "running: pharmacy-monitor run ${ARGS[*]}"
.venv/bin/pharmacy-monitor run "${ARGS[@]}"
EXIT_CODE=$?

echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | run-scrape.sh end (exit $EXIT_CODE) ====="
exit $EXIT_CODE
