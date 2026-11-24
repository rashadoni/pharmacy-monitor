#!/usr/bin/env bash
# Разрешить Маку владельца присылать каталог Kontakt Home на сервер.
#
# Запуск от root, из каталога infra/electronics выложенного кода:
#   bash /opt/pharmacy-monitor/infra/electronics/kontakt-upload-setup.sh 'ssh-ed25519 AAAA… kontakt-upload'
#   bash …/kontakt-upload-setup.sh --remove     убрать ключ
#
# Делает две вещи:
#   1. kontakt-receive.sh → /usr/local/lib/electro-monitor/kontakt-receive
#      (каталог root: root не исполняет то, что может подменить pm);
#   2. ключ Мака → /root/.ssh/authorized_keys с принудительной командой и
#      `restrict`: этим ключом можно только прислать файл каталога, ни оболочки,
#      ни перенаправления портов. Прежняя строка этого ключа заменяется.
set -euo pipefail

LIB=/usr/local/lib/electro-monitor
TARGET=$LIB/kontakt-receive
KEYS=/root/.ssh/authorized_keys
MARK=kontakt-upload
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

[[ $EUID -eq 0 ]] || { echo "запускать от root" >&2; exit 1; }
[[ $# -eq 1 ]] || { echo "нужен один аргумент: открытый ключ Мака или --remove" >&2; exit 2; }
[[ -d $LIB ]] || { echo "нет $LIB — экземпляр электроники не установлен" >&2; exit 1; }

install -d -m 700 /root/.ssh
touch "$KEYS"; chmod 600 "$KEYS"
tmp=$(mktemp "$KEYS.XXXXXX")
trap 'rm -f "$tmp"' EXIT
# Строки с меткой — только наши: все остальные ключи остаются как были.
grep -v " $MARK\$" "$KEYS" > "$tmp" || true

if [[ $1 == --remove ]]; then
    install -m 600 "$tmp" "$KEYS"
    rm -f "$TARGET"
    echo "ключ $MARK убран, $TARGET удалён"
    exit 0
fi

read -r type blob _ <<< "$1"
[[ $type =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)$ && $blob =~ ^[A-Za-z0-9+/=]{40,}$ ]] \
    || { echo "это не открытый ключ ssh" >&2; exit 2; }

install -o root -g root -m 755 "$HERE/kontakt-receive.sh" "$TARGET"
echo "restrict,command=\"$TARGET\" $type $blob $MARK" >> "$tmp"
install -m 600 "$tmp" "$KEYS"
echo "готово: $TARGET, ключ $MARK в $KEYS"
