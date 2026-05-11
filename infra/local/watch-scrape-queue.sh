#!/bin/bash
#
# Pharmacy Monitor — Mac launchd watcher для UI-triggered scrape requests.
#
# Каждые 60 секунд (тикает launchd) опрашивает прод API:
#   GET /api/v1/internal/pending-scrape
# Если есть pending — исполняет `pharmacy-monitor run` с указанными параметрами,
# затем PATCH'ит запрос как 'ok' или 'failed'.
#
# Использует тот же SSH-tunnel к prod Postgres что и run-scrape.sh.
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
API_KEY="${API_KEY:-}"  # X-Pharmacy-API-Key
LOG_FILE="${LOG_FILE:-$HOME/Library/Logs/pharmacy-monitor-watch.log}"

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') | watch-scrape-queue tick ====="

cd "$PROJECT_DIR"

# Pull API_KEY from Keychain if not set in env
if [[ -z "$API_KEY" ]]; then
    API_KEY="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s pharmacy-monitor-api-key -w 2>/dev/null || true)"
fi
if [[ -z "$API_KEY" ]]; then
    echo "ERROR: API_KEY not set (env or Keychain 'pharmacy-monitor-api-key')"
    exit 1
fi

# Poll for pending request.
# require_api_key() в src/api.py принимает header X-API-Key (FastAPI alias из
# параметра `x_api_key`). Не путать с `X-Pharmacy-API-Key`.
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

# Open SSH tunnel
ssh -i "$SSH_KEY" -N -L "$LOCAL_PG_PORT:localhost:5432" \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ConnectTimeout=15 \
    "root@$PROD_HOST" &
TUNNEL_PID=$!
cleanup() { kill $TUNNEL_PID 2>/dev/null || true; }
trap cleanup EXIT INT TERM
sleep 3

export DATABASE_URL="postgresql+psycopg://pm:${PG_PASS}@localhost:${LOCAL_PG_PORT}/pharmacy_monitor"

# Build CLI args
# --request-id заставит pharmacy-monitor пометить ScrapeRequest как 'ok' сразу
# после persist phase (через несколько минут), не дожидаясь matcher/analyzer —
# UI получит «Готово — N товаров» как только scrape физически завершился.
ARGS=("run" "--mode" "category" "--no-alerts" "--request-id" "$req_id")
# Sites
IFS=',' read -ra SITE_ARR <<< "$sites"
for s in "${SITE_ARR[@]}"; do
    [[ -n "$s" ]] && ARGS+=("--site" "$s")
done
# Category-id если есть
[[ -n "$category_id" ]] && ARGS+=("--category-id" "$category_id")

echo "  running: pharmacy-monitor ${ARGS[*]}"
run_id=""
error_msg=""
if .venv/bin/pharmacy-monitor "${ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"; then
    # Try to extract run_id из последней SQL queries
    run_id=$(.venv/bin/python -c "
from src.storage import make_session, Run
from sqlalchemy import select, desc
S = make_session()
with S() as s:
    r = s.scalar(select(Run).order_by(desc(Run.id)).limit(1))
    print(r.id if r else '')
" 2>/dev/null || true)
    echo "  done, run_id=$run_id"
else
    error_msg="pharmacy-monitor run exit non-zero"
    echo "  FAILED: $error_msg"
fi

# Notify backend
payload="{}"
if [[ -n "$run_id" ]]; then
    payload="{\"run_id\":$run_id}"
fi
if [[ -n "$error_msg" ]]; then
    payload="{\"error_message\":\"$error_msg\"}"
fi
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
    -d "$payload" "$API_BASE/api/v1/internal/scrape-complete/$req_id"

echo "  ===== request #$req_id complete ====="
