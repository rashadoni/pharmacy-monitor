#!/bin/bash
#
# Pharmacy Monitor — Mac launchd watcher для UI-triggered scrape requests.
#
# Каждые 60 секунд (тикает launchd) опрашивает прод API:
#   GET /api/v1/internal/pending-scrape
#
# Архитектура (refactored 2026-05-11):
#   1. Pgrep guard: если другой `pharmacy-monitor run` уже работает (queue
#      или daily cron), watcher выходит сразу — избегаем DB-race.
#   2. Detached spawn: pharmacy-monitor запускается в отдельной подоболочке
#      (subshell + `&` + `disown`), watcher сам выходит за секунды. Launchd
#      ticks больше НЕ блокируются на 60+ мин matcher-фазах.
#   3. --request-id N: pharmacy-monitor пометит ScrapeRequest как 'ok' сам,
#      сразу после persist (не дожидаясь matcher/analyzer). Watcher здесь
#      больше НЕ дёргает /scrape-complete на успех — только на ранние ошибки.
#   4. Subshell держит SSH-tunnel живым до завершения pharmacy-monitor и
#      сам же его убирает.
#
# Запуск через launchd:
#   cp infra/local/com.pharmacy-monitor.watch.plist ~/Library/LaunchAgents/
#   launchctl load -w ~/Library/LaunchAgents/com.pharmacy-monitor.watch.plist

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/rashadrahimov/pharmacy-monitor}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PROD_HOST="${PROD_HOST:-46.225.149.52}"
LOCAL_PG_PORT="${LOCAL_PG_PORT:-5433}"
API_BASE="${API_BASE:-https://leaddrive.cloud}"
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-pharmacy-monitor-db}"
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-pm}"
API_KEY="${API_KEY:-}"  # X-API-Key
LOG_FILE="${LOG_FILE:-$HOME/Library/Logs/pharmacy-monitor-watch.log}"

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | watch-scrape-queue tick ====="

cd "$PROJECT_DIR"

# === Pgrep guard ============================================================
# Если уже бежит ANY `pharmacy-monitor run` (queue, daily cron, или ручной
# запуск) — откладываем тик. Запускать второй scrape параллельно нельзя:
# конкурентные UPSERT'ы в `products`/`price_snapshots` пораждают конфликты
# на уникальном (site, external_id), а matcher одновременно с persist
# создаёт дубликаты Match'ей. Launchd попробует ещё раз через 60 секунд.
if pgrep -f "pharmacy-monitor run" >/dev/null 2>&1; then
    echo "  pharmacy-monitor run already in progress, deferring tick"
    exit 0
fi

# Pull API_KEY from Keychain if not set in env
if [[ -z "$API_KEY" ]]; then
    API_KEY="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s pharmacy-monitor-api-key -w 2>/dev/null || true)"
fi
if [[ -z "$API_KEY" ]]; then
    echo "ERROR: API_KEY not set (env or Keychain 'pharmacy-monitor-api-key')"
    exit 1
fi

# === Poll for pending request ===============================================
# require_api_key() в src/api.py принимает header X-API-Key (FastAPI alias из
# параметра `x_api_key`).
resp=$(curl -s -m 10 -w "\n__HTTP_STATUS:%{http_code}" \
    -H "X-API-Key: $API_KEY" \
    "$API_BASE/api/v1/internal/pending-scrape")
http_code=$(echo "$resp" | sed -n 's/^__HTTP_STATUS://p' | tail -1)
body=$(echo "$resp" | sed '/^__HTTP_STATUS:/d')

if [[ -z "$body" ]]; then
    echo "  no response from API (timeout?)"
    exit 0
fi
if [[ "$http_code" != "200" ]]; then
    echo "  API error HTTP $http_code: $body"
    exit 1
fi
resp="$body"

# Parse JSON via python (always available on macOS)
parsed=$(echo "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    p = d.get("pending")
    if not p:
        print("NONE")
    else:
        req_id = p["id"]
        mode = p["mode"]
        cat_id = p.get("category_id") or ""
        sites_list = p.get("sites") or ["pharmonline", "aptekonline"]
        sites = ",".join(sites_list)
        print(f"{req_id}|{mode}|{cat_id}|{sites}")
except Exception as e:
    print(f"ERROR|{e}")
')

if [[ "$parsed" == "NONE" ]]; then
    echo "  no pending requests"
    exit 0
fi
if [[ "$parsed" == ERROR* ]]; then
    echo "  parse error: $parsed"
    exit 1
fi

IFS='|' read -r req_id mode category_id sites <<< "$parsed"
echo "  picked up request #$req_id mode=$mode category_id=$category_id sites=$sites"

# Pull PG_PASS from Keychain (same flow как run-scrape.sh)
PG_PASS="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$PG_PASS" ]]; then
    echo "  ERROR: PG_PASS not in Keychain"
    curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
        -d '{"error_message":"Mac watcher: PG_PASS not found in Keychain"}' \
        "$API_BASE/api/v1/internal/scrape-complete/$req_id"
    exit 1
fi

# === Build CLI args ==========================================================
# --request-id заставит pharmacy-monitor пометить ScrapeRequest как 'ok' сразу
# после persist phase (через ~30 мин), не дожидаясь matcher/analyzer (которые
# работают ещё ~30-60 мин в фоне). UI получает «Готово — N товаров» в три раза
# быстрее, чем при подходе «watcher ждёт всё».
ARGS=("run" "--mode" "category" "--no-alerts" "--request-id" "$req_id")
IFS=',' read -ra SITE_ARR <<< "$sites"
for s in "${SITE_ARR[@]}"; do
    [[ -n "$s" ]] && ARGS+=("--site" "$s")
done
[[ -n "$category_id" ]] && ARGS+=("--category-id" "$category_id")

echo "  spawning detached: pharmacy-monitor ${ARGS[*]}"

# === Detached subshell ======================================================
# Subshell:
#   - открывает SSH-tunnel (со своим trap-cleanup)
#   - запускает pharmacy-monitor синхронно
#   - на не-zero exit вызывает /scrape-complete с error_message
#     (на success ничего не нужно — pharmacy-monitor сам пометит ok через
#     --request-id сразу после persist)
#   - убивает tunnel
#
# Snake `setsid` нет на macOS, используем nohup-стиль через `&` + `disown`
# + редирект stdin/out/err. После `disown` watcher теряет связь с subshell,
# subshell живёт независимо до своего собственного выхода.
(
    cd "$PROJECT_DIR"
    ssh -i "$SSH_KEY" -N -L "$LOCAL_PG_PORT:localhost:5432" \
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ConnectTimeout=15 \
        "root@$PROD_HOST" &
    TUNNEL_PID=$!
    trap 'kill $TUNNEL_PID 2>/dev/null || true' EXIT
    sleep 3

    export DATABASE_URL="postgresql+psycopg://pm:${PG_PASS}@localhost:${LOCAL_PG_PORT}/pharmacy_monitor"

    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | bg-run for request #$req_id starting"
    if .venv/bin/pharmacy-monitor "${ARGS[@]}" 2>&1; then
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | bg-run for request #$req_id finished cleanly"
    else
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | bg-run for request #$req_id FAILED, marking via API"
        # Idempotent — endpoint оставит 'ok' если --request-id уже пометил,
        # иначе пометит 'failed'.
        curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
            -d '{"error_message":"pharmacy-monitor run exit non-zero"}' \
            "$API_BASE/api/v1/internal/scrape-complete/$req_id"
    fi
) </dev/null >>"$LOG_FILE" 2>&1 &
BG_PID=$!
disown $BG_PID

echo "  spawned bg-subshell PID=$BG_PID for request #$req_id, watcher exiting"
