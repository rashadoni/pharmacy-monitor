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

# С 2026-05-27: pharmonline переехал на прод (DDP scraper через IPRoyal —
# работает прямо с Hetzner IP, не требует Mac-IP). Mac launchd теперь скрейпит
# только aptekonline (BD Web Unlocker сейчас даёт 403 на Hetzner для JSON API
# когда rate-limit'ы тяжёлые — Mac residential gateway остаётся надёжней пока
# мы не доделаем IPRoyal для aptekonline тоже).
#
# Aloe скрейпится прод-таймером (systemd) — direct.
#
# Mac launchd теперь:
#   - aptekonline (через тоннель в прод-DB)
#   - DR-fallback для pharmonline если прод DDP path упадёт (manual override:
#     `bash run-scrape.sh --site pharmonline --site aptekonline`)
DEFAULT_ARGS=(
    "--site" "aptekonline"
    "--mode" "category"
    "--no-alerts"
)

# ── Bootstrap ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | run-scrape.sh start ====="

cd "$PROJECT_DIR"

# ── Keep Mac awake пока скрейп работает ────────────────────────────────────
# launchd's StartCalendarInterval=18:00 не разбудит Mac если он в sleep, но
# проблема большая в том, что Mac уходит в idle sleep ПОКА скрейп работает
# (наш прогон 30-60 мин). caffeinate -imsu блокирует idle/disk/system sleep,
# -w $$ привязывает к жизни этого процесса (auto-exit при завершении).
if [[ -x /usr/bin/caffeinate ]]; then
    /usr/bin/caffeinate -imsu -w $$ &
    CAFFEINATE_PID=$!
    echo "caffeinate pid=$CAFFEINATE_PID — Mac будет бодрствовать до конца скрейпа"
else
    echo "WARN: caffeinate not found, Mac может уйти в sleep"
fi

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

# Retry: до 3 попыток с экспоненциальной задержкой (network blip / интернет
# поднимается медленнее launchd timer / SSH banner exchange timeout).
TUNNEL_ATTEMPTS=3
TUNNEL_OK=0
for attempt in $(seq 1 $TUNNEL_ATTEMPTS); do
    # Если предыдущая попытка оставила хвост — убить
    if [[ -n "${TUNNEL_PID:-}" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        sleep 1
    fi

    echo "tunnel attempt $attempt/$TUNNEL_ATTEMPTS …"
    ssh -i "$SSH_KEY" -N -L "$LOCAL_PG_PORT:localhost:5432" \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=20 \
        -o TCPKeepAlive=yes \
        -o ConnectTimeout=15 \
        -o StrictHostKeyChecking=accept-new \
        "$PROD_USER@$PROD_HOST" &
    TUNNEL_PID=$!
    echo "  tunnel pid=$TUNNEL_PID port=$LOCAL_PG_PORT"

    # Ждём подъёма порта до 15с
    for i in {1..30}; do
        if nc -z localhost "$LOCAL_PG_PORT" 2>/dev/null; then
            TUNNEL_OK=1
            break
        fi
        # Если SSH-процесс упал — нет смысла ждать
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            echo "  SSH process died early"
            break
        fi
        sleep 0.5
    done

    if [[ $TUNNEL_OK -eq 1 ]]; then
        echo "  tunnel up on attempt $attempt"
        break
    fi

    echo "  attempt $attempt failed; ssh exit pending"
    if [[ $attempt -lt $TUNNEL_ATTEMPTS ]]; then
        # exponential backoff: 5s → 15s → (no third)
        sleep $((5 * attempt))
    fi
done

if [[ $TUNNEL_OK -ne 1 ]]; then
    echo "ERROR: SSH tunnel не поднялся за $TUNNEL_ATTEMPTS попытки"
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

# ── Validate aptekonline links (фантомные товары: страница 404) ──────────────
# aptekonline JSON API листит товары без живой страницы (в каталоге, но 404).
# Они скрейпятся/matched (age=0), но ссылка мёртвая. HTTP-чек URL с Baku-IP
# (прямой, без прокси — Hetzner-IP забанен) помечает 404 (Product.url_dead_at);
# comparison их скрывает. Non-fatal: проблема валидации не валит скрейп.
if [[ "${ARGS[*]}" == *aptekonline* ]]; then
    echo "validating aptekonline links…"
    .venv/bin/pharmacy-monitor validate-links --site aptekonline \
        || echo "WARN: validate-links failed (non-fatal)"
fi

echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | run-scrape.sh end (exit $EXIT_CODE) ====="
exit $EXIT_CODE
