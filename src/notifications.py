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
from typing import Iterable

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


def _format_event_html(event: storage.AlertEvent) -> str:
    """HTML snippet for one event in email digest."""
    color = {
        "critical": "#dc2626",
        "warning": "#f59e0b",
        "info": "#3b82f6",
        "opportunity": "#22c55e",
    }.get(event.severity, "#71717a")
    when = event.created_at.strftime("%d.%m.%Y %H:%M")
    return f"""
<tr>
  <td style="padding:10px;border-left:3px solid {color};background:#fafafa;">
    <div style="font-size:11px;color:#71717a;text-transform:uppercase;font-weight:600;">
      {event.severity} · {when}
    </div>
    <div style="font-weight:600;margin-top:4px;color:#18181b;">{event.title}</div>
    {f'<div style="font-size:13px;color:#52525b;margin-top:4px;">{event.detail}</div>' if event.detail else ''}
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
        if (
            not user.daily_digest
            and _severity_passes(user.email_severity_min, event.severity, DEFAULT_EMAIL_SEVERITY)
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
            and _severity_passes(user.telegram_severity_min, event.severity, DEFAULT_TELEGRAM_SEVERITY)
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


def send_daily_digest(session: Session, tenant_id: int = 1) -> int:
    """Send daily digest to opted-in users. Returns count of emails sent."""
    return _send_digest(session, tenant_id, kind="daily", since=utcnow() - timedelta(hours=24))


def send_weekly_digest(session: Session, tenant_id: int = 1) -> int:
    return _send_digest(session, tenant_id, kind="weekly", since=utcnow() - timedelta(days=7))


def _send_digest(session: Session, tenant_id: int, kind: str, since: datetime) -> int:
    users = session.scalars(
        select(storage.TenantUser).where(
            storage.TenantUser.tenant_id == tenant_id,
            storage.TenantUser.is_active.is_(True),
            storage.TenantUser.daily_digest.is_(True) if kind == "daily" else
            storage.TenantUser.weekly_digest.is_(True),
        )
    ).all()
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

    if not events:
        log.info("digest_no_events", kind=kind, tenant=tenant_id)
        return 0

    html = _render_digest_email(events, kind=kind, since=since)
    subject = f"Pharmacy Monitor — {kind} digest ({len(events)} events)"
    sent = 0
    for user in users:
        try:
            notifier.send_email(subject=subject, html_body=html, to=[user.email])
            sent += 1
        except Exception as e:
            log.warning("digest_email_failed", user=user.email, kind=kind, error=str(e))
    log.info("digest_sent", kind=kind, recipients=sent, events=len(events))
    return sent


# ─── Email templates ─────────────────────────────────────────────────────────


def _render_single_event_email(event: storage.AlertEvent) -> str:
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{event.title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    <tr><td style="padding:24px;border-bottom:1px solid #e4e4e7;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:20px;font-weight:600;color:#18181b;">{event.severity.upper()} alert</div>
    </td></tr>
    {_format_event_html(event)}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      Открой <a href="{os.environ.get('PHARMACY_PUBLIC_URL', '')}/alerts" style="color:#3b82f6;">дашборд</a>
      чтобы увидеть полный список и историю.
    </td></tr>
  </table>
</body>
</html>
""".strip()


def _render_digest_email(events: Iterable[storage.AlertEvent], kind: str, since: datetime) -> str:
    rows = "\n".join(_format_event_html(e) for e in events)
    title = "Daily" if kind == "daily" else "Weekly"
    period = since.strftime("%d.%m %H:%M")
    public = os.environ.get("PHARMACY_PUBLIC_URL", "")
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title} digest</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f5;padding:20px;">
  <table style="max-width:600px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;width:100%;border-collapse:collapse;">
    <tr><td style="padding:24px;border-bottom:1px solid #e4e4e7;">
      <div style="font-size:14px;color:#71717a;margin-bottom:4px;">Pharmacy Monitor</div>
      <div style="font-size:22px;font-weight:600;color:#18181b;">{title} digest</div>
      <div style="font-size:13px;color:#71717a;margin-top:4px;">События с {period}</div>
    </td></tr>
    {rows}
    <tr><td style="padding:16px 24px;border-top:1px solid #e4e4e7;font-size:12px;color:#71717a;">
      <a href="{public}/alerts" style="color:#3b82f6;">Открыть в дашборде →</a>
      <br><br>
      Чтобы отписаться от этого digest — настройки → notifications.
    </td></tr>
  </table>
</body>
</html>
""".strip()
