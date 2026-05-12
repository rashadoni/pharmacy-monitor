"""Telegram бот: long-polling процесс отвечающий на команды.

Запуск:
    pharmacy-monitor telegram run-bot

Поддерживаемые команды:
    /start  — приветствие + пояснение как привязать chat_id
    /help   — список команд
    /today  — краткая сводка за сегодня (KPI + топ-3 действия)
    /alerts — последние 5 алертов
    /status — здоровье системы

Использует raw HTTP через urllib (без python-telegram-bot — лишняя зависимость).
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from src._time import utcnow
from typing import Callable

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src import notifier, storage

log = structlog.get_logger()


def cmd_start(session: Session, chat_id: str, args: str) -> str:
    """Привязка chat_id к существующему TenantUser по email.

    Usage: /start <email>     — пытается привязать к пользователю с этим email
           /start             — без аргумента просто показывает chat_id и инструкцию
    """
    from src import notifications

    email = args.strip().lower() if args else ""
    if email and "@" in email:
        bound = notifications.bind_telegram(session, chat_id, email)
        if bound:
            return (
                "✅ *Привязано!*\n\n"
                f"Email: `{email}`\n"
                f"Chat ID: `{chat_id}`\n\n"
                "Теперь будешь получать алерты сюда (по умолчанию — critical).\n"
                "Настройки уведомлений: дашборд → ⚙️ Настройки.\n\n"
                "Команды: /today /alerts /status /help"
            )
        return (
            f"❌ Email `{email}` не найден или неактивен.\n\n"
            "Сначала аккаунт должен быть создан (admin → Получатели в дашборде)."
        )
    return (
        "👋 *Привет!* Это Pharmacy Monitor.\n\n"
        f"Твой `chat_id`: `{chat_id}`\n\n"
        "Чтобы привязать chat к аккаунту:\n"
        "  `/start your-email@example.com`\n\n"
        "После этого будешь получать сюда алерты автоматически.\n\n"
        "Команды:\n"
        "/today — что нового за сегодня\n"
        "/alerts — последние алерты\n"
        "/status — здоровье системы\n"
        "/help — справка"
    )


def cmd_help(session: Session, chat_id: str, args: str) -> str:
    return (
        "*Pharmacy Monitor бот*\n\n"
        "/start — приветствие + chat_id\n"
        "/today — KPI + топ-3 действия\n"
        "/alerts — последние 5 событий\n"
        "/status — здоровье системы\n"
        "/help — это сообщение"
    )


def cmd_today(session: Session, chat_id: str, args: str) -> str:
    """Краткая сводка: топ-3 действия из ROI."""
    from src import roi
    actions = roi.compute_actions(session)
    agg = roi.aggregate_impact(actions)
    if not actions:
        return "📊 *Сегодня:* данных пока нет — запусти прогон."

    lines = [
        f"📊 *Сегодня — {utcnow().strftime('%d.%m.%Y')}*",
        "",
        f"💰 Возможный профит: `+{agg['opportunity']:.0f} ₼/мес`",
        f"🔴 Возможные потери: `{agg['loss']:.0f} ₼/мес`",
        f"📋 Всего действий: {agg['count']}",
        "",
        "*Топ-3:*",
    ]
    for i, a in enumerate(actions[:3], 1):
        sev = {"critical": "🔴", "warning": "⚠️", "opportunity": "💡", "info": "ℹ️"}.get(
            a.severity, "•"
        )
        # Показываем разницу на единицу + % спред — реально проверяемые цифры.
        # «AZN/мес» убран 2026-05-13: множитель volume=30 был placeholder без
        # основания, см. roi.py module docstring.
        gap = a.unit_gap_azn or 0
        spread = a.spread_pct or 0
        gap_str = f"+{gap:.2f}" if gap > 0 else f"{gap:.2f}"
        lines.append(
            f"{i}. {sev} {a.title[:60]} (`{gap_str} ₼/ед`, спред {spread:.1f}%)"
        )
    return "\n".join(lines)


def cmd_alerts(session: Session, chat_id: str, args: str) -> str:
    """Последние 5 алертов."""
    events = session.scalars(
        select(storage.AlertEvent)
        .order_by(desc(storage.AlertEvent.created_at))
        .limit(5)
    ).all()
    if not events:
        return "🔔 Алертов ещё не было."
    lines = ["🔔 *Последние алерты:*\n"]
    for ev in events:
        sev = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️"}.get(ev.severity, "•")
        when = ev.created_at.strftime("%d.%m %H:%M")
        lines.append(f"{sev} `{when}` {ev.title[:60]}")
    return "\n".join(lines)


def cmd_status(session: Session, chat_id: str, args: str) -> str:
    """Health-check кратко."""
    from src import health
    rep = health.check_health(session)
    sev_emoji = {"ok": "✅", "warning": "⚠️", "critical": "🔴"}.get(rep.status, "•")
    out = [
        f"{sev_emoji} *Status:* {rep.status.upper()}",
        f"Last run: #{rep.last_run_id} ({rep.last_run_status})",
    ]
    for i in rep.issues[:5]:
        out.append(f"  • _{i.code}_: {i.message[:80]}")
    return "\n".join(out)


COMMANDS: dict[str, Callable[[Session, str, str], str]] = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/today": cmd_today,
    "/alerts": cmd_alerts,
    "/status": cmd_status,
}


def handle_update(session: Session, update: dict) -> None:
    """Обработать одно входящее сообщение."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id"))
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return  # игнорируем не-команды
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()  # /today@bot_name → /today
    args = parts[1] if len(parts) > 1 else ""

    handler = COMMANDS.get(cmd)
    if not handler:
        notifier.send_telegram_message(
            chat_id, f"Неизвестная команда: `{cmd}`. /help"
        )
        return
    try:
        response = handler(session, chat_id, args)
    except Exception as e:
        log.exception("telegram_command_failed", cmd=cmd, error=str(e))
        response = f"❌ Ошибка: {type(e).__name__}"
    notifier.send_telegram_message(chat_id, response)


def run_polling(poll_timeout: int = 30) -> None:
    """Long-polling loop: получает updates и роутит на команды.

    Поднимается отдельным процессом (`pharmacy-monitor telegram run-bot`).
    Не блокирует основной воркфлоу.
    """
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        log.error("telegram_bot_no_token", hint="Set TELEGRAM_BOT_TOKEN in .env")
        return

    log.info("telegram_bot_start")
    last_update_id: int | None = None
    Session = storage.make_session()

    while True:
        try:
            updates = notifier.telegram_get_updates(
                offset=last_update_id, timeout=poll_timeout
            )
        except Exception as e:
            log.warning("telegram_poll_error", error=str(e))
            time.sleep(5)
            continue

        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                last_update_id = update_id + 1
            with Session() as s:
                try:
                    handle_update(s, update)
                except Exception as e:
                    log.exception("telegram_handle_update_failed", error=str(e))
