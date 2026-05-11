"""Real-time alerts: правила, dedup, доставка по каналам.

Архитектура:
1. Клиент создаёт `AlertRule` (через CLI или дашборд) — тип + параметры + каналы
2. После каждого прогона `evaluate_rules(session, run_id)` запускается:
   - Для каждого активного правила — соответствующий detector
   - Каждое детектированное событие проверяется на dedup (cooldown_hours)
   - Не-дубликаты создают `AlertEvent` и отправляются по всем `channels`

Поддерживаемые типы правил:
- `undercut_threshold` — конкурент дешевле клиента на ≥ params["min_pct"]
- `price_drop_pct` — клиент или любой сайт уронил цену на ≥ params["min_pct"] vs предыдущий прогон
- `new_product` — на сайте появился новый SKU (был не в предыдущем прогоне)
- `promo_started` — на конкуренте запустилась новая промо-кампания
- `price_raise_opportunity` — клиент дешевле всех на ≥ params["min_pct"]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from src._time import utcnow
from typing import Any, Callable

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src.storage import (
    AlertEvent,
    AlertRule,
    Match,
    PriceSnapshot,
    Product,
    Promo,
    Run,
)

log = structlog.get_logger()

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")


@dataclass
class CandidateEvent:
    """Не-дедуплицированный детектированный события — кандидат на AlertEvent."""

    rule_type: str
    dedup_key: str
    severity: str  # info / warning / critical
    title: str
    detail: str
    payload: dict


# === DETECTORS ===


def _detect_undercut_threshold(
    session: Session, run_id: int, params: dict
) -> list[CandidateEvent]:
    """Конкурент дешевле клиента на ≥ min_pct."""
    min_pct = float(params.get("min_pct", 5.0))
    out: list[CandidateEvent] = []
    matches = session.scalars(select(Match)).all()
    for m in matches:
        prices = _prices_for_match(session, m, run_id)
        client_price = prices.get(CLIENT_SITE)
        if client_price is None:
            continue
        for site in COMPETITOR_SITES:
            comp_price = prices.get(site)
            if comp_price is None or comp_price >= client_price:
                continue
            diff_pct = (client_price - comp_price) / client_price * 100
            if diff_pct < min_pct:
                continue
            sev = "critical" if diff_pct >= 10 else "warning"
            out.append(CandidateEvent(
                rule_type="undercut_threshold",
                dedup_key=f"undercut|m={m.id}|site={site}",
                severity=sev,
                title=f"{site} дешевле на {diff_pct:.1f}%: {m.canonical_name}",
                detail=(
                    f"Клиент: {client_price:.2f} ₼, {site}: {comp_price:.2f} ₼ "
                    f"(−{diff_pct:.1f}%)."
                ),
                payload={
                    "match_id": m.id, "site": site,
                    "client_price": client_price, "competitor_price": comp_price,
                    "diff_pct": round(diff_pct, 2),
                },
            ))
    return out


def _detect_price_drop(
    session: Session, run_id: int, params: dict
) -> list[CandidateEvent]:
    """Любой сайт уронил цену на ≥ min_pct относительно предыдущего snapshot'а.

    Diff-only-aware (2026-05-09): «предыдущий» — последний snapshot из
    более ранних прогонов (не обязательно из соседнего ok-прогона; цена
    могла стоять стабильно неделю и не писаться в snapshots).
    """
    from src.storage import curr_and_prev_snapshots_for_run

    min_pct = float(params.get("min_pct", 10.0))
    site_filter = params.get("site")  # None = все сайты, иначе только указанный
    out: list[CandidateEvent] = []

    current_run = session.get(Run, run_id)
    if current_run is None:
        return []

    curr_snaps, prev_by_product = curr_and_prev_snapshots_for_run(session, current_run)

    for snap in curr_snaps:
        prev = prev_by_product.get(snap.product_id)
        if not prev:
            continue
        prev_price = prev.discount_price or prev.price
        curr_price = snap.discount_price or snap.price
        if prev_price is None or curr_price is None or prev_price <= 0:
            continue
        drop_pct = (prev_price - curr_price) / prev_price * 100
        if drop_pct < min_pct:
            continue
        product = snap.product
        if site_filter and product.site != site_filter:
            continue
        sev = "warning" if drop_pct < 20 else "critical"
        out.append(CandidateEvent(
            rule_type="price_drop_pct",
            dedup_key=f"drop|p={product.id}",
            severity=sev,
            title=f"Цена упала на {drop_pct:.1f}%: {product.name[:60]}",
            detail=(
                f"{product.site}: {prev_price:.2f} → {curr_price:.2f} ₼ "
                f"(−{drop_pct:.1f}%)."
            ),
            payload={
                "product_id": product.id, "site": product.site,
                "prev_price": prev_price, "curr_price": curr_price,
                "drop_pct": round(drop_pct, 2),
            },
        ))
    return out


def _detect_new_product(
    session: Session, run_id: int, params: dict
) -> list[CandidateEvent]:
    """Появились товары впервые в БД (нет snapshot'ов до текущего прогона).

    Diff-only-aware (2026-05-09): «новый» = нет snapshot'ов до current_run.
    """
    from src.storage import curr_and_prev_snapshots_for_run

    site_filter = params.get("site")
    out: list[CandidateEvent] = []

    current_run = session.get(Run, run_id)
    if current_run is None:
        return []

    curr_snaps, prev_by_product = curr_and_prev_snapshots_for_run(session, current_run)
    for snap in curr_snaps:
        if snap.product_id in prev_by_product:
            continue
        product = snap.product
        if site_filter and product.site != site_filter:
            continue
        out.append(CandidateEvent(
            rule_type="new_product",
            dedup_key=f"new|p={product.id}",
            severity="info",
            title=f"Новый товар на {product.site}: {product.name[:60]}",
            detail=(
                f"Цена: {snap.price or '?'} ₼. "
                f"Появился впервые на {product.site}."
            ),
            payload={"product_id": product.id, "site": product.site},
        ))
    return out


def _detect_promo_started(
    session: Session, run_id: int, params: dict
) -> list[CandidateEvent]:
    """Новые промо-кампании на конкурентах."""
    out: list[CandidateEvent] = []
    prev_run_id = session.scalar(
        select(Run.id)
        .where(Run.id != run_id, Run.status == "ok")
        .order_by(desc(Run.id))
        .limit(1)
    )
    if not prev_run_id:
        return []
    prev_keys = {
        (p.site, p.title) for p in session.scalars(
            select(Promo).where(Promo.run_id == prev_run_id)
        )
    }
    curr_promos = session.scalars(
        select(Promo).where(Promo.run_id == run_id)
    ).all()
    for promo in curr_promos:
        if (promo.site, promo.title) in prev_keys:
            continue
        out.append(CandidateEvent(
            rule_type="promo_started",
            dedup_key=f"promo|{promo.site}|{promo.title[:80]}",
            severity="warning",
            title=f"Новая промо у {promo.site}: {promo.title[:60]}",
            detail=promo.title,
            payload={"site": promo.site, "title": promo.title, "url": promo.landing_url},
        ))
    return out


def _detect_price_raise_opportunity(
    session: Session, run_id: int, params: dict
) -> list[CandidateEvent]:
    """Клиент дешевле всех конкурентов на ≥ min_pct."""
    min_pct = float(params.get("min_pct", 7.0))
    out: list[CandidateEvent] = []
    matches = session.scalars(select(Match)).all()
    for m in matches:
        prices = _prices_for_match(session, m, run_id)
        client_price = prices.get(CLIENT_SITE)
        if client_price is None:
            continue
        comp_prices = [p for s, p in prices.items() if s in COMPETITOR_SITES and p is not None]
        if not comp_prices:
            continue
        median = sorted(comp_prices)[len(comp_prices) // 2]
        if median <= client_price:
            continue
        gap_pct = (median - client_price) / client_price * 100
        if gap_pct < min_pct:
            continue
        out.append(CandidateEvent(
            rule_type="price_raise_opportunity",
            dedup_key=f"raise|m={m.id}",
            severity="info",
            title=f"Можно поднять: {m.canonical_name} (+{gap_pct:.1f}%)",
            detail=(
                f"Клиент: {client_price:.2f} ₼. Медиана конкурентов: {median:.2f} ₼."
            ),
            payload={
                "match_id": m.id, "client_price": client_price,
                "median_competitor": median, "gap_pct": round(gap_pct, 2),
            },
        ))
    return out


DETECTORS: dict[str, Callable[[Session, int, dict], list[CandidateEvent]]] = {
    "undercut_threshold": _detect_undercut_threshold,
    "price_drop_pct": _detect_price_drop,
    "new_product": _detect_new_product,
    "promo_started": _detect_promo_started,
    "price_raise_opportunity": _detect_price_raise_opportunity,
}


# === EVALUATION ENGINE ===


def evaluate_rules(
    session: Session,
    run_id: int | None = None,
    rule_ids: list[int] | None = None,
) -> list[AlertEvent]:
    """Прогнать все активные правила, создать AlertEvent для не-дубликатов.

    `rule_ids` — если задан, прогоняются только указанные правила
    (для UI "запустить вручную одно правило").
    """
    if run_id is None:
        run_id = session.scalar(
            select(Run.id).where(Run.status == "ok").order_by(desc(Run.id)).limit(1)
        )
        if run_id is None:
            log.warning("alerts_no_run", reason="no successful runs in DB")
            return []

    stmt = select(AlertRule).where(AlertRule.is_active.is_(True))
    if rule_ids:
        stmt = stmt.where(AlertRule.id.in_(rule_ids))
    rules = session.scalars(stmt).all()

    fired: list[AlertEvent] = []
    for rule in rules:
        detector = DETECTORS.get(rule.rule_type)
        if detector is None:
            log.warning("alerts_unknown_rule_type", rule_type=rule.rule_type)
            continue
        try:
            candidates = detector(session, run_id, rule.params or {})
        except Exception as e:
            log.exception("alerts_detector_failed", rule_type=rule.rule_type, error=str(e))
            continue

        for cand in candidates:
            if _is_duplicate(session, cand.dedup_key, rule.cooldown_hours):
                continue
            event = AlertEvent(
                rule_id=rule.id,
                rule_type=cand.rule_type,
                dedup_key=cand.dedup_key,
                severity=cand.severity,
                title=cand.title,
                detail=cand.detail,
                payload=cand.payload,
            )
            session.add(event)
            session.flush()
            fired.append(event)

    session.commit()
    log.info("alerts_evaluated", run_id=run_id, fired=len(fired), rules=len(rules))
    # Prometheus metrics: count by severity + rule_type
    try:
        from src.observability import metrics

        for ev in fired:
            metrics.alert_events_total.labels(
                severity=ev.severity,
                rule_type=ev.rule_type or "unknown",
            ).inc()
    except Exception:
        pass
    return fired


def _is_duplicate(session: Session, dedup_key: str, cooldown_hours: int) -> bool:
    """Был ли event с таким dedup_key за последние cooldown_hours."""
    cutoff = utcnow() - timedelta(hours=cooldown_hours)
    existing = session.scalar(
        select(AlertEvent.id)
        .where(AlertEvent.dedup_key == dedup_key, AlertEvent.created_at >= cutoff)
        .limit(1)
    )
    return existing is not None


def _prices_for_match(session: Session, match: Match, run_id: int) -> dict[str, float | None]:
    """Цены на сайтах для конкретного match'а — берём latest snapshot per product.

    Diff-only-aware (2026-05-09): после оптимизации persist'а snapshot не
    пишется каждый прогон. `run_id` параметр сохранён для обратной
    совместимости с сигнатурой, но семантика теперь — «текущая (latest)
    цена», что и нужно для realtime undercut alerts.
    """
    from src.storage import latest_snapshots_per_product

    out: dict[str, float | None] = {CLIENT_SITE: None, "aptekonline": None, "aloe": None}
    pids = [p.id for p in match.products]
    snaps = latest_snapshots_per_product(session, pids)
    for p in match.products:
        snap = snaps.get(p.id)
        if snap is None:
            continue
        eff = snap.discount_price if snap.discount_price else snap.price
        if eff is not None:
            out[p.site] = eff
    return out


# === DELIVERY ===


def dispatch_event(session: Session, event: AlertEvent) -> dict:
    """Отправить event по каналам которые настроены в его правиле.

    Возвращает {channel: status} где status = 'sent' / 'skipped' / 'error: msg'.
    """
    rule = session.get(AlertRule, event.rule_id) if event.rule_id else None
    channels = (rule.channels if rule else None) or ["email"]
    results: dict[str, Any] = {}

    if "email" in channels:
        results["email"] = _send_email_alert(event)

    if "telegram" in channels:
        results["telegram"] = _send_telegram_alert(session, event)

    event.channels_sent = list(results.keys())
    session.commit()
    return results


def _send_email_alert(event: AlertEvent) -> str:
    """Отправить email-алерт. Использует существующий notifier."""
    try:
        from src import notifier
        sev_emoji = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️"}.get(
            event.severity, "•"
        )
        html = f"""
        <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px;
                    margin:24px auto;padding:18px 24px;background:#fff;
                    border-radius:12px;border:1px solid #e5e5e7;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;
                      color:#86868b;font-weight:500;">
            Pharmacy Monitor — Alert
          </div>
          <div style="font-size:18px;font-weight:600;margin-top:6px;">
            {sev_emoji} {event.title}
          </div>
          <div style="font-size:14px;color:#3a3a3c;margin-top:10px;line-height:1.4;">
            {event.detail or ''}
          </div>
        </div>
        """
        notifier.send_email(
            subject=f"[{event.severity.upper()}] {event.title[:80]}",
            html_body=html,
        )
        return "sent"
    except Exception as e:
        log.warning("alert_email_failed", error=str(e))
        return f"error: {e}"


def _send_telegram_alert(session: Session, event: AlertEvent) -> str:
    """Отправить Telegram-алерт всем активным получателям с привязанным chat_id."""
    try:
        from src import notifier, watchlist as wl
        recipients = wl.list_recipients(session, active_only=True)
        chat_ids = [r.telegram_chat_id for r in recipients if r.telegram_chat_id]
        if not chat_ids:
            return "skipped: no telegram chat_ids"
        sev_emoji = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️"}.get(
            event.severity, "•"
        )
        text = f"{sev_emoji} *{event.title}*\n\n{event.detail or ''}"
        sent = 0
        for chat_id in chat_ids:
            ok = notifier.send_telegram_message(chat_id, text)
            if ok:
                sent += 1
        return f"sent to {sent}/{len(chat_ids)}"
    except Exception as e:
        log.warning("alert_telegram_failed", error=str(e))
        return f"error: {e}"
