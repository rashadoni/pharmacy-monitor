#!/usr/bin/env bash
# Экземпляр электроники на сервере, где уже работает аптечный: первая установка.
#
# Что делает:
#   1. сценарии экземпляра — в каталог root (/usr/local/lib/electro-monitor),
#      команда `electro-monitor` для ручных операций;
#   2. база electro_monitor и каталоги /opt/electro-monitor (владелец pm);
#   3. файл окружения /etc/electro-monitor/env: своя база, свои секреты входа,
#      профиль electronics; из аптечного берутся только почта и учётка прокси.
#      Значения не печатаются, кроме нового пароля входа — сразу после создания;
#   4. юниты systemd, затем sync.sh: код из выложенного аптечного экземпляра,
#      миграции, сборка интерфейса, запуск API и интерфейса;
#   5. блок сайта в Caddy (общий с аптечным сайтом файл и общий перезапуск).
#
# Ночные сборы и проверка здоровья НЕ включаются: сначала разделы сайтов и
# каталог клиента (команды печатаются в конце).
#
# Запуск:  bash install.sh --check          показать, что будет сделано
#          bash install.sh                  установить
#          bash install.sh --no-caddy       всё, кроме Caddy
#          bash install.sh --skip-dns-check не сверять запись DNS с адресом сервера
#
# Повторный запуск продолжает с места сбоя: уже сделанные шаги пропускаются,
# миграции и сборку sync.sh доводит сам.
#
# Аптечного экземпляра касаются два места: общий файл Caddy (новый блок
# дописывается, файл проверяется до установки, при неудачном перезапуске
# возвращается прежний) и перезапуск Caddy — секунды простоя обоих сайтов.
set -euo pipefail

DOMAIN=${ELECTRO_DOMAIN:-electro.leaddrive.cloud}
DB=electro_monitor
SRC=/opt/pharmacy-monitor
DST=/opt/electro-monitor
SRC_ENV=/etc/pharmacy-monitor/env
ENV_DIR=/etc/electro-monitor
ENV_FILE=$ENV_DIR/env
LIB=/usr/local/lib/electro-monitor
WRAPPER=/usr/local/bin/electro-monitor
CADDYFILE=/etc/caddy/Caddyfile
PY="$SRC/.venv/bin/python"
# Сам сценарий исполняет от root только системный интерпретатор в изолированном
# режиме: venv аптечного экземпляра принадлежит pm.
SYSPY="/usr/bin/python3 -I"
API_PORT=8090
WEB_PORT=3010
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

CHECK=0; CADDY=1; DNS_CHECK=1
for arg in "$@"; do
    case $arg in
        --check) CHECK=1 ;;
        --no-caddy) CADDY=0 ;;
        --skip-dns-check) DNS_CHECK=0 ;;
        *) echo "неизвестный параметр: $arg" >&2; exit 2 ;;
    esac
done

say() { echo "==> $*"; }
do_or_show() { if (( CHECK )); then echo "    [проверка] $*"; else "$@"; fi; }

[[ $EUID -eq 0 ]] || { echo "запускать от root" >&2; exit 1; }
for needed in install.sh sync.sh make-env.py Caddyfile.block systemd/electro-monitor-api.service; do
    [[ -f $HERE/$needed ]] || { echo "рядом со сценарием нет $needed — нужен весь каталог infra/electronics" >&2; exit 1; }
done
[[ -d $SRC/src && -x $PY ]] || { echo "нет аптечного экземпляра в $SRC" >&2; exit 1; }
[[ -r $SRC_ENV ]] || { echo "нет $SRC_ENV" >&2; exit 1; }
for needed in src/site_profile.py src/electronics_matcher.py src/catalog_file.py src/site_routes.py; do
    [[ -f $SRC/$needed ]] || { echo "в $SRC нет $needed — сначала выкладка main" >&2; exit 1; }
done

port_owner_ok() {  # порт свободен или занят своим юнитом
    local port=$1 unit=$2
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port\$" || return 0
    systemctl is-active --quiet "$unit"
}
port_owner_ok $API_PORT electro-monitor-api.service || { echo "порт $API_PORT занят чужим процессом" >&2; exit 1; }
port_owner_ok $WEB_PORT electro-monitor-frontend.service || { echo "порт $WEB_PORT занят чужим процессом" >&2; exit 1; }

# --- 1. сценарии в каталоге root --------------------------------------------
say "сценарии в $LIB и команда $WRAPPER"
if (( CHECK )); then
    echo "    [проверка] скопировать sync.sh, install.sh, make-env.py, Caddyfile.block, systemd/"
else
    install -d -o root -g root -m 755 "$LIB" "$LIB/systemd"
    # Повторный запуск идёт уже из $LIB: копировать файл в самого себя нельзя.
    if [[ ! $HERE -ef $LIB ]]; then
        install -o root -g root -m 755 "$HERE/install.sh" "$HERE/sync.sh" "$LIB/"
        install -o root -g root -m 644 "$HERE/make-env.py" "$HERE/Caddyfile.block" "$LIB/"
        install -o root -g root -m 644 "$HERE"/systemd/electro-monitor-* "$LIB/systemd/"
    fi
    cat > "$WRAPPER" <<WRAP
#!/usr/bin/env bash
# Команда экземпляра электроники: его окружение, его каталог, его код.
# \`pharmacy-monitor …\` из venv исполнил бы аптечный код — для этого экземпляра
# всегда эта команда.
#
# Команда идёт во временном юните от учётки pm, в каталоге экземпляра:
#   * путь к файлу для \`catalog import\` — полный, и файл должен читаться pm
#     (из /root она его не прочтёт; положите, например, в /tmp);
#   * Ctrl-C останавливает ожидание, но не саму команду: её юнит видно в
#     \`systemctl list-units 'run-*'\`, остановить — \`systemctl stop <юнит>\`.
[[ \$EUID -eq 0 ]] || { echo "electro-monitor: запускать от root" >&2; exit 1; }
exec systemd-run --quiet --wait --pipe --collect --expand-environment=no \\
    -p User=pm -p Group=pm \\
    -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$DST \\
    $PY -m src.main "\$@"
WRAP
    chown root:root "$WRAPPER"; chmod 755 "$WRAPPER"
fi

# --- 2. файл окружения, база, каталоги --------------------------------------
say "файл окружения $ENV_FILE"
do_or_show install -d -o root -g pm -m 750 "$ENV_DIR"
if [[ -f $ENV_FILE ]]; then
    echo "    уже есть — не меняю (пароль входа был показан при создании)"
    db_role=$($SYSPY -c 'import sys
from urllib.parse import urlsplit
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("DATABASE_URL="):
        print(urlsplit(line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))).username or "")
        break' "$ENV_FILE")
elif (( CHECK )); then
    echo "    [проверка] создать: своя база, свои JWT_SECRET, ключ API и пароль входа;"
    echo "    [проверка] из аптечного — только почта для отправки и учётка прокси"
    db_role=pm
else
    made=$(umask 077; $SYSPY "$LIB/make-env.py" "$SRC_ENV" "$ENV_FILE" "$DB" "https://$DOMAIN")
    chown root:pm "$ENV_FILE"; chmod 640 "$ENV_FILE"
    db_role=$(sed -n 1p <<<"$made")
    echo "    создан (значения не печатаются)"
    echo
    echo "    Вход: https://$DOMAIN  логин admin  пароль $(sed -n 2p <<<"$made")"
    echo "    Пароль показан один раз и нигде не сохранён — запишите его сейчас."
    echo
    unset made
fi
[[ -n ${db_role:-} ]] || { echo "в адресе базы нет имени роли PostgreSQL" >&2; exit 1; }

say "база $DB (роль $db_role) и каталоги $DST"
if sudo -u postgres psql -Atc "select 1 from pg_database where datname='$DB'" | grep -q 1; then
    echo "    база уже есть"
else
    do_or_show sudo -u postgres createdb -O "$db_role" "$DB"
fi
do_or_show install -d -o pm -g pm "$DST" "$DST/data" "$DST/logs" "$DST/reports" \
    "$DST/frontend" "$DST/frontend-src"

# --- 3. юниты и первая синхронизация ----------------------------------------
say "юниты systemd, код, миграции, сборка интерфейса"
if (( CHECK )); then
    ls "$HERE"/systemd | sed 's/^/    [проверка] /'
    echo "    [проверка] $LIB/sync.sh"
else
    install -o root -g root -m 644 "$LIB"/systemd/electro-monitor-* /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable electro-monitor-api.service electro-monitor-frontend.service >/dev/null
    if ! "$LIB/sync.sh"; then
        echo >&2
        echo "Шаг не завершён: синхронизация кода упала (причина — выше)." >&2
        echo "После исправления повторите: bash $LIB/install.sh" >&2
        exit 1
    fi
    systemctl enable --now electro-monitor-sync.timer >/dev/null
    systemctl is-active electro-monitor-api.service electro-monitor-frontend.service | sed 's/^/    /'
fi

# --- 4. Caddy ----------------------------------------------------------------
if (( CADDY )); then
    say "Caddy: сайт $DOMAIN"
    if grep -q "^$DOMAIN\b" "$CADDYFILE"; then
        echo "    блок уже есть"
    else
        resolved=$(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | head -1 || true)
        on_this_host=$(hostname -I | tr ' ' '\n' | grep -Fx "${resolved:-none}" || true)
        if [[ -z $resolved ]]; then
            echo "    записи DNS для $DOMAIN нет. Caddy не трогаю: без неё он не получит сертификат."
            echo "    Заведите A-запись и повторите: bash $LIB/install.sh"
        elif (( DNS_CHECK )) && [[ -z $on_this_host ]]; then
            echo "    запись DNS для $DOMAIN указывает на $resolved — среди адресов этого сервера его нет."
            echo "    Если это он и есть (адрес за NAT), повторите с --skip-dns-check."
        elif (( CHECK )); then
            echo "    [проверка] собрать $CADDYFILE.new с блоком $DOMAIN, проверить, установить, перезапустить"
        else
            caddy_user=$(systemctl show -p User --value caddy.service)
            new="$CADDYFILE.new"
            backup="$CADDYFILE.bak.$(date -u +%Y%m%d-%H%M%S)-electro"
            cp -p "$CADDYFILE" "$new"
            sed "s/__DOMAIN__/$DOMAIN/g; s/__API_PORT__/$API_PORT/g; s/__WEB_PORT__/$WEB_PORT/g" \
                "$LIB/Caddyfile.block" >> "$new"
            # Проверка — копии файла и от имени службы: validate создаёт файлы,
            # на которые ссылается конфигурация, и созданное root служба не откроет.
            if ! sudo -u "${caddy_user:-caddy}" caddy validate --config "$new" --adapter caddyfile; then
                rm -f "$new"
                echo "caddy validate отверг новый блок — живой файл не менялся" >&2
                exit 1
            fi
            cp -p "$CADDYFILE" "$backup"
            cp -p "$new" "$CADDYFILE"; rm -f "$new"
            if ! systemctl restart caddy || ! { sleep 2; systemctl is-active --quiet caddy; }; then
                cp -p "$backup" "$CADDYFILE"
                systemctl restart caddy || true
                echo "Caddy не поднялся с новым блоком — возвращён прежний файл ($backup)." >&2
                systemctl is-active caddy >&2 || echo "Caddy НЕ ЗАПУЩЕН: journalctl -u caddy" >&2
                exit 1
            fi
            echo "    добавлен; прежний файл — $backup"
        fi
    fi
fi

say "готово"
if (( ! CHECK )); then
    curl -fsS -o /dev/null "http://127.0.0.1:$API_PORT/health" && echo "    API экземпляра отвечает на 127.0.0.1:$API_PORT"
    cat <<NEXT

Дальше — руками, от root:
  electro-monitor routes sync --site bakuelectronics --dry-run   # разделы сайта (через прокси)
  electro-monitor routes sync --site bakuelectronics
  electro-monitor routes sync --site irshad                      # минуты: пауза 30 секунд на запрос
  electro-monitor catalog import --site kontakt /путь/файл.xlsx --dry-run
  electro-monitor catalog import --site kontakt /путь/файл.xlsx

Когда разделы записаны — включить ночные сборы и проверку здоровья:
  systemctl enable --now 'electro-monitor-scrape@bakuelectronics.timer' \\
      'electro-monitor-scrape@irshad.timer' electro-monitor-health.timer

Откат (аптечный экземпляр не затрагивает, кроме перезапуска Caddy):
  systemctl disable --now 'electro-monitor-*.timer' electro-monitor-api electro-monitor-frontend
  rm /etc/systemd/system/electro-monitor-* $WRAPPER; rm -r $LIB; systemctl daemon-reload
  # Caddy: вернуть файл из $CADDYFILE.bak.*-electro и systemctl restart caddy
  # База и каталоги остаются: sudo -u postgres dropdb $DB; rm -rf $DST $ENV_DIR
NEXT
fi
