#!/usr/bin/env bash
#
# Cleanup stale Caddy config backups in /etc/caddy/.
#
# Каждый раз когда мы редактируем /etc/caddy/Caddyfile через `sed` / patch,
# мы оставляем `.bak.YYYYMMDD-HHMM` копию для отката. Без чистки они копятся
# годами (например Caddyfile.bak.20260506-2038 после HTTPS migration лежит
# в проде до сих пор). Этот скрипт держит последние N (по mtime), удаляет
# старее.
#
# Phase 0.4 (2026-05-26). Запускать вручную или из cron @weekly.
#
# Usage:
#   bash cleanup_caddy_backups.sh            # dry-run, prints what would delete
#   bash cleanup_caddy_backups.sh --apply    # actually delete
#   KEEP=5 bash cleanup_caddy_backups.sh --apply  # override "keep last N"
#
# Cron suggestion (root crontab on prod):
#   0 3 * * 0 /opt/pharmacy-monitor/infra/scripts/cleanup_caddy_backups.sh --apply >> /var/log/caddy-cleanup.log 2>&1

set -euo pipefail

CADDY_DIR="${CADDY_DIR:-/etc/caddy}"
KEEP="${KEEP:-3}"
APPLY=0

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ ! -d "$CADDY_DIR" ]]; then
    echo "ERROR: $CADDY_DIR not found" >&2
    exit 1
fi

# Find .bak.* files. Sort by filename (newest LAST because timestamps are
# embedded in names like Caddyfile.bak.20260506-2038 — alphabetical sort
# = chronological).
#
# Portable: works on macOS bash 3.2 (no `mapfile`) and BSD find (no `-printf`).
backups=()
while IFS= read -r line; do
    backups+=("$line")
done < <(find "$CADDY_DIR" -maxdepth 1 -type f \
    \( -name '*.bak.*' -o -name 'Caddyfile.bak.*' \) \
    | sort)

total=${#backups[@]}
if (( total <= KEEP )); then
    echo "OK: $total backup(s) in $CADDY_DIR, threshold KEEP=$KEEP. Nothing to do."
    exit 0
fi

# Keep the LAST KEEP entries (newest by name = newest by timestamp).
keep_start=$((total - KEEP))
to_keep=("${backups[@]:$keep_start}")
to_delete=("${backups[@]:0:$keep_start}")

echo "Keeping (newest $KEEP):"
for f in "${to_keep[@]}"; do echo "  + $f"; done

echo
echo "Would delete (older, $((total - KEEP)) file(s)):"
for f in "${to_delete[@]}"; do echo "  - $f"; done

if (( APPLY == 0 )); then
    echo
    echo "Dry-run. Pass --apply to actually delete."
    exit 0
fi

echo
for f in "${to_delete[@]}"; do
    rm -f -- "$f"
    echo "deleted: $f"
done
echo "Done."
