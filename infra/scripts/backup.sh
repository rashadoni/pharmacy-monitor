#!/usr/bin/env bash
#
# Daily Postgres backup → local /var/backups/pharmacy-monitor/
# + optional GPG encryption + Backblaze B2 offsite upload.
#
# Called by systemd timer pharmacy-monitor-backup.timer (04:00 UTC daily).
# Manual run: sudo systemctl start pharmacy-monitor-backup.service
#
# Retention:
#   - local /var/backups: 14 days
#   - B2 offsite:         lifecycle rule on B2 bucket (set when creating)
#
# Env vars (in /etc/pharmacy-monitor/env, read by grep — no shell-source):
#   DATABASE_URL              (required)
#   BACKUP_GPG_PASSPHRASE     (optional, enables GPG encryption)
#   B2_APPLICATION_KEY_ID     (optional, enables offsite upload)
#   B2_APPLICATION_KEY        (optional)
#   B2_BUCKET                 (optional, default "pharmacy-monitor-backups")
#
# History: пре-2026-05-27 версия делала `source /etc/pharmacy-monitor/env`
# под `set -u` и падала из-за `$2: unbound variable` (один из значений
# содержит unbound expansion). Поэтому теперь читаем grep'ом, без exec.

set -eo pipefail
# NOTE: deliberately NOT using `set -u` — env file values are not safe.

BACKUP_DIR="/var/backups/pharmacy-monitor"
RETENTION_DAYS=14
ENV_FILE="/etc/pharmacy-monitor/env"

# Extract single env value safely (no shell-exec of env file).
read_env() {
    local key="$1"
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

DATABASE_URL=$(read_env DATABASE_URL)
if [[ -z "$DATABASE_URL" ]]; then
    echo "ERROR: DATABASE_URL not found in $ENV_FILE" >&2
    exit 1
fi
GPG_PASSPHRASE=$(read_env BACKUP_GPG_PASSPHRASE)
B2_KEY_ID=$(read_env B2_APPLICATION_KEY_ID)
B2_KEY=$(read_env B2_APPLICATION_KEY)
B2_BUCKET=$(read_env B2_BUCKET)
B2_BUCKET="${B2_BUCKET:-pharmacy-monitor-backups}"

mkdir -p "$BACKUP_DIR"
ts=$(date -u +%FT%H%M%SZ)
base="$BACKUP_DIR/pharmacy-monitor-${ts}.sql.gz"

# 1) pg_dump → local gzipped file ─────────────────────────────────────
# SQLAlchemy URL → pg_dump-compatible (strip +psycopg dialect tag).
clean_url=$(echo "$DATABASE_URL" | sed 's/postgresql+psycopg/postgresql/')

echo "==> [1/3] pg_dump → $base"
pg_dump --no-owner --clean --if-exists "$clean_url" | gzip -9 > "$base"
size=$(du -h "$base" | cut -f1)
echo "    OK ($size)"

# 2) GPG encrypt if passphrase set ────────────────────────────────────
final="$base"
if [[ -n "$GPG_PASSPHRASE" ]]; then
    enc="${base}.gpg"
    echo "==> [2/3] GPG encrypt → $enc"
    # systemd unit ProtectHome=true → $HOME inaccessible → GPG can't create
    # default ~/.gnupg. Force tmp homedir (writable, ephemeral).
    export GNUPGHOME="${TMPDIR:-/tmp}/pharm-gpg-$$"
    mkdir -p "$GNUPGHOME"
    chmod 700 "$GNUPGHOME"
    trap 'rm -rf "$GNUPGHOME"' EXIT
    printf '%s' "$GPG_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 --output "$enc" "$base"
    rm -f "$base"  # only keep encrypted local copy
    final="$enc"
    size=$(du -h "$final" | cut -f1)
    echo "    OK ($size)"
else
    echo "==> [2/3] GPG skipped (BACKUP_GPG_PASSPHRASE not set)"
fi

# 3) B2 offsite upload if creds set ───────────────────────────────────
if [[ -n "$B2_KEY_ID" && -n "$B2_KEY" ]]; then
    echo "==> [3/3] B2 upload → b2://$B2_BUCKET/$(basename "$final")"
    if ! command -v b2 &>/dev/null; then
        echo "    WARN: b2 CLI not installed. Run: pip install b2 (or apt install b2)"
    else
        # Auth is cached, but cheap to re-auth each run for idempotency.
        b2 account authorize "$B2_KEY_ID" "$B2_KEY" >/dev/null 2>&1 || {
            echo "    ERROR: b2 authorize failed" >&2
            exit 1
        }
        b2 file upload "$B2_BUCKET" "$final" "$(basename "$final")" >/dev/null
        echo "    OK"
    fi
else
    echo "==> [3/3] B2 upload skipped (B2_APPLICATION_KEY_ID not set)"
fi

# 4) Retention: prune local backups older than N days ────────────────
deleted=$(find "$BACKUP_DIR" -name 'pharmacy-monitor-*.sql.gz*' -mtime "+$RETENTION_DAYS" -type f -delete -print | wc -l)
if (( deleted > 0 )); then
    echo "==> Pruned $deleted local backup(s) older than ${RETENTION_DAYS}d"
fi

total=$(find "$BACKUP_DIR" -name 'pharmacy-monitor-*.sql.gz*' -type f | wc -l)
total_size=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
echo "==> Done. $total local backup(s), $total_size total."
