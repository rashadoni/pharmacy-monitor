#!/usr/bin/env bash
# Ночной сбор Kontakt Home на Маке владельца: обойти каталог в настоящем
# Chrome и отправить файл на сервер. Запускает launchd
# (az.leaddrive.kontakt-collect); руками — так же:
#   bash "$HOME/Library/Application Support/kontakt-collector/run.sh"
#
# Ставит install-mac.sh: копия этого файла лежит в каталоге сборщика, а не в
# репозитории, который этот же сценарий обновляет из main.
#
# Итог — уведомление macOS и журнал ~/Library/Logs/kontakt-collect.log.
set -uo pipefail

BASE="$HOME/Library/Application Support/kontakt-collector"
REPO="$BASE/repo"
VENV="$BASE/venv"
OUT_DIR="$BASE/out"
LOG="$HOME/Library/Logs/kontakt-collect.log"
KEY="$HOME/.ssh/kontakt_upload"
SERVER=root@13.140.186.143
KEEP=14

mkdir -p "$OUT_DIR" "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') start"

notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"Kontakt\"" >/dev/null 2>&1 || true
}
finish() { echo "=== $(date '+%Y-%m-%d %H:%M:%S') $1"; notify "$1"; exit "${2:-0}"; }

# Один сбор за раз. Замок старше 8 часов — от упавшего процесса.
LOCK="$BASE/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    if [[ -n $(find "$LOCK" -maxdepth 0 -mmin +480 2>/dev/null) ]]; then
        rm -rf "$LOCK"; mkdir "$LOCK"
    else
        echo "сбор уже идёт"; exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

# Код — из ветки, записанной установкой (обычно main). Не вышло обновить —
# собираем тем, что есть.
BRANCH=$(cat "$BASE/branch" 2>/dev/null || echo main)
if git -C "$REPO" fetch -q origin "$BRANCH"; then
    git -C "$REPO" reset -q --hard FETCH_HEAD
else
    echo "git fetch не прошёл — собираю прежним кодом"
fi
echo "код: $(git -C "$REPO" rev-parse --short HEAD)"
want=$(shasum "$REPO/pyproject.toml" | cut -d' ' -f1)
if [[ $(cat "$BASE/deps.sha" 2>/dev/null) != "$want" ]]; then
    "$VENV/bin/python" -m pip install -q -e "$REPO" || finish "Kontakt: не встали зависимости" 1
    echo "$want" > "$BASE/deps.sha"
fi

out="$OUT_DIR/kontakt-$(date +%Y%m%d-%H%M).csv"
cd "$REPO" || finish "Kontakt: нет каталога кода" 1
# caffeinate: Мак не засыпает посреди обхода.
# Предел на весь обход: зависший сбор не должен держать Мак без сна и
# закрывать дорогу завтрашнему запуску.
/usr/bin/caffeinate -i "$VENV/bin/python" -m src.kontakt_collect \
    --out "$out" --profile "$BASE/chrome-profile" --max-minutes 300
code=$?
case $code in
    0) ;;
    2) finish "Kontakt: Cloudflare не пустил Chrome — сбор остановлен" 2 ;;
    4) finish "Kontakt: обход не уложился в 5 часов — файл не отправлен" 4 ;;
    3) finish "Kontakt: каталог собран не полностью — файл не отправлен" 3 ;;
    *) finish "Kontakt: сбор упал (код $code), см. kontakt-collect.log" "$code" ;;
esac
ls -1t "$OUT_DIR"/kontakt-*.csv 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -I{} rm -f -- {}

# Ключ годится только для приёма файла (принудительная команда на сервере);
# сервер узнаётся по прошитому ключу из репозитория.
# Путь к ключу сервера — без пробелов: ssh делит значение UserKnownHostsFile
# по пробелам, а каталог сборщика лежит в «Application Support».
KNOWN="$HOME/.ssh/kontakt_known_hosts"
cp "$REPO/infra/prod_known_hosts" "$KNOWN"
answer=$(/usr/bin/ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlPath=none \
    -o UserKnownHostsFile="$KNOWN" -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=30 "$SERVER" import < "$out" 2>&1)
code=$?
echo "$answer"
(( code == 0 )) || finish "Kontakt: сервер не принял файл (код $code), см. kontakt-collect.log" "$code"
products=$(($(wc -l < "$out") - 1))
finish "Kontakt: собрано $products товаров, загрузка на сервере запущена"
