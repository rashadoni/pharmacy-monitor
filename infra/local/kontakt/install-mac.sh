#!/usr/bin/env bash
# Установить ночной сбор Kontakt Home на Мак владельца. Один раз:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/rashadoni/pharmacy-monitor/main/infra/local/kontakt/install-mac.sh)
#
# Повторный запуск безопасен: обновляет код, сценарий и расписание, ключ не
# трогает. Другая ветка (до мержа): KONTAKT_BRANCH=<ветка> перед командой.
#
# Что делает:
#   1. отдельная копия репозитория и Python-окружение в
#      ~/Library/Application Support/kontakt-collector (рабочие копии
#      владельца не трогаются);
#   2. ключ ~/.ssh/kontakt_upload — только для отправки файла каталога;
#   3. расписание launchd az.leaddrive.kontakt-collect: каждый день в 04:30.
# В конце печатает открытый ключ: его один раз ставит на сервер root
# (infra/electronics/kontakt-upload-setup.sh).
set -euo pipefail

BRANCH=${KONTAKT_BRANCH:-main}
URL=https://github.com/rashadoni/pharmacy-monitor.git
BASE="$HOME/Library/Application Support/kontakt-collector"
REPO="$BASE/repo"
VENV="$BASE/venv"
KEY="$HOME/.ssh/kontakt_upload"
LABEL=az.leaddrive.kontakt-collect
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[[ $(uname) == Darwin ]] || { echo "это установка для Мака" >&2; exit 1; }
[[ -d "/Applications/Google Chrome.app" ]] || {
    echo "нужен Google Chrome в /Applications" >&2; exit 1; }

python=""
for candidate in python3.13 python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python=$(command -v "$candidate"); break
    fi
done
[[ -n $python ]] || {
    echo "нужен Python 3.11 или новее: brew install python@3.12" >&2; exit 1; }

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
"$VENV/bin/python" -m pip install -q -e "$REPO"
shasum "$REPO/pyproject.toml" | cut -d' ' -f1 > "$BASE/deps.sha"

echo "==> сценарий и расписание"
install -m 755 "$REPO/infra/local/kontakt/run.sh" "$BASE/run.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed "s|HOME_DIR|$HOME|g" "$REPO/infra/local/kontakt/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
# Сразу после bootout macOS иногда отвечает «Bootstrap failed: 5»: повтор.
for attempt in 1 2 3 4 5; do
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null && break
    (( attempt < 5 )) || { echo "launchctl bootstrap не прошёл" >&2; exit 1; }
    sleep 2
done

if [[ ! -f $KEY ]]; then
    echo "==> ключ $KEY"
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    # Без пароля: сбор идёт ночью без человека. Ключ на сервере годится только
    # для приёма файла каталога.
    ssh-keygen -q -t ed25519 -N "" -C kontakt-upload -f "$KEY"
fi

cat <<DONE

Готово. Сбор — каждый день в 04:30 (если Мак спал — после пробуждения).
Запустить сейчас:   bash "$BASE/run.sh"
Журнал:             ~/Library/Logs/kontakt-collect.log

Открытый ключ для сервера (ставится один раз):
$(cat "$KEY.pub")
DONE
