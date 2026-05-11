#!/usr/bin/env bash
#
# Daily Postgres backup. Called by systemd timer pharmacy-monitor-backup.timer.
# Uses `pg_dump` + gzip, stores under /var/backups/pharmacy-monitor/.
# Retention: 14 days local + offsite via rclone (configure separately).
#
# Manual run:
#   sudo -u pm bash infra/scripts/backup.sh

set -euo pipefail

BACKUP_DIR="/var/backups/pharmacy-monitor"
RETENTION_DAYS=14

source /etc/pharmacy-monitor/env  # provides DATABASE_URL

mkdir -p "$BACKUP_DIR"
ts=$(date +%F_%H%M%S)
out="$BACKUP_DIR/pharmacy-monitor-${ts}.sql.gz"

# Parse postgres URL: postgresql+psycopg://user:pass@host:port/db
# pg_dump wants postgresql:// scheme, not postgresql+psycopg://
clean_url=$(echo "$DATABASE_URL" | sed 's/postgresql+psycopg/postgresql/')

echo "==> Backing up to $out"
pg_dump --no-owner --clean --if-exists "$clean_url" | gzip > "$out"

# Retention: prune old backups
find "$BACKUP_DIR" -name 'pharmacy-monitor-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

# Optional offsite backup (uncomment when configured)
# rclone copy "$out" b2:pharmacy-monitor-backups/ --quiet

echo "==> Backup OK ($(du -h "$out" | cut -f1)). Local files: $(ls "$BACKUP_DIR" | wc -l)"
