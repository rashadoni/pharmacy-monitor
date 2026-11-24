#!/usr/bin/env bash
# Отправить каталог Kontakt Home на сервер: склеить выгрузки из папки и
# загрузить. Запускается двойным кликом по «Kontakt — отправить.command» на
# рабочем столе (его ставит install-mac.sh); руками — так же:
#   bash "$HOME/Library/Application Support/kontakt-collector/upload.sh"
#
# Выгрузки собирает владелец в своём Chrome (Instant Data Scraper по разделам
# kontakt.az) и кладёт в папку ~/Desktop/Kontakt. После удачной отправки
# файлы уезжают в ~/Desktop/Kontakt/отправлено/<дата>, папка снова пуста.
#
# Копия этого файла лежит в каталоге сборщика: сам он обновляет репозиторий.
set -uo pipefail

BASE="$HOME/Library/Application Support/kontakt-collector"
REPO="$BASE/repo"
VENV="$BASE/venv"
INBOX="$HOME/Desktop/Kontakt"
LOG="$HOME/Library/Logs/kontakt-upload.log"
KEY="$HOME/.ssh/kontakt_upload"
SERVER=root@13.140.186.143

mkdir -p "$INBOX" "$(dirname "$LOG")"
# Вывод — и в окно Терминала, и в журнал.
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') отправка каталога Kontakt"

notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"Kontakt\"" >/dev/null 2>&1 || true
}
finish() { echo; echo ">>> $1"; notify "$1"; exit "${2:-0}"; }

shopt -s nullglob
files=("$INBOX"/*.xlsx)
(( ${#files[@]} > 0 )) || finish "В папке $INBOX нет выгрузок .xlsx — нечего отправлять" 1
echo "выгрузок: ${#files[@]}"

# Код — из ветки, записанной установкой (обычно main). Не вышло обновить —
# работаем тем, что есть.
BRANCH=$(cat "$BASE/branch" 2>/dev/null || echo main)
if git -C "$REPO" fetch -q origin "$BRANCH"; then
    git -C "$REPO" reset -q --hard FETCH_HEAD
else
    echo "git fetch не прошёл — работаю прежним кодом"
fi

# Ключ сервера — в пути без пробелов: ssh делит значение UserKnownHostsFile
# по пробелам, а каталог сборщика лежит в «Application Support».
KNOWN="$HOME/.ssh/kontakt_known_hosts"
cp "$REPO/infra/prod_known_hosts" "$KNOWN"
server() {
    /usr/bin/ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlPath=none \
        -o UserKnownHostsFile="$KNOWN" -o StrictHostKeyChecking=yes \
        -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=10 "$SERVER" "$@"
}

# Сервер доступен и ждёт те же колонки, что пишет склейка.
expected=$(server ping 2>&1) || finish "Сервер не отвечает на ключ — см. $LOG" 1
ours=$(cd "$REPO" && "$VENV/bin/python" -c 'from src.kontakt_merge import COLUMNS; print(",".join(COLUMNS))')
[[ $expected == "$ours" ]] || finish "Колонки файла и приёма на сервере разошлись — ничего не отправлено" 1

out="$BASE/out/kontakt-$(date +%Y%m%d-%H%M).csv"
mkdir -p "$BASE/out"
(cd "$REPO" && "$VENV/bin/python" -m src.kontakt_merge "$INBOX" --out "$out") \
    || finish "Склейка не удалась — ничего не отправлено, см. $LOG" 1

answer=$(server import < "$out" 2>&1)
code=$?
echo "$answer"
(( code == 0 )) || finish "Сервер не принял файл (код $code) — выгрузки остались в папке, см. $LOG" "$code"

sent="$INBOX/отправлено/$(date +%Y-%m-%d_%H%M)"
mkdir -p "$sent"
mv "${files[@]}" "$sent/"
[[ -f $INBOX/catalog-status.csv ]] && mv "$INBOX/catalog-status.csv" "$sent/"
ls -1t "$BASE/out"/kontakt-*.csv 2>/dev/null | tail -n +15 | xargs -I{} rm -f -- {}
products=$(($(wc -l < "$out") - 1))
finish "Отправлено $products товаров Kontakt — сервер загружает (минуты; если идёт ночной сбор Irshad — после него)"
