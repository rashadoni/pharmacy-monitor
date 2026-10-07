#!/bin/bash
#
# Pharmacy Monitor — one-stop interactive setup for blocked integrations.
#
# Запускается локально с Mac. Что делает:
#   1. SMTP (Resend) — для email magic-link и daily reports
#   2. Telegram bot — для push-алертов
#   3. Sentry — для error tracking
#   (блоки «GitHub remote» и «ScraperAPI Hobby» сняты 2026-10-07 — см. ниже)
#
# Каждый блок — отдельная функция, можно skip любую.
# После каждого блока обновляет /etc/pharmacy-monitor/env на проде
# и перезапускает pharmacy-monitor-api.
#
# Запуск: PROD_HOST=<адрес прода> bash scripts/configure-integrations.sh
#
# УСТАРЕЛ (помечено 2026-10-07). Адреса по умолчанию больше нет: скрипт был
# нацелен на Hetzner-сервер, удалённый в сентябре 2026, а он отправляет на
# PROD_HOST вводимые ключи. Теперь адрес задаётся явно, и соединение принимается
# только с host key из infra/prod_known_hosts.
# Блоки писались под май 2026:
#   SMTP, Sentry      — уже настроены на проде;
#   ScraperAPI Hobby  — снят, main() его не вызывает: aptekonline давно
#                       собирается на проде через Decodo, блок переписал бы
#                       SCRAPER_API_SITES;
#   GitHub remote     — снят, main() его не вызывает: репозиторий давно на
#                       GitHub, а прямой push в main запрещён
#                       (docs/DELIVERY-ARCHITECTURE.md).
# Функции обоих снятых блоков оставлены в файле как история.

set -e
trap 'echo "Прерывание. Существующая конфигурация на проде не изменена." ; exit 130' INT

PROD_HOST="${PROD_HOST:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
KNOWN_HOSTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/prod_known_hosts"
PROD_ENV_FILE="/etc/pharmacy-monitor/env"

# ────────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────────

ssh_root() {
    ssh -i "$SSH_KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes \
        -o ControlPath=none -l root "$PROD_HOST" "$@"
}

# update_env KEY VALUE — set or replace KEY=VALUE in prod env file
update_env() {
    local key="$1"
    local value="$2"
    # backslash-escape any single quotes in value before pushing via SSH
    local escaped_value
    escaped_value="${value//\'/\'\\\'\'}"
    ssh_root "if grep -q '^${key}=' '$PROD_ENV_FILE'; then \
        sed -i \"s|^${key}=.*|${key}=${escaped_value}|\" '$PROD_ENV_FILE'; \
    else \
        echo '${key}=${escaped_value}' >> '$PROD_ENV_FILE'; \
    fi"
    echo "  ✓ $key обновлён в $PROD_ENV_FILE"
}

restart_api() {
    echo "  Restart pharmacy-monitor-api..."
    ssh_root "systemctl restart pharmacy-monitor-api && sleep 2 && systemctl is-active pharmacy-monitor-api"
}

ask_yes_no() {
    local prompt="$1"
    read -r -p "$prompt [y/N]: " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

read_secret() {
    local prompt="$1"
    local value
    read -r -s -p "$prompt: " value
    echo
    echo "$value"
}

section() {
    echo
    echo "═══════════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "═══════════════════════════════════════════════════════════════════"
    echo
}

# ────────────────────────────────────────────────────────────────────────────
# 1. SMTP (Resend recommended)
# ────────────────────────────────────────────────────────────────────────────

configure_smtp() {
    section "1. SMTP (email alerts + magic-link)"
    echo "Рекомендуется Resend (бесплатно 100 emails/день, простой API)."
    echo "  Регистрация:  https://resend.com/signup"
    echo "  Создать ключ: https://resend.com/api-keys (Name: 'pharmacy-monitor')"
    echo "  Verify domain (можно потом, начнётся работать с testing@resend.dev)"
    echo
    if ! ask_yes_no "Настроить SMTP сейчас?"; then
        echo "  Пропускаю SMTP."
        return
    fi
    local api_key
    api_key=$(read_secret "Resend API key (re_xxx...)")
    if [[ -z "$api_key" ]]; then
        echo "  Пустой ключ — пропускаю."
        return
    fi
    read -r -p "Email FROM (например 'Pharmacy <noreply@your-domain.com>' или onboarding@resend.dev): " from_email
    read -r -p "Email TO (получатель отчётов; пустая = опираемся на DB recipient list): " to_email

    update_env "SMTP_HOST" "smtp.resend.com"
    update_env "SMTP_PORT" "587"
    update_env "SMTP_USER" "resend"
    update_env "SMTP_PASSWORD" "$api_key"
    [[ -n "$from_email" ]] && update_env "SMTP_FROM" "$from_email"
    [[ -n "$to_email" ]] && update_env "EMAIL_TO" "$to_email"
    restart_api
    echo "  ✓ SMTP готов. Test: ssh root@prod 'cd /opt/pharmacy-monitor && .venv/bin/pharmacy-monitor notify test'"
}

# ────────────────────────────────────────────────────────────────────────────
# 2. Telegram bot
# ────────────────────────────────────────────────────────────────────────────

configure_telegram() {
    section "2. Telegram bot (push-алерты)"
    echo "Шаги (5 минут):"
    echo "  1. Открой Telegram → найди @BotFather → /newbot"
    echo "  2. Назови бота (например 'Pharmacy Monitor Alerts')"
    echo "  3. Username бота должен заканчиваться на 'bot' (например 'pharmacy_mon_bot')"
    echo "  4. Скопируй token (формат '123456:ABC-DEF...')"
    echo "  5. Получи chat_id: напиши боту что-нибудь, потом открой"
    echo "     https://api.telegram.org/botYOUR_TOKEN/getUpdates"
    echo "     найди 'chat.id' в JSON"
    echo
    if ! ask_yes_no "Настроить Telegram сейчас?"; then
        echo "  Пропускаю Telegram."
        return
    fi
    local token chat_id
    token=$(read_secret "Bot token")
    if [[ -z "$token" ]]; then
        echo "  Пустой token — пропускаю."
        return
    fi
    read -r -p "Chat ID (число): " chat_id
    update_env "TELEGRAM_BOT_TOKEN" "$token"
    [[ -n "$chat_id" ]] && update_env "TELEGRAM_CHAT_ID" "$chat_id"
    restart_api
    echo "  ✓ Telegram готов. Test: на /settings странице фронта."
}

# ────────────────────────────────────────────────────────────────────────────
# 3. Sentry (error tracking)
# ────────────────────────────────────────────────────────────────────────────

configure_sentry() {
    section "3. Sentry (error tracking)"
    echo "Шаги:"
    echo "  1. https://sentry.io/signup/ (бесплатно 5K events/мес)"
    echo "  2. Create project → Platform: Python → Framework: FastAPI"
    echo "  3. Скопируй DSN (формат 'https://xxx@xxx.ingest.sentry.io/yyy')"
    echo
    if ! ask_yes_no "Настроить Sentry сейчас?"; then
        echo "  Пропускаю Sentry."
        return
    fi
    local dsn
    dsn=$(read_secret "Sentry DSN")
    if [[ -z "$dsn" ]]; then
        echo "  Пустой DSN — пропускаю."
        return
    fi
    update_env "SENTRY_DSN" "$dsn"
    restart_api
    echo "  ✓ Sentry готов. Test: ssh root@prod 'curl http://localhost:8080/api/v1/_debug/error' (если есть)."
}

# ────────────────────────────────────────────────────────────────────────────
# 4. ScraperAPI Hobby (residential pool — возврат aptekonline на прод)
# ────────────────────────────────────────────────────────────────────────────

configure_scraperapi() {
    section "4. ScraperAPI Hobby \$49/мес (опционально)"
    echo "Только если хочешь вернуть aptekonline на прод-скрейп (вместо Mac launchd)."
    echo "  https://www.scraperapi.com/pricing/ → Hobby (\$49/мес, 100K credits, residential pool)"
    echo "  Текущий ключ Free trial уже работает для pharmonline через default pool."
    echo "  С Hobby + residential pool aptekonline тоже пробьётся (default давал 403)."
    echo
    if ! ask_yes_no "Уже подписан на Hobby и хочешь активировать aptekonline на проде?"; then
        echo "  Пропускаю — aptekonline остаётся на Mac launchd."
        return
    fi
    local key
    key=$(read_secret "ScraperAPI key (если меняется; Enter чтобы оставить текущий)")
    [[ -n "$key" ]] && update_env "SCRAPER_API_KEY" "$key"
    # Включить residential pool флаг + добавить aptekonline в SCRAPER_API_SITES
    update_env "SCRAPER_API_SITES" "pharmonline,aptekonline"
    update_env "SCRAPER_API_PREMIUM" "true"
    # Re-enable systemd timer для aptekonline
    if ask_yes_no "Включить прод-таймер pharmacy-monitor-scrape@aptekonline.timer?"; then
        ssh_root "systemctl enable --now pharmacy-monitor-scrape@aptekonline.timer && systemctl list-timers pharmacy-monitor-scrape@*.timer --no-pager | head -5"
    fi
    restart_api
    echo "  ✓ ScraperAPI Hobby настроен. Следующий ночной прогон проверит aptekonline на проде."
}

# ────────────────────────────────────────────────────────────────────────────
# 5. GitHub remote (backup кода)
# ────────────────────────────────────────────────────────────────────────────

configure_github() {
    section "5. GitHub remote (backup исходников)"
    if git remote get-url origin >/dev/null 2>&1; then
        echo "  Remote 'origin' уже настроен:"
        git remote -v | head -2
        if ! ask_yes_no "Заменить на другой?"; then
            return
        fi
    fi
    echo "Шаги:"
    echo "  1. https://github.com/new — создай 'pharmacy-monitor' (private!)"
    echo "  2. Скопируй URL (HTTPS или SSH формата 'git@github.com:USERNAME/pharmacy-monitor.git')"
    echo
    read -r -p "Git remote URL (пустая = пропустить): " url
    if [[ -z "$url" ]]; then
        echo "  Пропускаю."
        return
    fi
    git remote remove origin 2>/dev/null || true
    git remote add origin "$url"
    echo "  Remote добавлен:"
    git remote -v
    echo
    if ask_yes_no "Push сейчас?"; then
        git push -u origin main
    else
        echo "  Push потом руками: git push -u origin main"
    fi
}

# ────────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────────

main() {
    if [[ -z "$PROD_HOST" ]]; then
        echo "PROD_HOST не задан. Прежний сервер удалён, адреса по умолчанию нет —" >&2
        echo "см. шапку скрипта и CLAUDE.md («Server»)." >&2
        exit 1
    fi
    cd "$(dirname "$0")/.."
    echo "Pharmacy Monitor — interactive integration setup"
    echo "Production target: root@$PROD_HOST (ssh key: $SSH_KEY)"
    echo
    echo "Скрипт пройдёт по 3 блокам. Каждый можно пропустить нажав 'n'."
    echo

    configure_smtp
    configure_telegram
    configure_sentry
    # configure_scraperapi и configure_github сняты 2026-10-07 — см. шапку.
    echo
    echo "Блоки «ScraperAPI Hobby» и «GitHub remote» сняты: они переписали бы рабочие настройки."

    section "Готово"
    echo "Текущая конфигурация прода (заполненные ключи):"
    ssh_root "grep -E '^(SMTP_HOST|TELEGRAM_BOT_TOKEN|SENTRY_DSN)=' '$PROD_ENV_FILE' | sed 's/=.*/=<set>/'"
    echo
    echo "Что дальше:"
    echo "  • Открой https://leaddrive.cloud/settings — проверь что нотификации показывают ✓"
    echo "  • Smoke-тест доставки (email+telegram одной командой):"
    echo "      ssh root@$PROD_HOST 'cd /opt/pharmacy-monitor && .venv/bin/pharmacy-monitor notify test'"
    echo "  • Sentry начнёт ловить ошибки автоматически на любых API-call'ах"
}

main "$@"
