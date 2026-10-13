"""Notification dispatch — centralized routing of alerts to email + telegram.

Replaces the scattered logic in src/alerts.py:_send_*_alert. New logic respects:
  - Per-tenant_user severity_min thresholds (info/warning/critical/off)
  - Quiet hours (don't ping telegram at 3 AM)
  - Daily / weekly digest opt-in (events queued, sent in batch)

Public API:
  - dispatch_event(session, event)        — for real-time alerts (called by alerts.evaluate_rules)
  - send_daily_letter(session, tenant_id) — утреннее письмо: разница цен + события за сутки
                                            (systemd timer, 09:00 Asia/Baku); возвращает исход
  - send_daily_digest(session, tenant_id) — то же, числом получателей (кнопка в дашборде)
  - send_weekly_digest(session, tenant_id) — call from systemd timer Mondays
  - bind_telegram(session, chat_id, email) — link telegram to user

Severity ordering (low → high):  info < warning < critical
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from html import escape
from typing import Iterable
from urllib.parse import urlencode

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src import (
    cadence,
    comparison_export,
    comparison_rows,
    daily_letter_state,
    notifier,
    product_policy,
    storage,
)
from src._time import utcnow

log = structlog.get_logger()

SEVERITY_ORDER = {"off": 100, "critical": 3, "warning": 2, "info": 1}
DEFAULT_EMAIL_SEVERITY = "warning"
DEFAULT_TELEGRAM_SEVERITY = "critical"


def _severity_passes(threshold: str | None, event_severity: str, default: str) -> bool:
    """True if event_severity meets-or-exceeds the threshold."""
    threshold = threshold or default
    if threshold == "off":
        return False
    return SEVERITY_ORDER.get(event_severity, 0) >= SEVERITY_ORDER.get(threshold, 0)


def _in_quiet_hours(quiet_hours: str | None, now: datetime | None = None) -> bool:
    """quiet_hours format: 'HH-HH' (e.g. '22-08'). True if current time falls inside."""
    if not quiet_hours or "-" not in quiet_hours:
        return False
    try:
        start_h, end_h = (int(x) for x in quiet_hours.split("-"))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            return False
    except ValueError:
        return False
    now = (now or utcnow()).time()
    start = time(hour=start_h)
    end = time(hour=end_h)
    if start < end:
        return start <= now < end
    # Overnight (e.g. 22-08): quiet if now >= start OR now < end
    return now >= start or now < end


def _format_event_text(event: storage.AlertEvent) -> str:
    """Telegram MarkdownV2 message for one event."""
    sev_emoji = {
        "critical": "🔴",
        "warning": "⚠️",
        "info": "ℹ️",
        "opportunity": "💡",
    }.get(event.severity, "•")
    when = event.created_at.strftime("%d.%m %H:%M")
    title = (event.title or "")[:200]
    detail = (event.detail or "")[:300]
    out = f"{sev_emoji} *{event.severity.upper()}* `{when}`\n*{title}*"
    if detail:
        out += f"\n_{detail}_"
    return out


# По этой полосе слева строка-событие узнаётся в готовом письме.
_EVENT_ROW_MARK = "border-left:3px solid"


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _format_event_html(event: storage.AlertEvent) -> str:
    """HTML snippet for one event in email digest.

    Название и описание приходят с чужих сайтов (имя товара, заголовок промо),
    поэтому экранируются и обрезаются. На живых данных обрезка не срабатывает
    (самое длинное название — 139 знаков, описание — 134); она нужна, чтобы у
    размера письма был потолок.
    """
    color = {
        "critical": "#dc2626",
        "warning": "#f59e0b",
        "info": "#3b82f6",
        "opportunity": "#22c55e",
    }.get(event.severity, "#71717a")
    when = event.created_at.strftime("%d.%m.%Y %H:%M")
    title = escape(_clip(event.title, 200), quote=False)
    detail = escape(_clip(event.detail, 200), quote=False)
    return f"""
<tr>
  <td style="padding:10px;{_EVENT_ROW_MARK} {color};background:#fafafa;">
    <div style="font-size:11px;color:#71717a;text-transform:uppercase;font-weight:600;">
      {escape(event.severity or "", quote=False)} · {when}
    </div>
    <div style="font-weight:600;margin-top:4px;color:#18181b;">{title}</div>
    {f'<div style="font-size:13px;color:#52525b;margin-top:4px;">{detail}</div>' if detail else ""}
  </td>
</tr>
""".strip()


# ─── Real-time dispatch (called from alerts.evaluate_rules) ──────────────────


def dispatch_event(session: Session, event: storage.AlertEvent) -> dict[str, str]:
    """Route a single AlertEvent to all eligible channels.

    Returns dict {channel_name: status}, e.g. {'email': 'sent_3', 'telegram': 'queued_2'}.
    Updates event.channels_sent so we don't re-dispatch on retry.
    """
    if event.channels_sent and len(event.channels_sent) > 0:
        log.debug("dispatch_skipped_already_sent", event_id=event.id)
        return {"skipped": "already_sent"}

    tenant_id = getattr(event, "tenant_id", 1)
    users = session.scalars(
        select(storage.TenantUser).where(
            storage.TenantUser.tenant_id == tenant_id,
            storage.TenantUser.is_active.is_(True),
        )
    ).all()

    results: dict[str, list[str]] = {"email": [], "telegram": []}
    sent_at = utcnow()
    for user in users:
        # Email — skip real-time if user opted into daily digest (digest will include it)
        if not user.daily_digest and _severity_passes(
            user.email_severity_min, event.severity, DEFAULT_EMAIL_SEVERITY
        ):
            try:
                notifier.send_email(
                    subject=f"[{event.severity.upper()}] {event.title[:80]}",
                    html_body=_render_single_event_email(event),
                    to=[user.email],
                )
                results["email"].append(user.email)
            except Exception as e:
                log.warning(
                    "email_dispatch_failed", user_id=user.id, **notifier.delivery_error_fields(e)
                )

        # Telegram (respects quiet hours)
        if (
            user.telegram_chat_id
            and _severity_passes(
                user.telegram_severity_min, event.severity, DEFAULT_TELEGRAM_SEVERITY
            )
            and not _in_quiet_hours(user.quiet_hours)
        ):
            try:
                notifier.send_telegram_message(user.telegram_chat_id, _format_event_text(event))
                results["telegram"].append(user.telegram_chat_id)
            except Exception as e:
                log.warning(
                    "telegram_dispatch_failed",
                    user_id=user.id,
                    **notifier.delivery_error_fields(e),
                )

    # Mark as dispatched
    channels_used = [c for c, recipients in results.items() if recipients]
    event.channels_sent = channels_used
    session.commit()
    log.info(
        "alert_dispatched",
        event_id=event.id,
        severity=event.severity,
        email=len(results["email"]),
        telegram=len(results["telegram"]),
    )
    return {c: f"sent_{len(r)}" for c, r in results.items()}


def dispatch_events_batch(session: Session, events: list[storage.AlertEvent]) -> dict[str, int]:
    """Слить ВСЕ события одного прогона в ОДНО письмо-сводку на получателя.

    Замена циклу `for ev: dispatch_event(ev)` (одно письмо на событие → поток:
    переоценка линейки из 15 товаров = 15 писем). Теперь одно письмо со списком
    всех алертов прогона, шлётся сразу после прогона — немедленность сохранена,
    поток убран. Уважает те же per-user правила, что и `dispatch_event`:
    `daily_digest`-opt-out (real-time пропускается, событие попадёт в дайджест),
    email/telegram `severity_min`, quiet hours. Telegram — одним сообщением.

    `channels_sent` проставляется per-event (точная dedup-метка: канал отмечается
    только у событий, реально вошедших в отправленную сводку) и коммитится
    per-получатель — чтобы сбой commit не дал повторную рассылку всей пачки.

    Возвращает {'email': писем, 'telegram': сообщений}.
    """
    # Dedup: не трогаем уже отправленные (retry-safe, как dispatch_event).
    pending = [e for e in events if not (e.channels_sent and len(e.channels_sent) > 0)]
    if not pending:
        return {"email": 0, "telegram": 0}

    # Группировка по tenant_id — forward-looking. СЕЙЧАС no-op: evaluate_rules
    # создаёт AlertEvent без tenant_id → все события дефолтятся в tenant 1 (как и
    # dispatch_event, читающий event.tenant_id). Реальную мульти-тенант изоляцию
    # даст только стамп tenant_id из правила в evaluate_rules (отдельная задача).
    by_tenant: dict[int, list[storage.AlertEvent]] = {}
    for e in pending:
        by_tenant.setdefault(getattr(e, "tenant_id", 1) or 1, []).append(e)

    emails_sent = 0
    tg_sent = 0

    for tenant_id, tevents in by_tenant.items():
        users = session.scalars(
            select(storage.TenantUser).where(
                storage.TenantUser.tenant_id == tenant_id,
                storage.TenantUser.is_active.is_(True),
            )
        ).all()
        by_obj = {id(e): e for e in tevents}
        for user in users:
            sent_now: dict[int, set[str]] = {}

            # Email — пропускаем real-time если юзер на daily_digest (попадёт в дайджест)
            if not user.daily_digest:
                ev_email = [
                    e
                    for e in tevents
                    if _severity_passes(user.email_severity_min, e.severity, DEFAULT_EMAIL_SEVERITY)
                ]
                if ev_email:
                    try:
                        notifier.send_email(
                            subject=_batch_subject(ev_email),
                            html_body=_render_batch_email(ev_email),
                            to=[user.email],
                        )
                        emails_sent += 1
                        for e in ev_email:
                            sent_now.setdefault(id(e), set()).add("email")
                    except Exception as exc:
                        log.warning(
                            "email_batch_failed",
                            user_id=user.id,
                            **notifier.delivery_error_fields(exc),
                        )

            # Telegram — одним сообщением, уважает quiet hours
            if user.telegram_chat_id and not _in_quiet_hours(user.quiet_hours):
                ev_tg = [
                    e
                    for e in tevents
                    if _severity_passes(
                        user.telegram_severity_min, e.severity, DEFAULT_TELEGRAM_SEVERITY
                    )
                ]
                if ev_tg:
                    try:
                        notifier.send_telegram_message(
                            user.telegram_chat_id, _format_batch_text(ev_tg)
                        )
                        tg_sent += 1
                        for e in ev_tg:
                            sent_now.setdefault(id(e), set()).add("telegram")
                    except Exception as exc:
                        log.warning(
                            "telegram_batch_failed",
                            user_id=user.id,
                            **notifier.delivery_error_fields(exc),
                        )

            # Коммитим прогресс СРАЗУ после каждого получателя: успешно отправленное
            # помечаем channels_sent и фиксируем. Если commit упадёт (например,
            # transient DB/network fault), повторная рассылка ограничится ОДНИМ
            # получателем, а не
            # всей пачкой (единый end-of-batch commit мог бы продублировать всем).
            if sent_now:
                for eid, chans in sent_now.items():
                    ev = by_obj[eid]
                    ev.channels_sent = sorted(set(ev.channels_sent or []) | chans)
                session.commit()

    log.info(
        "alerts_dispatched_batch",
        events=len(pending),
        emails=emails_sent,
        telegram=tg_sent,
    )
    return {"email": emails_sent, "telegram": tg_sent}


# ─── События, которых нет в журнале: только администраторам ──────────────────

# Кому идут письма «только администраторам» — те же роли, что API считает
# администраторскими. Это роль, а не лично владелец: у сотрудника клиента роль
# должна быть viewer.
ADMIN_ROLES = ("admin", "owner")

_ADMIN_ONLY_TELEGRAM_NOTE = "Только администраторам: частичный сбор, в журнал алертов не попадает."


def mail_unstored_events_to_admins(
    session: Session, events: list[storage.AlertEvent], *, note: str
) -> dict[str, int]:
    """Одно письмо на администратора о событиях, которых нет в журнале алертов.

    Для событий частичного сбора (`alerts.local_price_alerts_for_partial_run`):
    клиенту они не показываются и в базе не хранятся. Отсюда отличия от
    `dispatch_events_batch`:

    - получатели — только активные пользователи с ролью из `ADMIN_ROLES`;
    - `daily_digest` письмо не отменяет: в дайджест такое событие не попадёт,
      его нет в базе;
    - `channels_sent` не ведётся и ничего не коммитится. Повторной отправки
      нет: что не ушло, то потеряно, и вызывающий узнаёт об этом из `failed`.

    Пороги важности и тихие часы действуют как обычно. `note` — пояснение под
    заголовком письма.

    Возвращает {'email': писем, 'telegram': сообщений, 'failed': сбоев}.
    """
    counts = {"email": 0, "telegram": 0, "failed": 0}
    by_tenant: dict[int, list[storage.AlertEvent]] = {}
    for e in events:
        by_tenant.setdefault(getattr(e, "tenant_id", 1) or 1, []).append(e)

    for tenant_id, tevents in by_tenant.items():
        admins = session.scalars(
            select(storage.TenantUser).where(
                storage.TenantUser.tenant_id == tenant_id,
                storage.TenantUser.is_active.is_(True),
                storage.TenantUser.role.in_(ADMIN_ROLES),
            )
        ).all()
        for user in admins:
            ev_email = [
                e
                for e in tevents
                if _severity_passes(user.email_severity_min, e.severity, DEFAULT_EMAIL_SEVERITY)
            ]
            if ev_email:
                try:
                    delivered = notifier.send_email(
                        subject=_batch_subject(ev_email),
                        html_body=_render_batch_email(ev_email, note=note),
                        to=[user.email],
                    )
                except Exception as exc:
                    delivered = False
                    log.warning(
                        "email_batch_failed",
                        user_id=user.id,
                        **notifier.delivery_error_fields(exc),
                    )
                counts["email" if delivered else "failed"] += 1

            if user.telegram_chat_id and not _in_quiet_hours(user.quiet_hours):
                ev_tg = [
                    e
                    for e in tevents
                    if _severity_passes(
                        user.telegram_severity_min, e.severity, DEFAULT_TELEGRAM_SEVERITY
                    )
                ]
                if ev_tg:
                    try:
                        delivered = notifier.send_telegram_message(
                            user.telegram_chat_id,
                            f"{_format_batch_text(ev_tg)}\n{_ADMIN_ONLY_TELEGRAM_NOTE}",
                        )
                    except Exception as exc:
                        delivered = False
                        log.warning(
                            "telegram_batch_failed",
                            user_id=user.id,
                            **notifier.delivery_error_fields(exc),
                        )
                    counts["telegram" if delivered else "failed"] += 1

    return counts


# ─── Telegram /start binding ─────────────────────────────────────────────────


def bind_telegram(session: Session, chat_id: str, email: str) -> bool:
    """Link a Telegram chat_id to an existing TenantUser by email.

    Called from telegram_bot.cmd_start when user does `/start <email>`.
    Returns True if bound, False if user not found.
    """
    user = session.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.email == email.lower().strip(),
            storage.TenantUser.is_active.is_(True),
        )
    )
    if not user:
        return False
    user.telegram_chat_id = str(chat_id)
    session.commit()
    log.info("telegram_bound", user_id=user.id)
    return True


# ─── Daily / weekly digest ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DigestOutcome:
    """Чем кончилась рассылка дайджеста."""

    # Получатели, отправка которым не упала; при dry_run — кому письмо ушло бы.
    sent: int = 0
    # Скольким письмо слали (или слали бы): 0 — слать было нечего или некому.
    recipients: int = 0
    # У скольких почтовый сервер письмо принял. При dry_run — ноль.
    accepted: int = 0
    # Что с отчётом о ценах — в записи `daily_letter_state`: "ok",
    # "skipped:<причина>", "failed:<класс ошибки>". У недельного дайджеста пусто.
    report: str = ""
    dry_run: bool = False
    report_failure: Exception | None = field(default=None, compare=False, repr=False)

    @property
    def no_recipients(self) -> bool:
        """Письмо некому слать: подписчиков нет или отбор (`only_…`) никого не нашёл."""
        return self.report == daily_letter_state.REPORT_NO_RECIPIENTS

    @property
    def problems(self) -> list[str]:
        """Почему рассылку нельзя считать состоявшейся; пусто — состоялась.

        Тексты печатает команда (и по ним она кончается ошибкой), поэтому в них
        класс ошибки и числа, без адресов и без текста ошибки.
        """
        found = []
        if self.report_failure is not None:
            error_type = type(self.report_failure).__name__
            if self.recipients:
                found.append(
                    f"отчёт о разнице цен не собрался ({error_type}) — письмо без чисел и без файла"
                )
            else:
                found.append(str(DailyLetterNotSent(error_type)))
        if not self.dry_run and self.recipients and not self.accepted:
            found.append(
                f"почтовый сервер не принял письмо ни у одного из {self.recipients} получателей "
                "(в журнале — digest_email_failed или email_skipped_no_smtp)"
            )
        return found


def send_daily_letter(
    session: Session,
    tenant_id: int = 1,
    *,
    only_email: str | None = None,
    only_user_id: int | None = None,
    dry_run: bool = False,
    locale: str = "ru",
) -> DigestOutcome:
    """Утреннее письмо тем, у кого включён суточный дайджест.

    Наверху — разница цен между сайтом клиента и конкурентами на сейчас
    (`_price_report`), во вложении — тот же Excel, что отдаёт кнопка на странице
    сравнения; ниже — события за сутки. `locale` — язык вложения: текст письма,
    как у всех писем системы, русский.

    Сломанный отчёт и отказ почтового сервера исключением не становятся: всё,
    что можно, отправлено, а что не так — в `DigestOutcome.problems`. Исход
    настоящей рассылки пишется в файл `daily_letter_state`: по нему health
    замечает утро, когда письмо не ушло. Не рассылка, и записи не оставляют:
    `dry_run` и письмо одному — `only_email` (из включивших дайджест) или
    `only_user_id` (пользователю арендатора по id, подписан он или нет; так
    шлёт кнопка «отправить тестовый дайджест» в дашборде).
    """
    outcome = _send_digest(
        session,
        tenant_id,
        kind="daily",
        since=utcnow() - timedelta(hours=24),
        only_email=only_email,
        only_user_id=only_user_id,
        dry_run=dry_run,
        locale=locale,
    )
    if not dry_run and only_email is None and only_user_id is None:
        daily_letter_state.record(
            tenant_id,
            at=utcnow(),
            recipients=outcome.recipients,
            accepted=outcome.accepted,
            report=outcome.report,
        )
    return outcome


def send_daily_digest(
    session: Session,
    tenant_id: int = 1,
    *,
    only_email: str | None = None,
    only_user_id: int | None = None,
    dry_run: bool = False,
    locale: str = "ru",
) -> int:
    """То же письмо, что `send_daily_letter`; возвращает число получателей.

    `DailyLetterNotSent` — письмо не ушло никому, потому что отчёт о ценах
    сломан, а событий за сутки нет: молча пропустить такое утро нельзя.
    """
    outcome = send_daily_letter(
        session,
        tenant_id,
        only_email=only_email,
        only_user_id=only_user_id,
        dry_run=dry_run,
        locale=locale,
    )
    if outcome.report_failure is not None and not outcome.recipients:
        failure = outcome.report_failure
        raise DailyLetterNotSent(type(failure).__name__) from failure
    return outcome.sent


def send_weekly_digest(
    session: Session, tenant_id: int = 1, *, only_email: str | None = None, dry_run: bool = False
) -> int:
    return _send_digest(
        session,
        tenant_id,
        kind="weekly",
        since=utcnow() - timedelta(days=7),
        only_email=only_email,
        dry_run=dry_run,
    ).sent


def _send_digest(
    session: Session,
    tenant_id: int,
    kind: str,
    since: datetime,
    *,
    only_email: str | None = None,
    only_user_id: int | None = None,
    dry_run: bool = False,
    locale: str = "ru",
) -> DigestOutcome:
    """Собрать и разослать дайджест. Возвращает исход рассылки.

    `only_email` — отправить одному получателю из включивших дайджест: так
    письмо смотрят на живых данных, не трогая остальных. `only_user_id` —
    одному активному пользователю арендатора по id, мимо галочки дайджеста.
    `dry_run` — собрать письма и записать в лог тему, размер и число строк,
    ничего не отправляя.

    Суточный дайджест (`kind="daily"`) несёт ещё отчёт о разнице цен и уходит и
    без событий, если отчёт есть. Недельный отчёта не знает и письмо шлёт
    прежнее.
    """
    if only_user_id is not None:
        subscribed = storage.TenantUser.id == only_user_id
    elif kind == "daily":
        subscribed = storage.TenantUser.daily_digest.is_(True)
    else:
        subscribed = storage.TenantUser.weekly_digest.is_(True)
    users = session.scalars(
        select(storage.TenantUser).where(
            storage.TenantUser.tenant_id == tenant_id,
            storage.TenantUser.is_active.is_(True),
            subscribed,
        )
    ).all()
    if only_email is not None:
        users = [u for u in users if u.email.lower() == only_email.strip().lower()]
    if not users:
        log.info("digest_no_recipients", kind=kind, tenant=tenant_id)
        return DigestOutcome(
            report=daily_letter_state.REPORT_NO_RECIPIENTS if kind == "daily" else "",
            dry_run=dry_run,
        )

    report: _PriceReport | None = None
    report_state = ""
    report_failure: Exception | None = None
    if kind == "daily":
        try:
            # Точка сохранения: упавший запрос отчёта не должен оставить
            # прерванной транзакцию, в которой ниже читаются события.
            with session.begin_nested():
                report, report_state = _price_report(session, tenant_id, locale=locale)
        except Exception as exc:
            # Письмо с событиями уйдёт и без отчёта. Адресов сборка отчёта не
            # читает; текст ошибки запроса к базе приходит уже без его данных.
            report_failure = exc
            report_state = daily_letter_state.report_failed(type(exc).__name__)
            log.exception(
                "digest_price_report_failed",
                kind=kind,
                tenant=tenant_id,
                error_type=type(exc).__name__,
            )

    events = session.scalars(
        select(storage.AlertEvent)
        .where(
            storage.AlertEvent.created_at >= since,
            storage.AlertEvent.tenant_id == tenant_id,
        )
        .order_by(desc(storage.AlertEvent.created_at))
    ).all()
    events = [event for event in events if _event_is_digest_eligible(session, event)]

    if not events and report is None:
        # Слать нечего. Тихое это утро или сломанный отчёт — видно по исходу.
        log.info("digest_no_events", kind=kind, tenant=tenant_id)
        return DigestOutcome(report=report_state, dry_run=dry_run, report_failure=report_failure)

    # Письмо одно на всех получателей. «Порог email-уведомлений» получателя
    # (`email_severity_min`) на дайджест не действует — решение владельца
    # 2026-10-07: размер держат потолки, а с порогом `warning` из почты совсем
    # пропали бы названия товаров из «Можно поднять цену».
    if report is None:
        html = _render_digest_email(events, kind=kind, since=since)
        subject = _digest_subject(events, kind)
        # Без отчёта письмо шлётся и пишется в журнал теми же аргументами, что раньше.
        with_file: dict = {}
        file_size: dict = {}
    else:
        html = _render_digest_email(events, kind=kind, since=since, price_report=report)
        subject = _price_letter_subject(report)
        with_file = {"attachments": [report.attachment]}
        file_size = {"attachment_kb": round(len(report.attachment[1]) / 1024, 1)}
    sent = 0
    accepted = 0
    for user in users:
        if dry_run:
            log.info(
                "digest_dry_run",
                kind=kind,
                user_id=user.id,
                subject=subject,
                kb=round(len(html.encode()) / 1024, 1),
                rows=html.count(_EVENT_ROW_MARK),
                **file_size,
            )
            sent += 1
            continue
        try:
            delivered = notifier.send_email(
                subject=subject, html_body=html, to=[user.email], **with_file
            )
            sent += 1
            # False — почтовый сервер не настроен: письмо никуда не ушло.
            accepted += delivered is True
        except Exception as e:
            log.warning(
                "digest_email_failed",
                user_id=user.id,
                kind=kind,
                **notifier.delivery_error_fields(e),
            )
    log.info(
        "digest_dry_run_done" if dry_run else "digest_sent",
        kind=kind,
        recipients=sent,
        events=len(events),
    )
    return DigestOutcome(
        sent=sent,
        recipients=len(users),
        accepted=accepted,
        report=report_state,
        dry_run=dry_run,
        report_failure=report_failure,
    )


_FINANCIAL_EVENT_TYPES = {
    "undercut_threshold",
    "price_drop_pct",
    "price_change_pct",
    "new_product",
    "promo_started",
    "price_raise_opportunity",
}


def _event_is_digest_eligible(session: Session, event: storage.AlertEvent) -> bool:
    """Fail closed for financial events created before verified run provenance."""
    if event.rule_type not in _FINANCIAL_EVENT_TYPES:
        return True
    run_id = _payload(event).get("source_run_id")
    if not isinstance(run_id, int):
        log.warning("digest_event_skipped_unverified", event_id=event.id, reason="no_source_run")
        return False
    run = session.get(storage.Run, run_id)
    eligible = bool(
        run is not None
        and run.tenant_id == event.tenant_id
        and (
            storage.run_is_financially_eligible(run)
            or (
                event.rule_type in {"price_drop_pct", "price_change_pct"}
                and storage.run_is_watchlist_price_alert_eligible(run)
            )
        )
    )
    if not eligible:
        log.warning(
            "digest_event_skipped_unverified",
            event_id=event.id,
            source_run_id=run_id,
            reason="run_not_financially_eligible",
        )
    return eligible


# ─── Утреннее письмо: разница цен ────────────────────────────────────────────
#
# Клиент попросил присылать «различающиеся данные» на почту каждый день сам.
# Суточный дайджест получил верх: сколько товаров в сравнении, у скольких цена
# на сайтах разная и в чью пользу, от какого числа данные каждого сайта — и тот
# же Excel, что отдаёт кнопка на странице сравнения. Списка «самых больших
# разниц» в письме нет намеренно: наверху по разнице чаще всего стоят пары, где
# сведены разные товары.


class DailyLetterNotSent(RuntimeError):
    """Утреннее письмо не ушло никому: отчёт о ценах сломан, событий за сутки нет.

    В тексте — только класс исходной ошибки: его печатают и команда, и ответ
    API администратору.
    """

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(
            f"отчёт о разнице цен не собрался ({error_type}), событий за сутки нет — "
            "письмо не отправлено"
        )


@dataclass(frozen=True)
class _SiteDataAge:
    site: str
    # Конец последнего полного сбора, которому система верит (UTC без пояса);
    # None — такого сбора у сайта ещё не было.
    collected_at: datetime | None
    # Сбор старше, чем позволяет ритм сайта (`cadence.site_max_age_hours`).
    overdue: bool


@dataclass(frozen=True)
class _PriceReport:
    generated_at: datetime
    client_site: str
    # Числа — `comparison_export.difference_counts`, определения там же.
    counts: dict[str, int]
    sites: tuple[_SiteDataAge, ...]
    # (имя файла, содержимое, тип) — как их ждёт `notifier.send_email`.
    attachment: tuple[str, bytes, str]


def _client_site(session: Session, tenant_id: int) -> str:
    """Сайт клиента из записи арендатора; незнакомое значение — сайт по умолчанию."""
    tenant = session.get(storage.Tenant, tenant_id)
    site = tenant.client_site if tenant is not None else None
    return site if site in comparison_export.SITES else comparison_export.CLIENT_SITE


def _site_data_ages(session: Session, tenant_id: int, now: datetime) -> tuple[_SiteDataAge, ...]:
    """Когда каждый сайт последний раз собран полностью сбором, которому система верит.

    Источник — `storage.latest_financial_run_ids_by_site`, тот же, по которому
    health поднимает `full_catalog_overdue`: последний полный сбор сайта,
    прошедший проверку каталога, сколько бы ему ни было. Возраст — от конца
    сбора. `product_policy._site_full_run_state` дешевле, но смотрит только на
    последнюю попытку: если она проверку не прошла, он не называет никакого
    сбора — а письму в этот день как раз и нужна дата последних годных данных.

    Данные «старые» (`overdue`) в двух случаях. Первый — порог health: сбор
    старше, чем позволяет ритм сайта с запасом (`cadence.site_max_age_hours`).
    Второй строже и нужен письму: в текущем окне ритма сайт не собран, хотя
    ночные будильники своё время уже отработали (от открытия окна прошёл запас
    `CADENCE_GRACE_HOURS`). При суточном ритме порог health — 30 часов, письмо
    уходит через 28 после вчерашнего сбора: без второго правила утро после
    пропущенной ночи выглядело бы в письме как обычное, только с вчерашней
    датой. Сбор относится к окну, в котором начался, — как у гварда в `run`.
    """
    run_ids = storage.latest_financial_run_ids_by_site(
        session, comparison_export.SITES, tenant_id=tenant_id
    )
    runs = (
        {
            run.id: run
            for run in session.scalars(
                select(storage.Run).where(storage.Run.id.in_(set(run_ids.values())))
            )
        }
        if run_ids
        else {}
    )
    ages = []
    for site in comparison_export.SITES:
        run = runs.get(run_ids.get(site))
        collected_at = (run.finished_at or run.started_at) if run is not None else None
        window_opened = cadence.cadence_window_start(site, now)
        night_is_over = now - window_opened >= timedelta(hours=cadence.CADENCE_GRACE_HOURS)
        overdue = (
            collected_at is None
            or now - collected_at > timedelta(hours=cadence.site_max_age_hours(site))
            or (night_is_over and run.started_at < window_opened)
        )
        ages.append(_SiteDataAge(site=site, collected_at=collected_at, overdue=overdue))
    return tuple(ages)


def _price_report(
    session: Session, tenant_id: int, *, locale: str
) -> tuple[_PriceReport | None, str]:
    """Отчёт «разница цен на сейчас» для утреннего письма и что с ним.

    Строки — те же, что показывает страница сравнения с фильтрами по умолчанию
    (минимум два сайта, достоверность пары от 0.70), файл — тот же, что отдаёт
    её кнопка «Excel». Числа считаются из строк, ушедших в файл, по правилам
    файла (`comparison_export.difference_counts`): «различаются» в письме — это
    число строк первого листа.

    Разбивка «в чью пользу» считается для сайта клиента из записи арендатора.
    Колонка «Позиция» в файле написана про Pharmonline: у арендатора с другим
    сайтом клиента разбивка письма с ней не совпадёт.

    Отчёта нет (None) — рядом причина, она же в журнале
    (`digest_price_report_skipped`): финансовые сравнения приостановлены
    политикой (страница в этом случае отвечает 503), успешного сбора ещё не
    было или сравнивать нечего. Исключение значит, что отчёт сломан; ловит его
    вызывающий.
    """
    eligibility = product_policy.policy_rollout_eligibility(session, tenant_id=tenant_id)
    if not eligibility.eligible:
        reason = eligibility.reason or "policy_not_ready"
    elif not comparison_rows.has_ok_run(session, tenant_id=tenant_id):
        reason = "no_successful_run"
    else:
        rows = comparison_rows.comparison_rows(session, tenant_id=tenant_id)
        if rows:
            now = utcnow()
            client_site = _client_site(session, tenant_id)
            report = _PriceReport(
                generated_at=now,
                client_site=client_site,
                counts=comparison_export.difference_counts(rows, client_site),
                sites=_site_data_ages(session, tenant_id, now),
                attachment=(
                    comparison_export.filename(locale, now),
                    comparison_export.build_workbook(rows, locale=locale, generated_at=now),
                    comparison_export.XLSX_MEDIA_TYPE,
                ),
            )
            log.info(
                "digest_price_report",
                tenant=tenant_id,
                locale=locale,
                attachment_kb=round(len(report.attachment[1]) / 1024, 1),
                overdue_sites=[age.site for age in report.sites if age.overdue],
                **report.counts,
            )
            return report, daily_letter_state.REPORT_OK
        reason = "no_comparison_rows"
    log.info("digest_price_report_skipped", tenant=tenant_id, reason=reason)
    return None, daily_letter_state.report_skipped(reason)


def _price_letter_title(report: _PriceReport) -> str:
    return f"Разница цен на {comparison_export.baku_time(report.generated_at):%d.%m.%Y}"


def _price_letter_subject(report: _PriceReport) -> str:
    day = comparison_export.baku_time(report.generated_at)
    differing = report.counts["differing"]
    found = (
        f"различаются у {_num(differing)} {_plural(differing, 'товара', 'товаров', 'товаров')}"
        if differing
        else "различий нет"
    )
    return f"Pharmacy Monitor — цены на {day:%d.%m.%Y}: {found}"


# «По проверенным сборам», а не «событий не было»: события сбора, не прошедшего
# проверку, дайджест отбрасывает (`digest_event_skipped_unverified`), и письмо
# без них не должно утверждать, что ничего не происходило.
_NO_EVENTS_LINE = "За последние 24 часа событий по проверенным сборам нет."


def _price_count_row(label: str, value: int, *, part: bool = False, strong: bool = False) -> str:
    """Строка «подпись — число»; `part` — слагаемое строки над ней."""
    label_style = (
        "padding:3px 0 3px 16px;font-size:13px;color:#52525b;"
        if part
        else f"padding:6px 0;color:#18181b;{'font-weight:600;' if strong else ''}"
    )
    value_style = (
        "font-size:13px;color:#52525b;"
        if part
        else f"font-size:{'18' if strong else '15'}px;font-weight:600;color:#18181b;"
    )
    return (
        f'<tr><td style="{label_style}">{label}</td>'
        f'<td style="text-align:right;white-space:nowrap;{value_style}">{_num(value)}</td></tr>'
    )


def _render_price_report(report: _PriceReport) -> str:
    """Верх утреннего письма: числа о разнице цен, возраст данных сайтов, что в файле."""
    titles = comparison_export.SITE_TITLES
    client = titles[report.client_site]
    rivals = " и ".join(titles[s] for s in comparison_export.SITES if s != report.client_site)
    counts = report.counts

    count_rows = [
        _price_count_row("Товаров в сравнении", counts["compared"]),
        _price_count_row("Цена на сайтах различается", counts["differing"], strong=True),
        _price_count_row(
            f"у конкурента дешевле, чем на {client}", counts["competitor_cheaper"], part=True
        ),
        _price_count_row(f"на {client} цена самая низкая", counts["client_cheapest"], part=True),
        _price_count_row(
            f"цена {client} в сравнение не вошла — различаются цены конкурентов",
            counts["no_client_price"],
            part=True,
        ),
        _price_count_row("Цена одинаковая", counts["same_price"]),
    ]
    if counts["not_comparable"]:
        count_rows.append(
            _price_count_row(
                "Сравнить не с чем: свежая цена меньше чем на двух сайтах",
                counts["not_comparable"],
            )
        )

    age_rows = []
    warnings = []
    for age in report.sites:
        site = titles[age.site]
        if age.collected_at is None:
            when = "—"
            warnings.append(f"{site}: полного сбора, прошедшего проверку, ещё не было.")
        else:
            collected = comparison_export.baku_time(age.collected_at)
            when = f"{collected:%d.%m.%Y %H:%M}"
            if age.overdue:
                warnings.append(
                    f"Данные {site} — от {collected:%d.%m.%Y}: с тех пор полный сбор этого "
                    "сайта не прошёл, его цены могут быть устаревшими."
                )
        age_rows.append(
            f'<tr><td style="padding:2px 0;">{site}</td>'
            f'<td style="text-align:right;white-space:nowrap;">{when}</td></tr>'
        )
    warning_rows = "".join(
        f"""
    <tr><td style="padding:10px 24px;background:#fffbeb;border-top:1px solid #e4e4e7;font-size:13px;color:#52525b;">
      {escape(text, quote=False)}
    </td></tr>"""
        for text in warnings
    )
    public = os.environ.get("PHARMACY_PUBLIC_URL", "")
    file_name = escape(report.attachment[0], quote=False)
    return f"""<tr><td style="padding:24px 24px 8px;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:22px;font-weight:600;color:#18181b;">{_price_letter_title(report)}</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">{client} в сравнении с {rivals}</div>
    </td></tr>
    <tr><td style="padding:8px 24px 4px;">
      <table style="width:100%;border-collapse:collapse;">
        {chr(10).join(count_rows)}
      </table>
      <div style="font-size:12px;color:#71717a;margin-top:8px;">
        Сравниваются свежие цены. Цена сайта в сравнение не входит, если его товара в паре нет, если сайт не показывал товар больше {comparison_rows.STALE_AFTER_DAYS} дней или если цена отброшена как явная ошибка — в разы ниже или выше остальных.
      </div>
    </td></tr>
    <tr><td style="padding:12px 24px;border-top:1px solid #f4f4f5;font-size:13px;color:#52525b;">
      <div style="font-weight:600;color:#18181b;margin-bottom:4px;">Когда сайты собраны полностью (время бакинское)</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;color:#52525b;">
        {chr(10).join(age_rows)}
      </table>
    </td></tr>{warning_rows}
    <tr><td style="padding:12px 24px 16px;border-top:1px solid #f4f4f5;font-size:13px;color:#52525b;">
      Все товары — в приложенном файле {file_name}: на первом листе товары с разной ценой, на втором — всё сравнение.
      <a href="{escape(public)}/comparison?diff=1" style="color:#3b82f6;">Открыть в дашборде →</a>
    </td></tr>"""


# ─── Email templates ─────────────────────────────────────────────────────────


def _render_single_event_email(event: storage.AlertEvent) -> str:
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{escape(event.title or "", quote=False)}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    <tr><td style="padding:24px;border-bottom:1px solid #e4e4e7;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:20px;font-weight:600;color:#18181b;">{event.severity.upper()} alert</div>
    </td></tr>
    {_format_event_html(event)}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      Открой <a href="{os.environ.get("PHARMACY_PUBLIC_URL", "")}/alerts" style="color:#3b82f6;">дашборд</a>
      чтобы увидеть полный список и историю.
    </td></tr>
  </table>
</body>
</html>
""".strip()


# ─── Digest: сводка и ограниченные списки ────────────────────────────────────
#
# 2026-10-05 недельное письмо ушло на 1 229 строк: одна сверка каталога дала
# 1 094 события «новый товар», а письмо клало по строке на каждое событие за
# окно. Теперь наверху сводка по типам, ниже — списки с потолком. Всё, что не
# попало в строки, остаётся числом в сводке и ссылкой на дашборд.

# Строк на один тип события; сверх этого — только самые крупные.
_DIGEST_TYPE_CAP = 30
# Строк на всё письмо. Gmail сворачивает письмо после 102 КБ («Message clipped»),
# и конец со ссылкой на дашборд пропадает. 60 строк с самыми длинными названиями
# из боевых данных — 62 КБ, обычное письмо — 20–35 КБ. Это потолок по числу
# строк, а не по байтам: названия из одних `&` или эмодзи дали бы больше.
_DIGEST_ROW_BUDGET = 60

# Подписи типов; порядок — порядок в письме при равной важности.
_RULE_TYPE_LABELS = {
    "undercut_threshold": "Конкурент дешевле",
    "price_drop_pct": "Цена упала",
    "price_change_pct": "Цена изменилась",
    "promo_started": "Новые промо",
    "site_drop_smoke": "Сайт собран не полностью",
    "price_raise_opportunity": "Можно поднять цену",
    "new_product": "Новые товары",
}
_BUCKET_RANK = {"critical": 0, "warning": 1, "info": 2}
_BUCKET_WORDS = {
    "critical": ("критичное", "критичных", "критичных"),
    "warning": ("предупреждение", "предупреждения", "предупреждений"),
    "info": ("информационное", "информационных", "информационных"),
}
# Поля payload с размером события в процентах — по ним выбираются «самые крупные».
_MAGNITUDE_KEYS = ("diff_pct", "drop_pct", "change_pct", "gap_pct")


def _digest_bucket(severity: str | None) -> str:
    """critical и warning как есть; всё остальное (info, opportunity, …) — информационное."""
    return severity if severity in ("critical", "warning") else "info"


def _payload(event: storage.AlertEvent) -> dict:
    """payload — колонка JSON: там может оказаться и не словарь."""
    return event.payload if isinstance(event.payload, dict) else {}


def _event_magnitude(event: storage.AlertEvent) -> float:
    payload = _payload(event)
    for key in _MAGNITUDE_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return abs(float(value))
    return 0.0


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    return few if 2 <= n <= 4 else many


def _num(n: int) -> str:
    """1909 → «1 909» (неразрывный пробел, чтобы число не рвалось переносом)."""
    return f"{n:,}".replace(",", "\u00a0")


def _bucket_counts(events: Iterable[storage.AlertEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        bucket = _digest_bucket(e.severity)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _severity_phrase(counts: dict[str, int]) -> str:
    """{'critical': 58, 'warning': 15} → «58 критичных, 15 предупреждений»."""
    return ", ".join(
        f"{_num(counts[b])} {_plural(counts[b], *_BUCKET_WORDS[b])}"
        for b in _BUCKET_RANK
        if counts.get(b)
    )


def _site_phrase(events: Iterable[storage.AlertEvent]) -> str:
    """«aloe 1 900, pharmonline 9» — пусто, если у событий типа нет сайта."""
    counts: dict[str, int] = {}
    for e in events:
        site = _payload(e).get("site")
        if isinstance(site, str) and site:
            counts[site] = counts.get(site, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{escape(site, quote=False)} {_num(n)}" for site, n in ordered)


def _digest_groups(
    events: list[storage.AlertEvent],
) -> list[tuple[str, list[storage.AlertEvent]]]:
    """События по типам: сначала типы с критичными, затем с предупреждениями."""
    by_type: dict[str, list[storage.AlertEvent]] = {}
    for e in events:
        by_type.setdefault(e.rule_type or "other", []).append(e)
    order = list(_RULE_TYPE_LABELS)

    def position(item: tuple[str, list[storage.AlertEvent]]) -> tuple[int, int, str]:
        rule_type, group = item
        worst = min(_BUCKET_RANK[_digest_bucket(e.severity)] for e in group)
        return (worst, order.index(rule_type) if rule_type in order else len(order), rule_type)

    return sorted(by_type.items(), key=position)


def _digest_selection(
    groups: list[tuple[str, list[storage.AlertEvent]]],
) -> dict[str, list[storage.AlertEvent]]:
    """Какие события идут в письмо строками, по типам.

    Строки раздаются по важности: сначала критичные всех типов, потом
    предупреждения, потом информационные — иначе предупреждения одного типа
    вытеснили бы критичные другого. Внутри одной важности остаток делится между
    типами поровну (типу не дают больше, чем у него есть), так что сотня падений
    цены не оставит без строк пять «конкурент дешевле». Внутри типа идут самые
    крупные по проценту.

    У информационных событий остаток не делят, а отдают типам по порядку. Тип,
    который помещается (и в потолок типа, и в остаток письма), идёт целиком.
    Который не помещается — зависит от того, есть ли у его событий размер в
    процентах: «можно поднять цену» показывает самые крупные, сколько влезает, а
    «новые товары» остаются числом — тридцать случайных из двух тысяч ничего не
    сообщают. Обрезанный список имеет смысл, только когда в нём главное.
    """
    chosen: dict[str, list[storage.AlertEvent]] = {rule_type: [] for rule_type, _ in groups}
    budget = _DIGEST_ROW_BUDGET

    def largest_first(group: list[storage.AlertEvent], bucket: str) -> list[storage.AlertEvent]:
        # Сортировка устойчивая: при равном размере остаётся порядок «новые сверху».
        return sorted(
            (e for e in group if _digest_bucket(e.severity) == bucket),
            key=lambda e: -_event_magnitude(e),
        )

    for bucket in ("critical", "warning"):
        wanted: dict[str, list[storage.AlertEvent]] = {}
        for rule_type, group in groups:
            room = max(0, _DIGEST_TYPE_CAP - len(chosen[rule_type]))
            events = largest_first(group, bucket)[:room]
            if events:
                wanted[rule_type] = events
        # Сначала типы, которым нужно меньше: их недобор достаётся остальным.
        # Доля округляется вверх, иначе при остатке меньше числа типов самый
        # малый тип не получил бы ничего.
        for i, rule_type in enumerate(sorted(wanted, key=lambda t: len(wanted[t]))):
            share = -(-budget // (len(wanted) - i))
            taken = wanted[rule_type][:share]
            chosen[rule_type].extend(taken)
            budget -= len(taken)

    for rule_type, group in groups:
        events = largest_first(group, "info")
        room = max(0, min(_DIGEST_TYPE_CAP - len(chosen[rule_type]), budget))
        if len(events) > room:
            if not any(_event_magnitude(e) for e in events):
                continue
            events = events[:room]
        chosen[rule_type].extend(events)
        budget -= len(events)
    return chosen


def _alerts_url(kind: str, **filters: str) -> str:
    """Ссылка на страницу алертов с тем же окном, что у дайджеста."""
    public = os.environ.get("PHARMACY_PUBLIC_URL", "")
    params = {"hours": "24" if kind == "daily" else "168", **filters}
    return escape(f"{public}/alerts?{urlencode(params)}")


def _digest_period_name(kind: str) -> str:
    return "сутки" if kind == "daily" else "неделю"


def _digest_subject(events: list[storage.AlertEvent], kind: str) -> str:
    total = len(events)
    subject = (
        f"Pharmacy Monitor — дайджест за {_digest_period_name(kind)}: "
        f"{_num(total)} {_plural(total, 'событие', 'события', 'событий')}"
    )
    critical = _bucket_counts(events).get("critical", 0)
    if critical:
        subject += f", из них {_num(critical)} {_plural(critical, *_BUCKET_WORDS['critical'])}"
    return subject


def _digest_summary_row(rule_type: str, group: list[storage.AlertEvent], kind: str) -> str:
    """Строка сводки: тип, сколько событий, разбивка по важности и по сайтам."""
    counts = _bucket_counts(group)
    facts = []
    if counts.get("critical") or counts.get("warning"):
        facts.append(_severity_phrase(counts))
    sites = _site_phrase(group)
    if sites:
        facts.append(sites)
    label = escape(_RULE_TYPE_LABELS.get(rule_type, rule_type), quote=False)
    facts_html = (
        f'<div style="font-size:12px;color:#71717a;margin-top:2px;">{" · ".join(facts)}</div>'
        if facts
        else ""
    )
    return f"""
<tr>
  <td style="padding:8px 24px;border-top:1px solid #f4f4f5;">
    <table style="width:100%;border-collapse:collapse;"><tr>
      <td>
        <a href="{_alerts_url(kind, type=rule_type)}" style="color:#18181b;font-weight:600;text-decoration:none;">{label}</a>
        {facts_html}
      </td>
      <td style="text-align:right;vertical-align:top;font-size:18px;font-weight:600;color:#18181b;white-space:nowrap;">{_num(len(group))}</td>
    </tr></table>
  </td>
</tr>
""".strip()


def _digest_block(
    rule_type: str,
    group: list[storage.AlertEvent],
    rows: list[storage.AlertEvent],
    kind: str,
) -> str:
    """Список событий одного типа с заголовком и ссылкой на остаток."""
    label = escape(_RULE_TYPE_LABELS.get(rule_type, rule_type), quote=False)
    rest = len(group) - len(rows)
    count = f"{_num(len(rows))} из {_num(len(group))}" if rest else _num(len(group))
    more = (
        f"""
<tr><td style="padding:8px 24px 4px;font-size:12px;">
  <a href="{_alerts_url(kind, type=rule_type)}" style="color:#3b82f6;">Ещё {_num(rest)} — в дашборде →</a>
</td></tr>"""
        if rest
        else ""
    )
    return f"""
<tr><td style="padding:18px 24px 8px;border-top:1px solid #e4e4e7;font-size:14px;font-weight:600;color:#18181b;">
  {label} <span style="font-weight:400;color:#71717a;">· {count}</span>
</td></tr>
{chr(10).join(_format_event_html(e) for e in rows)}{more}
""".strip()


def _render_digest_email(
    events: Iterable[storage.AlertEvent],
    kind: str,
    since: datetime,
    *,
    price_report: _PriceReport | None = None,
) -> str:
    """Письмо-дайджест: сводка по типам, затем списки с потолком.

    С `price_report` письмо начинается отчётом о разнице цен, а события идут под
    ним; если событий нет, вместо их сводки — одна строка об этом.
    """
    events = list(events)
    total = len(events)

    groups = _digest_groups(events)
    chosen = _digest_selection(groups)
    summary_rows = [_digest_summary_row(rule_type, group, kind) for rule_type, group in groups]
    blocks = [
        _digest_block(rule_type, group, chosen[rule_type], kind)
        for rule_type, group in groups
        if chosen[rule_type]
    ]
    listed = sum(len(rows) for rows in chosen.values())

    if listed == total:
        scope_note = ""
    elif listed:
        scope_note = (
            f"Ниже по строке на событие — {_num(listed)} из {_num(total)}, самые важные. "
            "Остальное — числами в сводке и в дашборде."
        )
    else:
        scope_note = "В этом письме только сводка. Сами события — в дашборде."
    period_name = _digest_period_name(kind)
    period = f"{since.strftime('%d.%m')} – {utcnow().strftime('%d.%m')}"
    totals = f"{_num(total)} {_plural(total, 'событие', 'события', 'событий')}"
    if total:
        totals += f": {_severity_phrase(_bucket_counts(events))}"
    scope_html = (
        f'<tr><td style="padding:12px 24px 16px;font-size:12px;color:#71717a;">{scope_note}</td></tr>'
        if scope_note
        else ""
    )
    title = f"Дайджест за {period_name}"
    if price_report is None:
        header = f"""<tr><td style="padding:24px;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:22px;font-weight:600;color:#18181b;">Дайджест за {period_name}</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">{period} · {totals}</div>
    </td></tr>"""
    elif events:
        title = _price_letter_title(price_report)
        header = f"""{_render_price_report(price_report)}
    <tr><td style="padding:20px 24px 12px;border-top:1px solid #e4e4e7;">
      <div style="font-size:18px;font-weight:600;color:#18181b;">События за {period_name}</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">{period} · {totals}</div>
    </td></tr>"""
    else:
        title = _price_letter_title(price_report)
        header = f"""{_render_price_report(price_report)}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:13px;color:#52525b;">
      {_NO_EVENTS_LINE}
    </td></tr>"""
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    {header}
    {chr(10).join(summary_rows)}
    {scope_html}
    {chr(10).join(blocks)}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      <a href="{_alerts_url(kind)}" style="color:#3b82f6;">Все события за {period_name} — в дашборде →</a>
      <br><br>
      Отписаться от дайджеста: администратор отключает его в дашборде, Настройки → Пользователи.
    </td></tr>
  </table>
</body>
</html>
""".strip()


# ─── Per-run batch (consolidated single email) ───────────────────────────────


def _batch_subject(events: list[storage.AlertEvent]) -> str:
    """Тема batch-письма. 1 событие → старый single-style (без регресса для
    одиночных алертов); >1 → сводка с худшей severity и количеством."""
    if len(events) == 1:
        e = events[0]
        return f"[{e.severity.upper()}] {(e.title or '')[:80]}"
    worst = max(events, key=lambda e: SEVERITY_ORDER.get(e.severity, 0)).severity
    return f"[{worst.upper()}] Pharmacy Monitor — {len(events)} алертов"


def _render_batch_email(events: list[storage.AlertEvent], *, note: str | None = None) -> str:
    """ОДНО письмо со списком всех событий прогона (critical → warning → info)."""
    note_row = (
        '<tr><td style="padding:12px 24px;background:#fffbeb;border-bottom:1px solid #e4e4e7;'
        f'font-size:13px;color:#52525b;">{escape(note, quote=False)}</td></tr>'
        if note
        else ""
    )
    ordered = sorted(events, key=lambda e: -SEVERITY_ORDER.get(e.severity, 0))
    rows = "\n".join(_format_event_html(e) for e in ordered)
    public = os.environ.get("PHARMACY_PUBLIC_URL", "")
    sev_counts: dict[str, int] = {}
    for e in events:
        sev_counts[e.severity] = sev_counts.get(e.severity, 0) + 1
    summary = " · ".join(
        f"{v} {k}"
        for k, v in sorted(sev_counts.items(), key=lambda kv: -SEVERITY_ORDER.get(kv[0], 0))
    )
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{len(events)} alerts</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    <tr><td style="padding:24px;border-bottom:1px solid #e4e4e7;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:22px;font-weight:600;color:#18181b;">{len(events)} алертов за прогон</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">{summary}</div>
    </td></tr>
    {note_row}
    {rows}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      <a href="{public}/alerts" style="color:#3b82f6;">Открыть в дашборде →</a>
    </td></tr>
  </table>
</body>
</html>
""".strip()


def _format_batch_text(events: list[storage.AlertEvent]) -> str:
    """Telegram: одно сообщение со списком (cap 30 строк)."""
    ordered = sorted(events, key=lambda e: -SEVERITY_ORDER.get(e.severity, 0))
    emoji = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️", "opportunity": "💡"}
    lines = [f"*Pharmacy Monitor — {len(events)} алертов*"]
    for e in ordered[:30]:
        lines.append(f"{emoji.get(e.severity, '•')} {(e.title or '')[:120]}")
    if len(ordered) > 30:
        lines.append(f"…и ещё {len(ordered) - 30}")
    return "\n".join(lines)
