#!/usr/bin/env bash
# Приём каталога Kontakt Home, собранного на Маке владельца
# (infra/local/kontakt/run.sh). Ставится kontakt-upload-setup.sh в
# /usr/local/lib/electro-monitor/kontakt-receive и исполняется как
# принудительная команда ключа Мака в /root/.ssh/authorized_keys:
# этот ключ ничего другого на сервере сделать не может.
#
# Что приходит в $SSH_ORIGINAL_COMMAND (иначе — отказ):
#   import   файл CSV на stdin: сохранить, проверить (`catalog import
#            --dry-run`), запустить загрузку отдельным юнитом и сразу ответить;
#   dry-run  то же без загрузки — проверка файла;
#   status   итог последней загрузки;
#   ping     проверка ключа.
#
# Загрузка идёт временным юнитом и не держит соединение: прогон ждёт общего
# замка сбора, а ночной сбор Irshad держит его часами.
set -euo pipefail
umask 022

INBOX=/var/lib/electro-monitor/kontakt-inbox
ENV_FILE=/etc/electro-monitor/env
DST=/opt/electro-monitor
PY=/opt/pharmacy-monitor/.venv/bin/python
UNIT_PREFIX=electro-monitor-kontakt-import
MAX_BYTES=$((64 * 1024 * 1024))
KEEP=14
HEADER='name,url,price,sale_price,category,availability'

say() { echo "kontakt-receive: $*"; }
die() { echo "kontakt-receive: $*" >&2; exit "${2:-1}"; }

as_instance() {  # команда экземпляра электроники от pm (как electro-monitor)
    systemd-run --quiet --wait --pipe --collect --expand-environment=no \
        -p User=pm -p Group=pm \
        -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$DST" \
        "$PY" -m src.main "$@"
}

running_unit() {  # идущая загрузка, если есть
    systemctl list-units --state=active,activating --plain --no-legend \
        "$UNIT_PREFIX-*.service" 2>/dev/null | awk '{print $1}' | head -n 1
}

action=${SSH_ORIGINAL_COMMAND:-import}
case $action in
    ping) say "ok"; exit 0 ;;
    status)
        # Удачный временный юнит systemd выгружает сразу, поэтому итог
        # читается из журнала по метке, а не из юнита.
        unit=$(running_unit)
        [[ -n $unit ]] && say "идёт: $unit"
        journalctl -t kontakt-import -n 20 --no-pager -o short-iso 2>/dev/null | cut -c1-300
        exit 0 ;;
    import|dry-run) ;;
    *) die "неизвестная команда" 64 ;;
esac

[[ -r $ENV_FILE && -x $PY ]] || die "экземпляр электроники не установлен"
install -d -o root -g root -m 755 /var/lib/electro-monitor "$INBOX"

exec 9>/run/electro-monitor-kontakt-receive.lock
flock -n 9 || die "идёт приём предыдущего файла" 75
busy=$(running_unit)
[[ $action == import && -n $busy ]] && die "предыдущая загрузка ещё идёт ($busy)" 75

stamp=$(date -u +%Y%m%dT%H%M%SZ)
part="$INBOX/.kontakt-$stamp.part"
file="$INBOX/kontakt-$stamp.csv"
trap 'rm -f "$part"' EXIT
head -c $((MAX_BYTES + 1)) > "$part"
size=$(stat -c %s "$part")
(( size > 0 )) || die "пустой файл" 65
(( size <= MAX_BYTES )) || die "файл больше $((MAX_BYTES / 1024 / 1024)) МБ" 65
first=$(head -n 1 "$part" | tr -d '\r')
[[ $first == "$HEADER" ]] || die "заголовок файла не тот: ${first:0:120}" 65
chmod 644 "$part"
mv "$part" "$file"
trap - EXIT
# Хранится последние $KEEP файлов: их хватает, чтобы разобрать неделю сбоев.
ls -1t "$INBOX"/kontakt-*.csv 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --

say "файл $(basename "$file"), $size байт"
# Сначала проверка без записи: файл с ошибками отвергается здесь, и Мак видит
# причины сразу, а не в журнале сервера.
as_instance catalog import --site kontakt "$file" --dry-run
[[ $action == dry-run ]] && { say "dry-run: загрузка не запускалась"; exit 0; }

unit="$UNIT_PREFIX-$stamp"
systemd-run --quiet --no-block --unit="$unit" --expand-environment=no \
    -p User=pm -p Group=pm \
    -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$DST" \
    -p SyslogIdentifier=kontakt-import \
    "$PY" -m src.main catalog import --site kontakt "$file"
say "загрузка запущена: $unit (итог — команда status)"
