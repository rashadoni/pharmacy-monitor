#!/usr/bin/env bash
# Pharmacy Monitor — SQLite backup script
#
# Делает атомарный snapshot БД через `sqlite3 .backup`. В отличие от cp —
# безопасен даже если приложение пишет в БД во время бэкапа.
#
# Запускается из cron (см. provision_vps.sh):
#   0 2 * * * /opt/pharmacy-monitor/scripts/backup.sh
#
# Поведение:
#   - Создаёт `data/backups/db-YYYY-MM-DD.sqlite`
#   - Удаляет бэкапы старше RETENTION_DAYS (по умолчанию 90)
#   - Логирует в logs/backup.log
#   - Exit-code 0 = ok, 1 = ошибка

set -euo pipefail

# === Конфигурация ===
INSTALL_DIR="${INSTALL_DIR:-/opt/pharmacy-monitor}"
DB_PATH="${DB_PATH:-$INSTALL_DIR/data/db.sqlite}"
BACKUP_DIR="${BACKUP_DIR:-$INSTALL_DIR/data/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-90}"
LOG_FILE="${LOG_FILE:-$INSTALL_DIR/logs/backup.log}"

# === Helpers ===
log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG_FILE" >&2
}

# === Проверки ===
if [[ ! -f "$DB_PATH" ]]; then
    log "ERROR: DB не найдена: $DB_PATH"
    exit 1
fi

if ! command -v sqlite3 &>/dev/null; then
    log "ERROR: sqlite3 не установлен (apt install sqlite3)"
    exit 1
fi

mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

# === Бэкап ===
DATE="$(date -u '+%Y-%m-%d')"
BACKUP_FILE="$BACKUP_DIR/db-$DATE.sqlite"

# Если за сегодня уже есть бэкап — добавляем час для уникальности (cron может срабатывать чаще)
if [[ -f "$BACKUP_FILE" ]]; then
    BACKUP_FILE="$BACKUP_DIR/db-$DATE-$(date -u '+%H%M').sqlite"
fi

log "Backup: $DB_PATH → $BACKUP_FILE"

# Атомарный backup через sqlite3 .backup (consistent during writes)
if ! sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"; then
    log "ERROR: sqlite3 .backup упал"
    exit 1
fi

# Verify backup
if ! sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" | grep -q "^ok$"; then
    log "ERROR: backup не прошёл integrity_check"
    rm -f "$BACKUP_FILE"
    exit 1
fi

SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
log "OK: backup создан, размер=$SIZE"

# === Compress (gzip — −70% размера для SQLite) ===
gzip -f "$BACKUP_FILE"
log "OK: compressed → ${BACKUP_FILE}.gz"

# === Retention: удаляем бэкапы старше RETENTION_DAYS ===
DELETED=0
while IFS= read -r OLD; do
    rm -f "$OLD"
    DELETED=$((DELETED + 1))
    log "  removed (>${RETENTION_DAYS}d): $(basename "$OLD")"
done < <(find "$BACKUP_DIR" -name "db-*.sqlite.gz" -mtime "+$RETENTION_DAYS" -type f)

# === Финальная статистика ===
TOTAL_FILES="$(find "$BACKUP_DIR" -name "db-*.sqlite.gz" -type f | wc -l | tr -d ' ')"
TOTAL_SIZE="$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)"
log "Done: $TOTAL_FILES backups in $TOTAL_SIZE total. Removed $DELETED older than ${RETENTION_DAYS}d."
