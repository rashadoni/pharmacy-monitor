#!/usr/bin/env bash
#
# Fetch latest prod Postgres backup to this Mac.
#
# User-initiated offsite backup: pulls newest pharmacy-monitor-*.sql.gz.gpg
# from /var/backups/pharmacy-monitor/ on Hetzner into ~/Backups/pharmacy/.
#
# Recommended cadence: once a week. Run when leaving for vacation /
# after big DB changes / before risky deployments.
#
# Usage:
#   bash infra/local/fetch-backup.sh            # latest backup
#   bash infra/local/fetch-backup.sh --list     # list available backups on prod
#   bash infra/local/fetch-backup.sh <name>     # fetch specific backup
#
# Decryption (recover from backup):
#   passphrase=$(security find-generic-password -a pm -s pharmacy-monitor-gpg -w)
#   gpg --batch --yes --passphrase "$passphrase" -d backup.sql.gz.gpg | gunzip > backup.sql
#   psql -h <host> -U pm pharmacy_monitor < backup.sql

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PROD_HOST="${PROD_HOST:-46.225.149.52}"
PROD_USER="${PROD_USER:-root}"
REMOTE_DIR="/var/backups/pharmacy-monitor"
LOCAL_DIR="${LOCAL_DIR:-$HOME/Backups/pharmacy}"

mkdir -p "$LOCAL_DIR"

case "${1:-}" in
    --list|-l)
        echo "Available backups on prod (newest first):"
        ssh -i "$SSH_KEY" "$PROD_USER@$PROD_HOST" "ls -lhS $REMOTE_DIR/pharmacy-monitor-*.sql.gz* 2>/dev/null | awk '{print \$NF, \"(\" \$5 \")\"}'"
        exit 0
        ;;
    "")
        # No arg: fetch latest. -t = sort by mtime descending, head -1 = newest.
        target=$(ssh -i "$SSH_KEY" "$PROD_USER@$PROD_HOST" "ls -t $REMOTE_DIR/pharmacy-monitor-*.sql.gz* 2>/dev/null | head -1")
        if [[ -z "$target" ]]; then
            echo "ERROR: no backups found on prod in $REMOTE_DIR" >&2
            exit 1
        fi
        ;;
    *)
        # Explicit filename
        target="$REMOTE_DIR/$1"
        ;;
esac

basename=$(basename "$target")
local_path="$LOCAL_DIR/$basename"

if [[ -f "$local_path" ]]; then
    echo "Already have $basename locally. Overwrite? [y/N]"
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "Skipped."; exit 0; }
fi

echo "==> Fetching $basename from prod..."
scp -i "$SSH_KEY" "$PROD_USER@$PROD_HOST:$target" "$local_path"

size=$(du -h "$local_path" | cut -f1)
echo "==> Saved → $local_path ($size)"

# Cleanup: keep only last 4 backups locally (1 month at weekly cadence).
keep=4
total=$(ls -t "$LOCAL_DIR"/pharmacy-monitor-*.sql.gz* 2>/dev/null | wc -l | tr -d ' ')
if (( total > keep )); then
    pruned=$(ls -t "$LOCAL_DIR"/pharmacy-monitor-*.sql.gz* | tail -n +$((keep + 1)))
    echo "==> Pruning $(( total - keep )) old local backup(s):"
    echo "$pruned" | xargs -n1 echo "    -"
    echo "$pruned" | xargs rm -f
fi

echo
echo "==> Done. Total local backups: $(ls "$LOCAL_DIR"/pharmacy-monitor-*.sql.gz* 2>/dev/null | wc -l | tr -d ' ')"
echo
echo "To decrypt (emergency restore):"
echo "  passphrase=\$(security find-generic-password -a pm -s pharmacy-monitor-gpg -w)"
echo "  gpg --batch --yes --passphrase \"\$passphrase\" -d \"$local_path\" | gunzip > restore.sql"
