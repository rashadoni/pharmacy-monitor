#!/usr/bin/env bash
#
# Fetch latest prod Postgres backup to this machine.
#
# User-initiated offsite backup: pulls newest pharmacy-monitor-*.sql.gz.gpg
# from /var/backups/pharmacy-monitor/ on the production server into
# ~/Backups/pharmacy/. Runs from any machine whose SSH key the server accepts.
#
# The server keeps 14 days of dumps on its own disk and nothing pushes them
# anywhere else: a copy outside the server exists only if this script ran.
# Until 2026-10-07 its default target was the Hetzner host deleted in 2026-09,
# so with defaults it could not fetch anything after the move to Contabo
# (2026-09-03).
#
# Recommended cadence: once a week. Run when leaving for vacation /
# after big DB changes / before risky deployments.
#
# Usage:
#   bash infra/local/fetch-backup.sh            # latest backup
#   bash infra/local/fetch-backup.sh --list     # list available backups on prod
#   bash infra/local/fetch-backup.sh <name>     # fetch specific backup
#
# Decryption (recover from backup) needs BACKUP_GPG_PASSPHRASE, the value the
# server encrypts with (/etc/pharmacy-monitor/env). A fetched dump is useless
# without it, so the passphrase must also live somewhere that is not the
# server (on the Mac: Keychain item pharmacy-monitor-gpg).
#   gpg --batch --yes --passphrase-fd 0 -d backup.sql.gz.gpg | gunzip > backup.sql
#   psql -h <host> -U pm pharmacy_monitor < backup.sql
# (gpg waits for the passphrase on stdin: type it and press Enter.)

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PROD_HOST="${PROD_HOST:-13.140.186.143}"
PROD_USER="${PROD_USER:-root}"
# Pinned host key: refuse anything that is not the production server instead
# of trusting whatever answers on the address. ControlPath=none because a
# multiplexed connection (ControlMaster in ~/.ssh/config) skips the check.
KNOWN_HOSTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/prod_known_hosts"
SSH_OPTS=(-i "$SSH_KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes
          -o ControlPath=none)
REMOTE_DIR="/var/backups/pharmacy-monitor"
LOCAL_DIR="${LOCAL_DIR:-$HOME/Backups/pharmacy}"

mkdir -p "$LOCAL_DIR"

case "${1:-}" in
    --list|-l)
        echo "Available backups on prod (newest first):"
        ssh "${SSH_OPTS[@]}" "$PROD_USER@$PROD_HOST" "ls -lht $REMOTE_DIR/pharmacy-monitor-*.sql.gz* 2>/dev/null | awk '{print \$NF, \"(\" \$5 \")\"}'"
        exit 0
        ;;
    "")
        # No arg: fetch latest. -t = sort by mtime descending, head -1 = newest.
        # Encrypted dumps first: while backup.sh is running, the newest file is
        # its unfinished, still-plaintext .sql.gz.
        target=$(ssh "${SSH_OPTS[@]}" "$PROD_USER@$PROD_HOST" "ls -t $REMOTE_DIR/pharmacy-monitor-*.sql.gz.gpg 2>/dev/null | head -1")
        newest=$(ssh "${SSH_OPTS[@]}" "$PROD_USER@$PROD_HOST" "ls -t $REMOTE_DIR/pharmacy-monitor-*.sql.gz* 2>/dev/null | head -1")
        if [[ -z "$target" ]]; then
            target="$newest"
        elif [[ "$newest" != "$target" ]]; then
            # Either the nightly job is running right now, or encryption got
            # switched off on the server and the encrypted dumps are going stale.
            echo "WARN: newest file on prod is unencrypted: $(basename "$newest")" >&2
            echo "      fetching the last encrypted dump instead: $(basename "$target")" >&2
            echo "      if this repeats, check BACKUP_GPG_PASSPHRASE on the server." >&2
        fi
        if [[ -z "$target" ]]; then
            echo "ERROR: no backups found on prod in $REMOTE_DIR" >&2
            exit 1
        fi
        ;;
    *)
        # Explicit filename (a full path from --list is accepted too)
        target="$REMOTE_DIR/$(basename "$1")"
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
# Download under a temporary name: an interrupted copy must not leave a
# truncated file that passes for a finished backup on the next run.
partial="$LOCAL_DIR/.partial-$basename"
trap 'rm -f "$partial"' EXIT
scp "${SSH_OPTS[@]}" "$PROD_USER@$PROD_HOST:$target" "$partial"
mv "$partial" "$local_path"

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
echo "To decrypt (emergency restore) you need BACKUP_GPG_PASSPHRASE kept off the server."
echo "The command waits for it on stdin (type it, press Enter):"
echo "  gpg --batch --yes --passphrase-fd 0 -d \"$local_path\" | gunzip > restore.sql"
