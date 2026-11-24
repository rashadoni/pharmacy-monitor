#!/usr/bin/env bash
# Установить на Мак владельца отправку каталога Kontakt Home. Один раз:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/rashadoni/pharmacy-monitor/main/infra/local/kontakt/install-mac.sh)
#
# Повторный запуск безопасен: обновляет код и ярлык, ключ не трогает. Другая
# ветка (до мержа): KONTAKT_BRANCH=<ветка> перед командой.
#
# Что делает:
#   1. отдельная копия репозитория и маленькое Python-окружение (только
#      openpyxl) в ~/Library/Application Support/kontakt-collector;
#   2. папка ~/Desktop/Kontakt — сюда кладутся выгрузки из Chrome;
#   3. ярлык «Kontakt — отправить.command» на рабочем столе;
#   4. ключ ~/.ssh/kontakt_upload — только для отправки файла каталога;
#   5. убирает расписание az.leaddrive.kontakt-collect, если его ставила
#      прежняя версия (автоматический сбор через Chrome, снят 2026-11-24).
# В конце печатает открытый ключ: его один раз ставит на сервер root
# (infra/electronics/kontakt-upload-setup.sh).
set -euo pipefail

BRANCH=${KONTAKT_BRANCH:-main}
URL=https://github.com/rashadoni/pharmacy-monitor.git
BASE="$HOME/Library/Application Support/kontakt-collector"
REPO="$BASE/repo"
VENV="$BASE/venv"
KEY="$HOME/.ssh/kontakt_upload"
INBOX="$HOME/Desktop/Kontakt"
SHORTCUT="$HOME/Desktop/Kontakt — отправить.command"
OLD_LABEL=az.leaddrive.kontakt-collect

[[ $(uname) == Darwin ]] || { echo "это установка для Мака" >&2; exit 1; }

python=""
for candidate in python3.13 python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        python=$(command -v "$candidate"); break
    fi
done
[[ -n $python ]] || { echo "нужен Python 3.9 или новее: brew install python@3.12" >&2; exit 1; }

echo "==> код ($BRANCH) в $REPO"
mkdir -p "$BASE"
if [[ -d $REPO/.git ]]; then
    git -C "$REPO" fetch -q origin "$BRANCH"
    git -C "$REPO" reset -q --hard FETCH_HEAD
else
    git clone -q --branch "$BRANCH" "$URL" "$REPO"
fi
echo "$BRANCH" > "$BASE/branch"

echo "==> Python-окружение ($python)"
[[ -x $VENV/bin/python ]] || "$python" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q openpyxl

echo "==> папка, сценарий и ярлык"
mkdir -p "$INBOX" "$HOME/Library/Logs"
install -m 755 "$REPO/infra/local/kontakt/upload.sh" "$BASE/upload.sh"
cat > "$SHORTCUT" <<'CMD'
#!/bin/bash
bash "$HOME/Library/Application Support/kontakt-collector/upload.sh"
echo
read -r -p "Нажмите Enter, чтобы закрыть окно…" _
CMD
chmod 755 "$SHORTCUT"

# Прежняя версия ставила ежедневный автоматический сбор — его больше нет.
if [[ -f $HOME/Library/LaunchAgents/$OLD_LABEL.plist ]]; then
    launchctl bootout "gui/$UID/$OLD_LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist" "$BASE/run.sh"
    echo "==> снято прежнее расписание $OLD_LABEL"
fi

if [[ ! -f $KEY ]]; then
    echo "==> ключ $KEY"
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    # Без пароля: ключ на сервере годится только для приёма файла каталога.
    ssh-keygen -q -t ed25519 -N "" -C kontakt-upload -f "$KEY"
fi

cat <<DONE

Готово.
1. Соберите выгрузки kontakt.az в своём Chrome (Instant Data Scraper) и
   положите файлы .xlsx в папку Kontakt на рабочем столе.
2. Дважды щёлкните «Kontakt — отправить» на рабочем столе.

Журнал: ~/Library/Logs/kontakt-upload.log

Открытый ключ для сервера (ставится один раз):
$(cat "$KEY.pub")
DONE
