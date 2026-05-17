#!/bin/bash
#
# Cutover script — переключить pharmonline + aptekonline scraping с
# Mac launchd обратно на prod systemd timers через ScraperAPI Hobby tier
# (residential pool, $49/мес).
#
# КОГДА ЗАПУСКАТЬ: ТОЛЬКО ПОСЛЕ upgrade'а ScraperAPI до Hobby plan на
# https://dashboard.scraperapi.com/billing — нужны creditsLeft > 0.
#
# КАК: на проде как root:
#   ssh -i ~/.ssh/id_ed25519 root@46.225.149.52
#   cd /opt/pharmacy-monitor
#   bash infra/scripts/cutover-to-prod-scrapers.sh
#
# Что делает:
#   1. Проверяет ScraperAPI credits > 0 (бэйлится если нет)
#   2. Manual smoke scrape pharmonline (5 минут, ожидать ~9-10K продуктов)
#   3. Если smoke OK → enable timer pharmonline
#   4. Аналогично aptekonline (smoke + enable timer)
#   5. Финально печатает чек-лист «после cutover'а сделай на Mac:
#      launchctl unload ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist»
#
# Idempotent: безопасно перезапустить — пропустит уже включённые шаги.

set -euo pipefail

PROD_ENV_FILE="${PROD_ENV_FILE:-/etc/pharmacy-monitor/env}"
MIN_CREDITS="${MIN_CREDITS:-5000}"  # safety: не запускать если credits < 5K
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-600}"  # 10 минут на smoke per site

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }

# ── Step 0: sanity ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "Должен запускаться как root (нужен systemctl)"
[[ -f $PROD_ENV_FILE ]] || fail "Не нашёл $PROD_ENV_FILE"

# shellcheck source=/dev/null
source "$PROD_ENV_FILE"
[[ -n "${SCRAPER_API_KEY:-}" ]] || fail "SCRAPER_API_KEY не выставлен в $PROD_ENV_FILE"

# ── Step 1: ScraperAPI credits check ────────────────────────────────────────
log "Проверяю ScraperAPI credits…"
ACCOUNT_JSON=$(curl -s "http://api.scraperapi.com/account?api_key=$SCRAPER_API_KEY")
CREDITS_LEFT=$(echo "$ACCOUNT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('creditsLeft', 0))")
REQUEST_LIMIT=$(echo "$ACCOUNT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('requestLimit', 0))")

log "  creditsLeft = $CREDITS_LEFT (план: $REQUEST_LIMIT)"

if [[ "$CREDITS_LEFT" -lt "$MIN_CREDITS" ]]; then
    fail "credits < $MIN_CREDITS — upgrade на Hobby plan не сделан (или истощён). \
\nUpgrade: https://dashboard.scraperapi.com/billing"
fi
ok "Credits достаточно ($CREDITS_LEFT)"

# Hobby plan имеет 100K credits/мес. Free trial — 5K. Если < 50K — warn.
if [[ "$REQUEST_LIMIT" -lt 50000 ]]; then
    warn "requestLimit = $REQUEST_LIMIT — выглядит как Free план, не Hobby. \
\nЕсли это намеренно, продолжай. Иначе проверь plan."
    read -rp "Продолжить? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ── Step 2: Helper — smoke + enable single site ─────────────────────────────
smoke_and_enable_site() {
    local site="$1"
    local timer="pharmacy-monitor-scrape@${site}.timer"
    local service="pharmacy-monitor-scrape@${site}.service"

    log "─── ${site} ───"

    # Already enabled? Skip smoke, just confirm.
    if systemctl is-enabled "$timer" 2>/dev/null | grep -q enabled; then
        warn "$timer уже enabled — пропускаю smoke, проверяю последний run"
        if systemctl is-active "$timer"; then
            ok "$timer активен"
        fi
        return 0
    fi

    log "Запускаю smoke scrape ${site} (max ${SMOKE_TIMEOUT}с)…"
    set +e
    timeout "$SMOKE_TIMEOUT" systemctl start "$service"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        warn "Smoke service exit code $rc (может быть timeout — это OK если он ещё работает в фоне)"
    fi

    # Подождать до 30 секунд пока run появится в БД
    log "Жду run появления в БД…"
    sleep 5
    LAST_RUN=$(sudo -u postgres psql pharmacy_monitor -tA -c \
        "SELECT id, status, products_scraped FROM runs WHERE sites_completed LIKE '%${site}%' ORDER BY started_at DESC LIMIT 1;" 2>/dev/null || true)

    if [[ -z "$LAST_RUN" ]]; then
        fail "Не нашёл свежий run для $site в БД. Проверь journalctl -u $service -n 50"
    fi

    log "  Last run: $LAST_RUN"

    # Парс status + products
    STATUS=$(echo "$LAST_RUN" | awk -F'|' '{print $2}')
    PRODUCTS=$(echo "$LAST_RUN" | awk -F'|' '{print $3}')

    if [[ "$STATUS" != "ok" ]]; then
        warn "Run status = $STATUS (ожидали ok). Может быть в процессе — проверяй вручную."
        fail "$site smoke не прошёл, не включаю timer. Логи: journalctl -u $service -n 100"
    fi

    if [[ "$PRODUCTS" -lt 1000 ]]; then
        warn "Run отдал только $PRODUCTS продуктов (ожидали ~9-10K). Возможно scraper ban или Recaptcha."
        read -rp "Всё равно включить timer для $site? [y/N] " ans
        [[ "$ans" =~ ^[Yy]$ ]] || return 1
    else
        ok "Smoke OK: $PRODUCTS продуктов"
    fi

    log "Enable + start timer $timer…"
    systemctl enable --now "$timer"
    ok "$timer активен; следующий старт: $(systemctl list-timers "$timer" --no-pager | sed -n '2p' | awk '{print $1, $2}')"
}

# ── Step 3: cutover pharmonline ─────────────────────────────────────────────
smoke_and_enable_site "pharmonline"

# ── Step 4: cutover aptekonline ─────────────────────────────────────────────
smoke_and_enable_site "aptekonline"

# ── Step 5: Final checklist ─────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
ok "Cutover на проде завершён"
echo ""
log "Активные timer'ы:"
systemctl list-timers --all | grep pharmacy-monitor-scrape || true
echo ""
warn "ВРУЧНУЮ на Mac (выключить launchd чтобы не дублировать scrape):"
echo "    launchctl unload ~/Library/LaunchAgents/com.pharmacy-monitor.scrape.plist"
echo ""
warn "ТАКЖЕ проверь через 24-30 часов:"
echo "    https://leaddrive.cloud/overview → 'Свежесть scrape' для всех 3 сайтов д.б. зелёная"
echo ""
warn "Обновить CLAUDE.md «Runtime layout» — все 3 сайта теперь на проде."
