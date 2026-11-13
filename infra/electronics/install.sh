#!/usr/bin/env bash
# Экземпляр электроники на сервере, где уже работает аптечный: первая установка.
#
# Что делает (каждый шаг повторно безопасен):
#   1. база electro_monitor и каталоги /opt/electro-monitor;
#   2. файл окружения /etc/electro-monitor/env — из аптечного, со своей базой,
#      своими секретами входа и профилем electronics (значения не печатаются);
#   3. код из выложенного аптечного экземпляра, миграции, сборка интерфейса
#      (sync.sh);
#   4. юниты systemd: API, интерфейс, ночные сборы irshad и bakuelectronics,
#      проверка здоровья, ежедневная синхронизация кода;
#   5. блок сайта в Caddy для адреса экземпляра.
#
# Запуск:  bash install.sh --check     — показать, что будет сделано
#          bash install.sh             — установить
#          bash install.sh --no-caddy  — всё, кроме Caddy (пока нет записи DNS)
#
# Аптечный экземпляр скрипт не трогает, кроме одного места: перезапуск Caddy
# (у него `admin off`, перезагрузка конфигурации не работает) — секунды простоя
# обоих сайтов. Откат — в конце вывода.
set -euo pipefail

DOMAIN=${ELECTRO_DOMAIN:-electro.leaddrive.cloud}
DB=electro_monitor
SRC=/opt/pharmacy-monitor
DST=/opt/electro-monitor
SRC_ENV=/etc/pharmacy-monitor/env
ENV_DIR=/etc/electro-monitor
ENV_FILE=$ENV_DIR/env
API_PORT=8090
WEB_PORT=3010
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

CHECK=0; CADDY=1
for arg in "$@"; do
    case $arg in
        --check) CHECK=1 ;;
        --no-caddy) CADDY=0 ;;
        *) echo "неизвестный параметр: $arg" >&2; exit 2 ;;
    esac
done

say() { echo "==> $*"; }
do_or_show() { if (( CHECK )); then echo "    [проверка] $*"; else "$@"; fi; }

[[ $EUID -eq 0 ]] || { echo "запускать от root" >&2; exit 1; }
[[ -d $SRC/src && -x $SRC/.venv/bin/python ]] || { echo "нет аптечного экземпляра в $SRC" >&2; exit 1; }
[[ -r $SRC_ENV ]] || { echo "нет $SRC_ENV" >&2; exit 1; }
grep -q "is_pharmacy" "$SRC/src/site_profile.py" 2>/dev/null || {
    echo "в $SRC выложен код без профилей (нет src/site_profile.py) — сначала выкладка main" >&2; exit 1; }
[[ -f $SRC/src/electronics_matcher.py && -f $SRC/src/catalog_file.py ]] || {
    echo "в $SRC нет кода экземпляра электроники — сначала выкладка main" >&2; exit 1; }

for port in $API_PORT $WEB_PORT; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port\$"; then
        systemctl is-active --quiet electro-monitor-api.service electro-monitor-frontend.service 2>/dev/null \
            || { echo "порт $port занят чужим процессом" >&2; exit 1; }
    fi
done

# --- 1. база и каталоги ------------------------------------------------------
say "база $DB и каталоги $DST"
if sudo -u postgres psql -Atc "select 1 from pg_database where datname='$DB'" | grep -q 1; then
    echo "    база уже есть"
else
    do_or_show sudo -u postgres createdb -O pm "$DB"
fi
do_or_show install -d -o pm -g pm "$DST" "$DST/data" "$DST/logs" "$DST/reports" "$DST/frontend"
do_or_show install -d -o root -g pm -m 750 "$ENV_DIR"
do_or_show install -d -o pm -g pm "$DST/infra/electronics"
do_or_show rsync -a --delete "$HERE/" "$DST/infra/electronics/"

# --- 2. файл окружения -------------------------------------------------------
say "файл окружения $ENV_FILE"
ADMIN_PASSWORD=""
if [[ -f $ENV_FILE ]]; then
    echo "    уже есть — не меняю"
elif (( CHECK )); then
    echo "    [проверка] будет создан из $SRC_ENV: своя база, свои JWT_SECRET, ключ API и пароль входа"
else
    umask 027
    tmp=$(mktemp "$ENV_DIR/env.XXXXXX")
    # Всё аптечное, что не относится к аптечным сайтам и к чужим учёткам.
    grep -Ev '^(PM_PROFILE|DATABASE_URL|REDIS_URL|JWT_SECRET|PHARMACY_API_KEY|ADMIN_PASSWORD_HASH|ADMIN_LOGIN|DECODO_SITES|PHARMACY_PUBLIC_URL|SCRAPE_REPORT_EMAIL|SENTRY_[A-Z_]*|TELEGRAM_[A-Z_]*|PHARMONLINE_[A-Z_]*|APTEKONLINE_[A-Z_]*|ALOE_[A-Z_]*|SCRAPER_API_[A-Z_]*|BRIGHTDATA_[A-Z_]*|IPROYAL_[A-Z_]*|CRAWLBASE_[A-Z_]*|AI_[A-Z_]*|DAILY_LETTER_STATE_FILE|HEALTH_ALERT_STATE_FILE|PHARMACY_CURRENT_TENANT_ID)=' \
        "$SRC_ENV" > "$tmp" || true

    src_db_url=$(grep -E '^DATABASE_URL=' "$SRC_ENV" | head -1 | cut -d= -f2-)
    [[ -n $src_db_url ]] || { rm -f "$tmp"; echo "в $SRC_ENV нет DATABASE_URL" >&2; exit 1; }
    # Та же учётка PostgreSQL, другая база: имя базы — хвост адреса после «/».
    new_db_url="${src_db_url%/*}/$DB"
    [[ $new_db_url != "$src_db_url" ]] || { rm -f "$tmp"; echo "адрес базы не удалось переписать" >&2; exit 1; }

    src_redis=$(grep -E '^REDIS_URL=' "$SRC_ENV" | head -1 | cut -d= -f2- || true)
    ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
    admin_hash=$(ADMIN_PASSWORD="$ADMIN_PASSWORD" "$SRC/.venv/bin/python" -c \
        'import os, bcrypt; print(bcrypt.hashpw(os.environ["ADMIN_PASSWORD"].encode(), bcrypt.gensalt()).decode())')
    {
        echo ""
        echo "# --- экземпляр электроники (создано install.sh $(date -u +%F)) ---"
        echo "PM_PROFILE=electronics"
        echo "DATABASE_URL=$new_db_url"
        # Своя «комната» в Redis: счётчики запросов и замки не делятся с аптеками.
        if [[ -n $src_redis ]]; then echo "REDIS_URL=$(echo "$src_redis" | sed -E 's#/[0-9]+$##')/1"; fi
        echo "JWT_SECRET=$(openssl rand -hex 32)"
        echo "PHARMACY_API_KEY=$(openssl rand -hex 24)"
        echo "ADMIN_PASSWORD_HASH=$admin_hash"
        echo "PHARMACY_PUBLIC_URL=https://$DOMAIN"
        # Через прокси ходит только Baku Electronics; Irshad открывается напрямую.
        echo "DECODO_SITES=bakuelectronics"
        echo "SCRAPE_REPORT_EMAIL=0"
    } >> "$tmp"
    chown root:pm "$tmp"; chmod 640 "$tmp"
    mv "$tmp" "$ENV_FILE"
    echo "    создан (значения не печатаются)"
fi

# --- 3. код, миграции, интерфейс --------------------------------------------
say "код, миграции и сборка интерфейса"
if (( CHECK )); then
    echo "    [проверка] $DST/infra/electronics/sync.sh"
else
    # Первый раз sync.sh считает «код изменился» и применяет миграции сам, но
    # его проверка здоровья в конце требует запущенного API: юниты — раньше.
    install -m 644 "$HERE"/systemd/electro-monitor-*.service "$HERE"/systemd/electro-monitor-*.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable electro-monitor-api.service electro-monitor-frontend.service >/dev/null
    "$DST/infra/electronics/sync.sh"
fi

# --- 4. юниты ----------------------------------------------------------------
say "юниты systemd"
if (( CHECK )); then
    ls "$HERE"/systemd | sed 's/^/    [проверка] /'
else
    systemctl enable --now electro-monitor-api.service electro-monitor-frontend.service \
        electro-monitor-health.timer electro-monitor-sync.timer \
        'electro-monitor-scrape@irshad.timer' 'electro-monitor-scrape@bakuelectronics.timer' >/dev/null
    systemctl is-active electro-monitor-api.service electro-monitor-frontend.service | sed 's/^/    /'
fi

# --- 5. Caddy ----------------------------------------------------------------
if (( CADDY )); then
    say "Caddy: сайт $DOMAIN"
    CADDYFILE=/etc/caddy/Caddyfile
    if grep -q "^$DOMAIN" "$CADDYFILE"; then
        echo "    блок уже есть"
    else
        resolved=$(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | head -1 || true)
        mine=$(hostname -I | tr ' ' '\n' | grep -Fx "${resolved:-none}" || true)
        if [[ -z $mine ]]; then
            echo "    запись DNS для $DOMAIN не указывает на этот сервер (сейчас: ${resolved:-нет записи})."
            echo "    Caddy не трогаю: без записи он не получит сертификат. Заведите A-запись и"
            echo "    повторите: bash $DST/infra/electronics/install.sh"
        elif (( CHECK )); then
            echo "    [проверка] добавить блок $DOMAIN в $CADDYFILE, caddy validate, restart"
        else
            backup="$CADDYFILE.bak.$(date -u +%Y%m%d-%H%M%S)-electro"
            cp -p "$CADDYFILE" "$backup"
            sed "s/__DOMAIN__/$DOMAIN/g; s/__API_PORT__/$API_PORT/g; s/__WEB_PORT__/$WEB_PORT/g" \
                "$HERE/Caddyfile.block" >> "$CADDYFILE"
            if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
                systemctl restart caddy
                sleep 2
                systemctl is-active --quiet caddy || { cp -p "$backup" "$CADDYFILE"; systemctl restart caddy
                    echo "Caddy не поднялся — возвращён прежний файл $backup" >&2; exit 1; }
                echo "    добавлен; прежний файл — $backup"
            else
                cp -p "$backup" "$CADDYFILE"
                echo "caddy validate отверг новый блок — файл возвращён, Caddy не перезапускался" >&2
                exit 1
            fi
        fi
    fi
fi

say "готово"
if (( ! CHECK )); then
    curl -fsS -o /dev/null "http://127.0.0.1:$API_PORT/health" && echo "    API экземпляра отвечает на 127.0.0.1:$API_PORT"
    curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/" -H 'Accept: text/html' && echo "    интерфейс отвечает на 127.0.0.1:$WEB_PORT" || true
    if [[ -n $ADMIN_PASSWORD ]]; then
        echo
        echo "    Вход: https://$DOMAIN  логин admin  пароль $ADMIN_PASSWORD"
        echo "    Пароль показан один раз и нигде не сохранён — запишите его."
    fi
    cat <<ROLLBACK

Откат (аптечный экземпляр не затрагивает):
  systemctl disable --now 'electro-monitor-*.timer' electro-monitor-api electro-monitor-frontend
  rm /etc/systemd/system/electro-monitor-*; systemctl daemon-reload
  # Caddy: вернуть файл из $CADDYFILE.bak.*-electro и systemctl restart caddy
  # База и каталог остаются: sudo -u postgres dropdb $DB; rm -rf $DST $ENV_DIR
ROLLBACK
fi
