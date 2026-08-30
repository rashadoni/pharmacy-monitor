#!/usr/bin/env bash
# Install the versioned daily scrape/alert schedule after an application release.
#
# Run only as root on the Pharmacy Monitor production host:
#   /opt/pharmacy-monitor/infra/scripts/install_systemd_schedule.sh
#
# The script deliberately does not delete existing drop-ins.  The checked-in
# ``zz-*`` overrides load after legacy ``override.conf`` files and reset their
# schedules/ExecStart values explicitly, leaving unrelated local hardening
# intact and making the desired cadence auditable from Git.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

repo_dir=${1:-/opt/pharmacy-monitor}
case "$repo_dir" in
  /opt/pharmacy-monitor) ;;
  *) echo "refusing unexpected repository path: $repo_dir" >&2; exit 2 ;;
esac

source_dir="$repo_dir/infra/systemd"
unit_dir=/etc/systemd/system
for unit in \
  pharmacy-monitor-scrape@.service \
  pharmacy-monitor-scrape@.timer \
  pharmacy-monitor-watchlist.service \
  pharmacy-monitor-watchlist.timer
do
  install -m 0644 "$source_dir/$unit" "$unit_dir/$unit"
done

for override in \
  "pharmacy-monitor-scrape@.service.d/zz-realtime-alerts.conf" \
  "pharmacy-monitor-scrape@pharmonline.timer.d/zz-daily-cadence.conf" \
  "pharmacy-monitor-scrape@aptekonline.timer.d/zz-daily-cadence.conf" \
  "pharmacy-monitor-scrape@aloe.timer.d/zz-daily-cadence.conf"
do
  install -D -m 0644 "$source_dir/overrides/$override" "$unit_dir/$override"
done

systemctl daemon-reload
for timer in \
  pharmacy-monitor-scrape@pharmonline.timer \
  pharmacy-monitor-scrape@aptekonline.timer \
  pharmacy-monitor-scrape@aloe.timer \
  pharmacy-monitor-watchlist.timer
do
  systemctl enable --now "$timer"
  systemctl restart "$timer"
done

systemctl list-timers --all \
  pharmacy-monitor-scrape@pharmonline.timer \
  pharmacy-monitor-scrape@aptekonline.timer \
  pharmacy-monitor-scrape@aloe.timer \
  pharmacy-monitor-watchlist.timer
