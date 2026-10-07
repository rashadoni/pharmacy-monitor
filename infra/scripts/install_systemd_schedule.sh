#!/usr/bin/env bash
# Install the versioned scrape/alert schedule after an application release.
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
  pharmacy-monitor-watchlist.timer \
  pharmacy-monitor-rematch.service \
  pharmacy-monitor-rematch.timer
do
  install -m 0644 "$source_dir/$unit" "$unit_dir/$unit"
done

# Устанавливаем ВСЁ, что лежит в overrides/, а не поимённый список. Список
# ломался при переименовании: 2026-10-04 aptekonline перевели на недельный ритм
# и файл стал zz-weekly-cadence.conf, а здесь остался zz-daily-cadence.conf —
# при set -e скрипт падал на `install` ещё до daemon-reload, то есть расписание
# молча не применялось вовсе. Обход каталога делает такой рассинхрон
# невозможным: что в Git — то и на проде.
shopt -s nullglob
overrides=()
for override_path in "$source_dir"/overrides/*/*.conf; do
  overrides+=("$override_path")
done
shopt -u nullglob
if [[ ${#overrides[@]} -eq 0 ]]; then
  echo "no drop-ins found under $source_dir/overrides" >&2
  exit 3
fi
for override_path in "${overrides[@]}"; do
  override=${override_path#"$source_dir/overrides/"}
  echo "installing drop-in: $override"
  install -D -m 0644 "$override_path" "$unit_dir/$override"
done

systemctl daemon-reload

# Местный drop-in в /etc переживает установку юнита. Полный сброс пар из
# недельного юнита пережить её не должен: проверяем то, что systemd реально
# запустит, а не файл из Git.
if systemctl show --property ExecStart --value pharmacy-monitor-rematch.service \
    | grep -q -- '--reset'; then
  echo "pharmacy-monitor-rematch.service still runs rematch --reset:" >&2
  systemctl cat pharmacy-monitor-rematch.service >&2
  exit 4
fi

for timer in \
  pharmacy-monitor-scrape@pharmonline.timer \
  pharmacy-monitor-scrape@aptekonline.timer \
  pharmacy-monitor-scrape@aloe.timer \
  pharmacy-monitor-watchlist.timer \
  pharmacy-monitor-rematch.timer
do
  systemctl enable --now "$timer"
  systemctl restart "$timer"
done

systemctl list-timers --all \
  pharmacy-monitor-scrape@pharmonline.timer \
  pharmacy-monitor-scrape@aptekonline.timer \
  pharmacy-monitor-scrape@aloe.timer \
  pharmacy-monitor-watchlist.timer \
  pharmacy-monitor-rematch.timer
