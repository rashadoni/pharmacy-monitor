#!/bin/bash
#
# Pharmacy Monitor — SERVER-side watcher for UI-triggered scrape requests.
#
# Заменяет Mac launchd-вотчер (com.pharmacy-monitor.watch). Кнопка «Запустить
# scrape» в дашборде кладёт ScrapeRequest(pending); этот вотчер опрашивает
# локальный API и исполняет прогон ПРЯМО НА СЕРВЕРЕ — с серверными прокси:
#   aptekonline → Decodo (AZ residential), pharmonline → IPRoyal DDP, aloe → direct.
# Hetzner-IP-бан, из-за которого скрейп раньше гнали только с Mac, обойдён этими
# прокси, поэтому ноут больше не нужен.
#
# Архитектура (важные отличия от Mac-версии):
#   1. Без SSH-туннеля и Keychain — БД локальная, креды из EnvironmentFile.
#   2. Скрейп выполняется СИНХРОННО внутри systemd-сервиса (НЕ в detached
#      субшелле). Пока скрейп идёт, .service остаётся active, поэтому таймер
#      (.timer, OnUnitInactiveSec) НЕ фаerr'ит повторно → нет двойного запуска.
#      И systemd не убивает скрейп как «осиротевший» процесс при выходе вотчера
#      (тот cgroup-kill баг, что ловили на Mac 2026-06-11 без AbandonProcessGroup).
#   3. --request-id N: `pharmacy-monitor run` сам пометит ScrapeRequest 'ok'
#      сразу после persist. Вотчер дёргает /scrape-complete только на ошибку.
#
# Запускается из pharmacy-monitor-scrape-watcher.timer (каждые ~60с в простое).

# NB: НЕ `set -e` — упавший curl/scrape не должен оборвать пометку статуса.
set -uo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8080}"
API_KEY="${PHARMACY_API_KEY:-}"            # X-API-Key, из EnvironmentFile
PROJECT_DIR="${PROJECT_DIR:-/opt/pharmacy-monitor}"
VENV_BIN="${VENV_BIN:-$PROJECT_DIR/.venv/bin}"
SCRAPE_MAX_HOURS="${SCRAPE_MAX_HOURS:-6}"  # дольше = прогон/запрос считаем зависшим

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | $*"; }

log "watch-scrape-queue tick (server)"

if [[ -z "$API_KEY" ]]; then
    log "ERROR: PHARMACY_API_KEY not set — cannot poll queue"
    exit 1
fi
cd "$PROJECT_DIR" || { log "ERROR: cannot cd $PROJECT_DIR"; exit 1; }

# === Reap зависших 'running' запросов =======================================
# При синхронной модели запрос остаётся 'running' только если вотчер убили
# в середине скрейпа (reboot / TimeoutStartSec). Если активного
# `pharmacy-monitor run` нет, а запрос висит 'running' > SCRAPE_MAX_HOURS —
# метим failed, чтобы UI-антиспам (409) разблокировался. Best-effort, не фатально.
if ! pgrep -f "pharmacy-monitor run" >/dev/null 2>&1; then
    "$VENV_BIN/python" - "$SCRAPE_MAX_HOURS" <<'PY' 2>/dev/null || true
import sys, datetime
from sqlalchemy import select
from src import storage

storage.init_db()
Session = storage.make_session()
cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=float(sys.argv[1]))
with Session() as s:
    stale = s.scalars(
        select(storage.ScrapeRequest).where(
            storage.ScrapeRequest.status == "running",
            storage.ScrapeRequest.started_at < cutoff,
        )
    ).all()
    for r in stale:
        r.status = "failed"
        r.completed_at = datetime.datetime.utcnow()
        r.error_message = (
            (r.error_message or "") + " | reaped: stuck running (watcher killed mid-scrape?)"
        ).strip(" |")
    if stale:
        s.commit()
        print(f"reaped {len(stale)} stale running request(s)")
PY
fi

# === Pgrep guard ============================================================
# Не опрашиваем очередь, пока активен ЛЮБОЙ `pharmacy-monitor run` (плановый
# таймер, intraday-тик или предыдущий button-прогон): параллельный persist даёт
# конфликты по unique (site, external_id) + дубли Match'ей. Guard ПЕРЕД опросом,
# иначе pending-scrape пометит запрос 'running' на GET, а мы его не запустим.
# Следующий тик (через ~60с) повторит.
if pgrep -f "pharmacy-monitor run" >/dev/null 2>&1; then
    log "skip: a pharmacy-monitor run is already active"
    exit 0
fi

# === Опрос pending-запроса ==================================================
resp="$(curl -s -m 10 -H "X-API-Key: $API_KEY" "$API_BASE/api/v1/internal/pending-scrape" || true)"
if [[ -z "$resp" ]]; then
    log "no response from API ($API_BASE)"
    exit 0
fi

# Парсим {pending:{id,mode,category_id,sites}} через venv-python (jq может не быть).
parsed="$("$VENV_BIN/python" - "$resp" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("ERROR"); sys.exit()
p = d.get("pending")
if not p:
    print("NONE"); sys.exit()
sites = ",".join(p.get("sites") or [])
print(f"{p['id']}|{p.get('mode') or 'all'}|{p.get('category_id') or ''}|{sites}")
PY
)"

if [[ -z "$parsed" || "$parsed" == "NONE" ]]; then
    log "no pending requests"
    exit 0
fi
if [[ "$parsed" == "ERROR" ]]; then
    log "parse error on pending-scrape response: $resp"
    exit 0
fi

IFS='|' read -r req_id mode category_id sites <<< "$parsed"
log "picked up request #$req_id mode=$mode category_id=$category_id sites=$sites"

# === Сборка CLI-аргументов ==================================================
# --request-id → run сам пометит ScrapeRequest 'ok' сразу после persist.
# --no-alerts → ручной refresh не должен спамить alert-письмами (как было на Mac).
# Scope: --site на каждый запрошенный сайт (нет сайтов → все сайты),
#        --category-id для category-режима.
ARGS=("run" "--mode" "category" "--no-alerts" "--request-id" "$req_id")
IFS=',' read -ra SITE_ARR <<< "$sites"
for st in "${SITE_ARR[@]}"; do
    [[ -n "$st" ]] && ARGS+=("--site" "$st")
done
[[ -n "$category_id" ]] && ARGS+=("--category-id" "$category_id")

log "running: pharmacy-monitor ${ARGS[*]}"

# Синхронный прогон — сервис остаётся active на всё время скрейпа, таймер не
# дублирует тик. На успех run сам метит 'ok' (--request-id); на non-zero exit
# метим failed (эндпоинт идемпотентен — не перетрёт уже выставленный 'ok').
if "$VENV_BIN/pharmacy-monitor" "${ARGS[@]}"; then
    log "request #$req_id finished cleanly"
else
    rc=$?
    log "request #$req_id FAILED (exit $rc) — marking via API"
    curl -s -m 10 -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
        -d '{"error_message":"server watcher: pharmacy-monitor run exit non-zero"}' \
        "$API_BASE/api/v1/internal/scrape-complete/$req_id" >/dev/null 2>&1 || true
fi
