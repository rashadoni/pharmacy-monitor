"""Daily digest: одно письмо в день с топ-N алертами вместо flood'а.

Запускается cron-таймером в 09:00 Baku (05:00 UTC):
  pharmacy-monitor digest [--top N] [--dry-run]

Логика:
- Берёт все AlertEvent за последние 24ч (с момента последнего digest)
- Сортирует: critical → warning → info, внутри — по created_at desc
- Берёт топ MAX_EVENTS (дефолт 20)
- Отправляет один email на всех активных получателей
- Если 0 событий за 24ч — письмо не отправляется (silent ok)
"""

from __future__ import annotations

from datetime import timedelta
from src._time import utcnow

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src.storage import AlertEvent

log = structlog.get_logger()

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
SEV_EMOJI = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️"}
MAX_EVENTS = 20


def send_daily_digest(
    session: Session,
    *,
    window_hours: int = 24,
    top_n: int = MAX_EVENTS,
    dry_run: bool = False,
) -> int:
    """Отправить daily digest. Возвращает кол-во событий в письме (0 = не отправлено)."""
    cutoff = utcnow() - timedelta(hours=window_hours)

    events = session.scalars(
        select(AlertEvent)
        .where(AlertEvent.created_at >= cutoff)
        .order_by(desc(AlertEvent.created_at))
    ).all()

    if not events:
        log.info("digest_skipped", reason="no_events", window_hours=window_hours)
        return 0

    # Сортируем: critical первыми, потом warning, потом info
    events_sorted = sorted(
        events,
        key=lambda e: (SEV_ORDER.get(e.severity, 99), -e.id),
    )[:top_n]

    n_critical = sum(1 for e in events_sorted if e.severity == "critical")
    n_warning  = sum(1 for e in events_sorted if e.severity == "warning")
    n_info     = sum(1 for e in events_sorted if e.severity == "info")
    total_in_window = len(events)

    subject = _make_subject(n_critical, n_warning, n_info, total_in_window, top_n)
    html = _make_html(events_sorted, total_in_window, top_n)

    if dry_run:
        log.info("digest_dry_run", subject=subject, events=len(events_sorted))
        print(f"[DRY RUN] Subject: {subject}")
        print(f"[DRY RUN] Events: {len(events_sorted)} (of {total_in_window} in {window_hours}h)")
        for e in events_sorted[:5]:
            print(f"  {SEV_EMOJI.get(e.severity, '•')} [{e.severity}] {e.title}")
        return len(events_sorted)

    try:
        from src import notifier
        notifier.send_email(subject=subject, html_body=html)
        log.info("digest_sent", subject=subject, events=len(events_sorted), total=total_in_window)
    except Exception as exc:
        log.error("digest_email_failed", error=str(exc))
        return 0

    return len(events_sorted)


def _make_subject(n_crit: int, n_warn: int, n_info: int, total: int, top_n: int) -> str:
    parts = []
    if n_crit:
        parts.append(f"🔴 {n_crit} critical")
    if n_warn:
        parts.append(f"⚠️ {n_warn} warning")
    if n_info:
        parts.append(f"ℹ️ {n_info} info")
    summary = ", ".join(parts) if parts else "нет событий"
    suffix = f" (+{total - top_n} ещё)" if total > top_n else ""
    return f"[Pharmacy Monitor] {summary}{suffix}"


def _make_html(events: list[AlertEvent], total: int, top_n: int) -> str:
    rows_html = ""
    for e in events:
        emoji = SEV_EMOJI.get(e.severity, "•")
        sev_color = {"critical": "#ff3b30", "warning": "#ff9500", "info": "#007aff"}.get(
            e.severity, "#8e8e93"
        )
        ts = e.created_at.strftime("%H:%M") if e.created_at else ""
        rows_html += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #f2f2f7;vertical-align:top;width:36px">
            <span style="font-size:16px">{emoji}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #f2f2f7;vertical-align:top">
            <div style="font-size:14px;font-weight:600;color:#1c1c1e">{e.title}</div>
            <div style="font-size:12px;color:#636366;margin-top:3px;line-height:1.4">
              {e.detail or ""}
            </div>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #f2f2f7;vertical-align:top;
                     white-space:nowrap;font-size:11px;color:#8e8e93;text-align:right">
            <span style="background:{sev_color}15;color:{sev_color};
                         padding:2px 6px;border-radius:4px;font-weight:600;
                         font-size:10px;text-transform:uppercase">
              {e.severity}
            </span><br>
            <span style="margin-top:4px;display:inline-block">{ts}</span>
          </td>
        </tr>"""

    more_note = ""
    if total > top_n:
        more_note = f"""
        <p style="font-size:12px;color:#8e8e93;text-align:center;margin:16px 0 0">
          Показаны {top_n} из {total} событий за 24 часа.
          <a href="https://leaddrive.cloud/alerts" style="color:#007aff">
            Смотреть все →
          </a>
        </p>"""

    return f"""
    <div style="font-family:-apple-system,Segoe UI,Helvetica Neue,sans-serif;
                max-width:600px;margin:24px auto;background:#fff;
                border-radius:16px;border:1px solid #e5e5ea;overflow:hidden">
      <div style="background:#1c1c1e;padding:18px 24px">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;
                    color:#636366;font-weight:600">Pharmacy Monitor</div>
        <div style="font-size:20px;font-weight:700;color:#fff;margin-top:4px">
          Дайджест за сегодня
        </div>
        <div style="font-size:13px;color:#8e8e93;margin-top:2px">
          {total} событий за последние 24 часа
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse">
        {rows_html}
      </table>
      {more_note}
      <div style="padding:16px 24px;border-top:1px solid #f2f2f7;
                  font-size:11px;color:#8e8e93;text-align:center">
        <a href="https://leaddrive.cloud/alerts" style="color:#007aff;text-decoration:none">
          Открыть алерты
        </a>
        &nbsp;·&nbsp;
        <a href="https://leaddrive.cloud/settings" style="color:#007aff;text-decoration:none">
          Настроить уведомления
        </a>
      </div>
    </div>"""
