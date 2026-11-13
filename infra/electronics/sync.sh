#!/usr/bin/env bash
# Экземпляр электроники: подтянуть код из выложенного аптечного экземпляра.
#
# Выкладка в проекте одна (`deploy.yml` → /opt/pharmacy-monitor). Второй
# экземпляр живёт в /opt/electro-monitor и берёт оттуда тот же код: этот скрипт
# копирует его, применяет миграции к своей базе, пересобирает интерфейс со
# своим профилем, если он изменился, и перезапускает свои юниты.
#
# Запускает root: руками после выкладки или таймером electro-monitor-sync.
# Повторный запуск без изменений ничего не делает.
set -euo pipefail

SRC=/opt/pharmacy-monitor
DST=/opt/electro-monitor
ENV_FILE=/etc/electro-monitor/env
PY="$SRC/.venv/bin/python"
PROFILE=electronics

[[ $EUID -eq 0 ]] || { echo "sync: запускать от root" >&2; exit 1; }
[[ -r $ENV_FILE ]] || { echo "sync: нет $ENV_FILE — сначала install.sh" >&2; exit 1; }
grep -qx "PM_PROFILE=$PROFILE" "$ENV_FILE" || {
    echo "sync: в $ENV_FILE нет строки PM_PROFILE=$PROFILE — отказ" >&2; exit 1; }

# Команда экземпляра: окружение из его файла, код из его каталога.
as_instance() {
    sudo -u pm -H bash -c 'set -a; . "$1"; set +a; cd "$2"; shift 2; exec "$@"' _ "$ENV_FILE" "$DST" "$@"
}

before=$(cd "$DST" 2>/dev/null && find src migrations -type f -name '*.py' -print0 2>/dev/null | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum || true)
front_before=$(cd "$DST/frontend" 2>/dev/null && find src messages package.json next.config.mjs -type f -print0 2>/dev/null | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum || true)

# Код и миграции. Данные, журналы, отчёты и сборка интерфейса — свои.
rsync -a --delete "$SRC/src/" "$DST/src/"
rsync -a --delete "$SRC/migrations/" "$DST/migrations/"
rsync -a "$SRC/alembic.ini" "$DST/alembic.ini"
[[ -d $SRC/templates ]] && rsync -a --delete "$SRC/templates/" "$DST/templates/"
rsync -a --delete --exclude .next --exclude node_modules "$SRC/frontend/" "$DST/frontend/"
# Зависимости интерфейса те же, что у аптечного: копия без обращения в сеть.
rsync -a --delete "$SRC/frontend/node_modules/" "$DST/frontend/node_modules/"
chown -R pm:pm "$DST/src" "$DST/migrations" "$DST/alembic.ini" "$DST/frontend"

after=$(cd "$DST" && find src migrations -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum | sha256sum)
front_after=$(cd "$DST/frontend" && find src messages package.json next.config.mjs -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)

if [[ $before != "$after" ]]; then
    echo "sync: код изменился — миграции и перезапуск API"
    as_instance "$PY" -m alembic upgrade head
    # Первый запуск ставит на базе метку профиля; дальше — проверка, что она та.
    as_instance "$PY" -m src.main init-db
    systemctl restart electro-monitor-api.service
else
    echo "sync: код без изменений"
fi

if [[ $front_before != "$front_after" || ! -f $DST/frontend/.next/standalone/server.js ]]; then
    echo "sync: интерфейс изменился — сборка"
    sudo -u pm -H bash -c "cd '$DST/frontend' && NEXT_PUBLIC_PM_PROFILE=$PROFILE NEXT_PUBLIC_API_URL=http://127.0.0.1:8090 NODE_OPTIONS=--max-old-space-size=3072 node_modules/.bin/next build"
    systemctl restart electro-monitor-frontend.service
else
    echo "sync: интерфейс без изменений"
fi

sleep 3
curl -fsS -o /dev/null http://127.0.0.1:8090/health && echo "sync: API отвечает" || {
    echo "sync: API не отвечает на /health" >&2; exit 1; }
