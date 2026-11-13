#!/usr/bin/env bash
# Экземпляр электроники: подтянуть код из выложенного аптечного экземпляра.
#
# Выкладка в проекте одна (`deploy.yml` → /opt/pharmacy-monitor). Второй
# экземпляр живёт в /opt/electro-monitor и берёт оттуда тот же код: этот скрипт
# копирует его, применяет миграции к своей базе, пересобирает интерфейс со
# своим профилем, если он изменился, и перезапускает свои юниты.
#
# Запускает root — руками после выкладки или таймером electro-monitor-sync —
# из каталога, принадлежащего root (/usr/local/lib/electro-monitor). От root
# здесь делается только то, что иначе нельзя: запуск и перезапуск юнитов. Всё,
# что читает и пишет каталоги учётки pm, исполняется от pm: root не ходит по
# дереву, которое pm может подменить.
#
# Повторный запуск безопасен и доводит до конца то, что оборвалось: миграции и
# метка базы применяются каждый раз (на готовой базе это пусто).
set -euo pipefail

SRC=/opt/pharmacy-monitor
DST=/opt/electro-monitor
ENV_FILE=/etc/electro-monitor/env
PY="$SRC/.venv/bin/python"
PROFILE=electronics
API_PORT=8090

[[ $EUID -eq 0 ]] || { echo "sync: запускать от root" >&2; exit 1; }
[[ -r $ENV_FILE ]] || { echo "sync: нет $ENV_FILE — сначала install.sh" >&2; exit 1; }
grep -qx "PM_PROFILE=$PROFILE" "$ENV_FILE" || {
    echo "sync: в $ENV_FILE нет строки PM_PROFILE=$PROFILE — отказ" >&2; exit 1; }
grep -q '^DATABASE_URL=' "$ENV_FILE" || { echo "sync: в $ENV_FILE нет DATABASE_URL — отказ" >&2; exit 1; }

# Под идущим сбором код не меняют: сбор Irshad длится часами и дочитал бы
# половину модулей новыми. Юнит сбора — oneshot: пока сбор идёт, его состояние
# «activating», и `is-active` ответил бы «нет» — поэтому состояние читается явно.
for site in bakuelectronics irshad; do
    state=$(systemctl show -p ActiveState --value "electro-monitor-scrape@$site.service" 2>/dev/null || true)
    case $state in
        activating|active|deactivating|reloading)
            echo "sync: идёт сбор $site — пропуск, повторить после него"
            exit 0 ;;
    esac
done
# Сбор или загрузка каталога, запущенные руками командой electro-monitor,
# идут во временном юните; их видно только по процессу.
if pgrep -u pm -f 'python -m src\.main( --log-level [A-Za-z]+)? (run|scrape|catalog|rematch)' >/dev/null 2>&1; then
    # Так же запускаются аптечные сборы из GitHub Actions: совпадение по времени
    # отложит синхронизацию до следующего запуска — это безвредно.
    echo "sync: идёт команда run / scrape / catalog / rematch — пропуск"
    exit 0
fi

as_pm() { sudo -u pm -H "$@"; }

# Команда экземпляра. Файл окружения читает systemd, а не оболочка: он не
# обязан быть годным для bash (пробелы, «$» в хеше пароля, «&» в адресе базы).
as_instance() {
    systemd-run --quiet --wait --pipe --collect \
        -p User=pm -p Group=pm \
        -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$DST" \
        "$@"
}

fingerprint() {  # отпечаток дерева файлов относительно каталога $1
    local dir=$1; shift
    as_pm bash -c 'cd "$1" 2>/dev/null || exit 0; shift
        find "$@" -type f ! -path "*/__pycache__/*" ! -name "*.pyc" \
            ! -path "./.next/*" ! -path "./node_modules/*" ! -name "*.tsbuildinfo" -print0 2>/dev/null \
            | sort -z | xargs -0 -r sha256sum | sha256sum | cut -d" " -f1' _ "$dir" "$@"
}

# --- код ---------------------------------------------------------------------
# Сверка по содержимому (--checksum), как в выкладке: правку, не изменившую
# размер файла в ту же секунду, сверка по времени пропустила бы.
as_pm rsync -a --checksum --delete --safe-links --exclude '__pycache__/' --exclude '*.pyc' "$SRC/src/" "$DST/src/"
# env.py и alembic.ini — те, что лежат в живом аптечном каталоге: выкладка их
# там намеренно не обновляет, и миграции обоих экземпляров идут на одном и том же.
as_pm rsync -a --checksum --delete --safe-links --exclude '__pycache__/' --exclude '*.pyc' "$SRC/migrations/" "$DST/migrations/"
as_pm rsync -a --checksum --safe-links "$SRC/alembic.ini" "$DST/alembic.ini"
if [[ -d $SRC/templates ]]; then as_pm rsync -a --checksum --delete --safe-links "$SRC/templates/" "$DST/templates/"; fi
# Исходники интерфейса; сборка, зависимости и приватные файлы сборки аптечного
# экземпляра (.env*) сюда не переходят.
# ⚠️ Исключения — без косой черты на конце: с ней правило rsync ловит только
# каталоги, а `.next` в живом аптечном каталоге — ссылка на его действующую
# сборку. Скопированная ссылка означала бы сборку интерфейса электроники прямо
# поверх работающего аптечного сайта. --safe-links не переносит ни одну ссылку,
# ведущую за пределы каталога.
as_pm rsync -a --checksum --delete --safe-links \
    --exclude 'node_modules' --exclude '.next' --exclude '.next.*' --exclude '*.tsbuildinfo' \
    --exclude 'test-results' --exclude '.env*' --exclude '.electro-*' \
    "$SRC/frontend/" "$DST/frontend-src/"

# --- база: каждый раз --------------------------------------------------------
as_instance "$PY" -m alembic upgrade head
# Первый запуск ставит на базе метку профиля; дальше — проверка, что она та.
as_instance "$PY" -m src.main init-db

code_now=$(fingerprint "$DST" src migrations alembic.ini)
code_was=$(cat "$DST/.electro-code-fingerprint" 2>/dev/null || true)
if [[ $code_now != "$code_was" ]] || ! systemctl is-active --quiet electro-monitor-api.service; then
    echo "sync: перезапуск API"
    systemctl restart electro-monitor-api.service
    echo "$code_now" | as_pm tee "$DST/.electro-code-fingerprint" >/dev/null
else
    echo "sync: код без изменений"
fi

# --- интерфейс ---------------------------------------------------------------
front_now=$(fingerprint "$DST/frontend-src" .)
front_was=$(cat "$DST/frontend/.electro-build-fingerprint" 2>/dev/null || true)
if [[ $front_now != "$front_was" || ! -f $DST/frontend/.next/standalone/server.js ]]; then
    echo "sync: сборка интерфейса (в стороне, работающий сайт не трогается)"
    # Собирается в frontend-src; в рабочий каталог переносится готовая сборка.
    # Каталог сборки обязан быть своим, а не ссылкой куда-то ещё.
    for own in "$DST/frontend-src/.next" "$DST/frontend-src/node_modules" "$DST/frontend/.next"; do
        if as_pm test -L "$own"; then
            echo "sync: $own — ссылка, а не каталог; отказ (сборка ушла бы в чужое место)" >&2
            exit 1
        fi
    done
    as_pm bash -lc "cd '$DST/frontend-src' \
        && NODE_OPTIONS=--max-old-space-size=3072 pnpm install --frozen-lockfile </dev/null \
        && NEXT_PUBLIC_PM_PROFILE=$PROFILE NEXT_PUBLIC_API_URL=http://127.0.0.1:$API_PORT \
           NODE_OPTIONS=--max-old-space-size=3072 pnpm build </dev/null \
        && test -s .next/BUILD_ID && test -s .next/standalone/server.js && test -d .next/static"
    as_pm bash -c 'set -e; cd "$1"
        rm -rf frontend/.next.new frontend/.next.old
        cp -a frontend-src/.next frontend/.next.new
        rm -rf frontend/.next.new/cache
        rm -rf frontend/public && { [ ! -d frontend-src/public ] || cp -a frontend-src/public frontend/public; }
        [ ! -d frontend/.next ] || mv frontend/.next frontend/.next.old
        mv frontend/.next.new frontend/.next
        rm -rf frontend/.next.old' _ "$DST"
    systemctl restart electro-monitor-frontend.service
    echo "$front_now" | as_pm tee "$DST/frontend/.electro-build-fingerprint" >/dev/null
else
    echo "sync: интерфейс без изменений"
fi

# --- проверка ----------------------------------------------------------------
for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$API_PORT/health"; then
        echo "sync: API отвечает"
        exit 0
    fi
    sleep 2
done
echo "sync: API не отвечает на /health за 40 секунд — journalctl -u electro-monitor-api" >&2
exit 1
