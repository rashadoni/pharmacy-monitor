"""Notification dispatch — centralized routing of alerts to email + telegram.

Replaces the scattered logic in src/alerts.py:_send_*_alert. New logic respects:
  - Per-tenant_user severity_min thresholds (info/warning/critical/off)
  - Quiet hours (don't ping telegram at 3 AM)
  - Daily / weekly digest opt-in (events queued, sent in batch)

Public API:
  - dispatch_event(session, event)        — for real-time alerts (called by alerts.evaluate_rules)
  - send_daily_digest(session, tenant_id) — call from systemd timer at 08:00
  - send_weekly_digest(session, tenant_id) — call from systemd timer Mondays
  - bind_telegram(session, chat_id, email) — link telegram to user

Severity ordering (low → high):  info < warning < critical
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from html import escape
from typing import Iterable
from urllib.parse import urlencode

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src import notifier, storage
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
                log.warning("email_dispatch_failed", user=user.email, error=str(e))

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
                log.warning("telegram_dispatch_failed", user=user.email, error=str(e))

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

    «Вошедшее» — событие, прошедшее порог получателя, чьё письмо ушло: и то,
    что показано строкой, и то, что из-за потолка осталось числом в сводке со
    ссылкой на дашборд. Метка отвечает на вопрос «слать ли ещё раз», а не
    «видел ли получатель название»: без неё повторный вызов на тех же событиях
    слал бы остаток письмо за письмом — тот самый поток, который убрали. Так же
    с первого дня ведёт себя Telegram (30 строк и «…и ещё N», помечены все).

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
                        log.warning("email_batch_failed", user=user.email, error=str(exc))

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
                        log.warning("telegram_batch_failed", user=user.email, error=str(exc))

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
    log.info("telegram_bound", user=email, chat_id=chat_id)
    return True


# ─── Daily / weekly digest ───────────────────────────────────────────────────


def send_daily_digest(
    session: Session, tenant_id: int = 1, *, only_email: str | None = None, dry_run: bool = False
) -> int:
    """Send daily digest to opted-in users. Returns count of emails sent."""
    return _send_digest(
        session,
        tenant_id,
        kind="daily",
        since=utcnow() - timedelta(hours=24),
        only_email=only_email,
        dry_run=dry_run,
    )


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
    )


def _send_digest(
    session: Session,
    tenant_id: int,
    kind: str,
    since: datetime,
    *,
    only_email: str | None = None,
    dry_run: bool = False,
) -> int:
    """Собрать и разослать дайджест. Возвращает число получателей.

    `only_email` — отправить одному получателю из включивших дайджест: так
    письмо смотрят на живых данных, не трогая остальных. `dry_run` — собрать
    письма и записать в лог тему, размер и число строк, ничего не отправляя.
    """
    users = session.scalars(
        select(storage.TenantUser).where(
            storage.TenantUser.tenant_id == tenant_id,
            storage.TenantUser.is_active.is_(True),
            storage.TenantUser.daily_digest.is_(True)
            if kind == "daily"
            else storage.TenantUser.weekly_digest.is_(True),
        )
    ).all()
    if only_email is not None:
        users = [u for u in users if u.email.lower() == only_email.strip().lower()]
    if not users:
        log.info("digest_no_recipients", kind=kind, tenant=tenant_id)
        return 0

    events = session.scalars(
        select(storage.AlertEvent)
        .where(
            storage.AlertEvent.created_at >= since,
            storage.AlertEvent.tenant_id == tenant_id,
        )
        .order_by(desc(storage.AlertEvent.created_at))
    ).all()
    events = [event for event in events if _event_is_digest_eligible(session, event)]

    if not events:
        log.info("digest_no_events", kind=kind, tenant=tenant_id)
        return 0

    # Письмо одно на всех получателей. «Порог email-уведомлений» получателя
    # (`email_severity_min`) на дайджест не действует — решение владельца
    # 2026-10-07: размер держат потолки, а с порогом `warning` из почты совсем
    # пропали бы названия товаров из «Можно поднять цену».
    html = _render_digest_email(events, kind=kind, since=since)
    subject = _digest_subject(events, kind)
    sent = 0
    for user in users:
        if dry_run:
            log.info(
                "digest_dry_run",
                kind=kind,
                user=user.email,
                subject=subject,
                kb=round(len(html.encode()) / 1024, 1),
                rows=html.count(_EVENT_ROW_MARK),
            )
            sent += 1
            continue
        try:
            notifier.send_email(subject=subject, html_body=html, to=[user.email])
            sent += 1
        except Exception as e:
            log.warning("digest_email_failed", user=user.email, kind=kind, error=str(e))
    log.info(
        "digest_dry_run_done" if dry_run else "digest_sent",
        kind=kind,
        recipients=sent,
        events=len(events),
    )
    return sent


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

    Информационный тип больше потолка идёт только числом: тридцать случайных
    «новых товаров» из двух тысяч ничего не сообщают. Остальные получают остаток
    по порядку, целиком. Если типу не хватает места в письме, он идёт числом —
    кроме типа, у событий которого есть размер в процентах («можно поднять
    цену»): у него показываются самые крупные, сколько помещается. Обрезанный
    список имеет смысл, только когда понятно, что в него попало главное.
    """
    chosen: dict[str, list[storage.AlertEvent]] = {rule_type: [] for rule_type, _ in groups}
    budget = _DIGEST_ROW_BUDGET
    for bucket in _BUCKET_RANK:
        wanted: dict[str, list[storage.AlertEvent]] = {}
        for rule_type, group in groups:
            events = [e for e in group if _digest_bucket(e.severity) == bucket]
            room = _DIGEST_TYPE_CAP - len(chosen[rule_type])
            if not events or room <= 0 or (bucket == "info" and len(events) > room):
                continue
            # Сортировка устойчивая: при равном размере остаётся порядок «новые сверху».
            events.sort(key=lambda e: -_event_magnitude(e))
            wanted[rule_type] = events[:room]
        if bucket == "info":
            for rule_type, events in wanted.items():
                if len(events) > budget:
                    if not any(_event_magnitude(e) for e in events):
                        continue
                    events = events[:budget]
                chosen[rule_type].extend(events)
                budget -= len(events)
            continue
        # Сначала типы, которым нужно меньше: их недобор достаётся остальным.
        # Доля округляется вверх, иначе при остатке меньше числа типов самый
        # малый тип не получил бы ничего.
        for i, rule_type in enumerate(sorted(wanted, key=lambda t: len(wanted[t]))):
            share = -(-budget // (len(wanted) - i))
            taken = wanted[rule_type][:share]
            chosen[rule_type].extend(taken)
            budget -= len(taken)
    return chosen


# Окно страницы алертов в часах, по виду письма. По номеру прогона страница не
# отбирает, поэтому письмо о прогоне ведёт на последние сутки.
_ALERTS_WINDOW_HOURS = {"daily": 24, "weekly": 168, "run": 24}


def _alerts_url(kind: str, **filters: str) -> str:
    """Ссылка на страницу алертов с тем же окном, что у письма."""
    public = os.environ.get("PHARMACY_PUBLIC_URL", "")
    params = {"hours": str(_ALERTS_WINDOW_HOURS[kind]), **filters}
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


def _render_summary_email(
    events: list[storage.AlertEvent], *, kind: str, heading: str, subheading: str, footer: str
) -> str:
    """Письмо со сводкой по типам и списками с потолком.

    Общее тело дайджеста и письма о прогоне: у них разные шапка, подвал и окно
    ссылок на дашборд (`kind`), а сводка, отбор строк и потолки — одни.
    """
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
    scope_html = (
        f'<tr><td style="padding:12px 24px 16px;font-size:12px;color:#71717a;">{scope_note}</td></tr>'
        if scope_note
        else ""
    )
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{heading}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    <tr><td style="padding:24px;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:22px;font-weight:600;color:#18181b;">{heading}</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">{subheading}</div>
    </td></tr>
    {chr(10).join(summary_rows)}
    {scope_html}
    {chr(10).join(blocks)}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      {footer}
    </td></tr>
  </table>
</body>
</html>
""".strip()


def _render_digest_email(events: Iterable[storage.AlertEvent], kind: str, since: datetime) -> str:
    """Письмо-дайджест: сводка по типам, затем списки с потолком."""
    events = list(events)
    total = len(events)
    period_name = _digest_period_name(kind)
    period = f"{since.strftime('%d.%m')} – {utcnow().strftime('%d.%m')}"
    totals = f"{_num(total)} {_plural(total, 'событие', 'события', 'событий')}"
    if total:
        totals += f": {_severity_phrase(_bucket_counts(events))}"
    return _render_summary_email(
        events,
        kind=kind,
        heading=f"Дайджест за {period_name}",
        subheading=f"{period} · {totals}",
        footer=f"""<a href="{_alerts_url(kind)}" style="color:#3b82f6;">Все события за {period_name} — в дашборде →</a>
      <br><br>
      Отписаться от дайджеста: администратор отключает его в дашборде, Настройки → Пользователи.""",
    )


# ─── Per-run batch (consolidated single email) ───────────────────────────────
#
# Письмо о прогоне собирает то же тело, что дайджест. До 2026-10-07 оно клало
# строку на каждое событие прогона без потолка: прогон 973 дал письмо на 99
# строк, а получатель с порогом `info` после сверки каталога получил бы около
# двух тысяч. Отличия от дайджеста — не в письме, а до него: сюда приходят
# только события, прошедшие порог получателя (`dispatch_events_batch`).


def _alert_count(n: int) -> str:
    return f"{_num(n)} {_plural(n, 'алерт', 'алерта', 'алертов')}"


def _batch_subject(events: list[storage.AlertEvent]) -> str:
    """Тема batch-письма. 1 событие → старый single-style (без регресса для
    одиночных алертов); >1 → сводка с худшей severity и количеством."""
    if len(events) == 1:
        e = events[0]
        return f"[{e.severity.upper()}] {(e.title or '')[:80]}"
    worst = max(events, key=lambda e: SEVERITY_ORDER.get(e.severity, 0)).severity
    return f"[{worst.upper()}] Pharmacy Monitor — {_alert_count(len(events))}"


def _render_batch_email(events: list[storage.AlertEvent]) -> str:
    """ОДНО письмо о событиях прогона: сводка по типам, затем списки с потолком."""
    events = list(events)
    return _render_summary_email(
        events,
        kind="run",
        heading=f"{_alert_count(len(events))} за прогон",
        subheading=_severity_phrase(_bucket_counts(events)),
        footer=f'<a href="{_alerts_url("run")}" style="color:#3b82f6;">Открыть в дашборде →</a>',
    )


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
