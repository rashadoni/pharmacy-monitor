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
# Runtime/test overrides:
#   BACKUP_DIR, RETENTION_DAYS, ENV_FILE, BACKUP_MIN_BYTES, BACKUP_TIMESTAMP
#
# History: пре-2026-05-27 версия делала `source /etc/pharmacy-monitor/env`
# под `set -u` и падала из-за `$2: unbound variable` (один из значений
# содержит unbound expansion). Поэтому теперь читаем grep'ом, без exec.

set -eo pipefail
# NOTE: deliberately NOT using `set -u` — env file values are not safe.
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/pharmacy-monitor}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
ENV_FILE="${ENV_FILE:-/etc/pharmacy-monitor/env}"
BACKUP_MIN_BYTES="${BACKUP_MIN_BYTES:-1024}"

tmp_base=""
tmp_enc=""
gpg_home_created=""

cleanup() {
    [[ -z "$tmp_base" ]] || rm -f -- "$tmp_base"
    [[ -z "$tmp_enc" ]] || rm -f -- "$tmp_enc"

    # Only remove the private temp homedir created by this process.
    if [[ -n "$gpg_home_created" && "$gpg_home_created" == "${TMPDIR:-/tmp}"/pharm-gpg-* ]]; then
        rm -rf -- "$gpg_home_created"
    fi
}
trap cleanup EXIT

verify_gzip_archive() {
    local archive="$1"
    local bytes

    if [[ ! -s "$archive" ]]; then
        echo "ERROR: backup archive is empty: $archive" >&2
        return 1
    fi

    bytes=$(wc -c < "$archive" | tr -d ' ')
    if (( bytes < BACKUP_MIN_BYTES )); then
        echo "ERROR: backup archive is suspiciously small: ${bytes}B < ${BACKUP_MIN_BYTES}B" >&2
        return 1
    fi

    gzip -t -- "$archive"
}

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

# A half-configured offsite target is a configuration error, not "disabled".
if [[ -n "$B2_KEY_ID" && -z "$B2_KEY" ]]; then
    echo "ERROR: B2_APPLICATION_KEY_ID is set but B2_APPLICATION_KEY is missing" >&2
    exit 1
fi
if [[ -z "$B2_KEY_ID" && -n "$B2_KEY" ]]; then
    echo "ERROR: B2_APPLICATION_KEY is set but B2_APPLICATION_KEY_ID is missing" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
ts="${BACKUP_TIMESTAMP:-$(date -u +%FT%H%M%SZ)}"
base="$BACKUP_DIR/pharmacy-monitor-${ts}.sql.gz"
tmp_base="$BACKUP_DIR/.pharmacy-monitor-${ts}.sql.gz.tmp"
tmp_enc="$BACKUP_DIR/.pharmacy-monitor-${ts}.sql.gz.gpg.tmp"

# 1) pg_dump → local gzipped file ─────────────────────────────────────
# SQLAlchemy URL → pg_dump-compatible (strip +psycopg dialect tag).
clean_url="${DATABASE_URL/postgresql+psycopg/postgresql}"

echo "==> [1/3] pg_dump → $base"
pg_dump --no-owner --clean --if-exists "$clean_url" | gzip -9 > "$tmp_base"
verify_gzip_archive "$tmp_base"
echo "    Dump verified"

# 2) GPG encrypt if passphrase set ────────────────────────────────────
final=""
if [[ -n "$GPG_PASSPHRASE" ]]; then
    enc="${base}.gpg"
    echo "==> [2/3] GPG encrypt → $enc"
    # systemd unit ProtectHome=true → $HOME inaccessible → GPG can't create
    # default ~/.gnupg. Force tmp homedir (writable, ephemeral).
    export GNUPGHOME="${TMPDIR:-/tmp}/pharm-gpg-$$"
    gpg_home_created="$GNUPGHOME"
    mkdir -p "$GNUPGHOME"
    chmod 700 "$GNUPGHOME"
    printf '%s' "$GPG_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 --output "$tmp_enc" "$tmp_base"

    if [[ ! -s "$tmp_enc" ]]; then
        echo "ERROR: encrypted backup is empty" >&2
        exit 1
    fi

    # Prove that the passphrase decrypts to a valid gzip stream before publish.
    printf '%s' "$GPG_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        --decrypt "$tmp_enc" | gzip -t

    mv -f -- "$tmp_enc" "$enc"
    rm -f -- "$tmp_base"  # only keep encrypted local copy
    tmp_enc=""
    tmp_base=""
    final="$enc"
    echo "    Encryption round-trip verified"
else
    echo "==> [2/3] GPG skipped (BACKUP_GPG_PASSPHRASE not set)"
    mv -f -- "$tmp_base" "$base"
    tmp_base=""
    final="$base"
fi

size=$(du -h "$final" | cut -f1)
echo "    Published atomically ($size)"

# 3) B2 offsite upload if creds set ───────────────────────────────────
if [[ -n "$B2_KEY_ID" && -n "$B2_KEY" ]]; then
    echo "==> [3/3] B2 upload → b2://$B2_BUCKET/$(basename "$final")"
    if ! command -v b2 &>/dev/null; then
        echo "    ERROR: B2 credentials are configured but b2 CLI is not installed" >&2
        exit 1
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
