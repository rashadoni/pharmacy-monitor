"""ROI / Actionable insights — превращаем сырые данные в конкретные действия.

Главная цель: вместо "вот метрики, разбирайся" дать клиенту:
    "Опусти Aspirin Cardio на 0.50 ₼ за единицу (−31% от текущей)"

Поддерживаемые типы действий:
    1. PRICE_RAISE     — клиент дешевле всех конкурентов, можно поднять
    2. UNDERCUT        — конкурент опустил цену ниже клиента (нужна реакция)
    3. ASSORTMENT_GAP  — товар есть у конкурента, нет у клиента
    4. PROMO_RESPONSE  — конкурент запустил акцию (нужно реагировать)

Что НЕ рассчитываем (и почему):
    «AZN/месяц» убран 2026-05-13 — старый код умножал разницу цен на
    захардкоженный объём `assumed_monthly_volume=30`. Это был placeholder
    без основания: у одного товара 200 шт/мес, у другого 2 шт/мес. Получался
    misleading вывод где приоритеты не отражали реальный профит.
    Теперь возвращаем ТОЛЬКО проверяемые цифры: `unit_gap_azn` (разница на
    единицу) и `spread_pct` (% спред). Клиент сам прикинет ×свой_объём.

    Когда у клиента будет ERP-интеграция с реальной stock-историей, можно
    будет deriv'ить `monthly_volume = (stock_start - stock_end + purchases)
    / days × 30` per-product и вернуть честные ₼/мес.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src._time import utcnow

from src import storage  # for load_pricing_config + RoiActionsCache
from src.storage import (
    Match,
    PriceSnapshot,
    Product,
    Promo,
    Run,
)
from src.run_lock import try_shared_scrape_read_lock

log = structlog.get_logger()

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")
ALL_SITES = (CLIENT_SITE, *COMPETITOR_SITES)

ActionType = Literal[
    "price_raise",
    "undercut",
    "assortment_gap",
    "promo_response",
    "map_violation",
]

# Phase 4.5 (2026-05-27): MAP-violation thresholds (% gap to brand-floor).
# Brand-floor = min observed competitor price for that brand over last N days.
# Если клиент дешевле floor больше чем на X% — флагуем (можно поднять цену).
_MAP_FLOOR_WINDOW_DAYS = 30
_MAP_MIN_GAP_PCT = 5.0  # ниже — не флагуем (внутри обычного шума)
_MAP_WARNING_GAP_PCT = 10.0  # выше — severity = "warning"
_MAP_CRITICAL_GAP_PCT = 20.0  # выше — severity = "critical"
# Fix 2026-06-03: защита от битых/placeholder цен. MAP-gap floor-relative (0–100%),
# верхней границы не было → товар клиента с ценой ~0.01 (нет реальной цены на сайте,
# сохранён как placeholder) давал gap ~99.9% и всплывал наверх как «подними с 0.01 до
# 11.14 (+99.9%)» — мусор, который убивает доверие в демо. Зеркалит max_spread guard
# из _price_raise_opportunities. Клиент дальше чем на _MAP_MAX_GAP_PCT ниже floor —
# почти наверняка data-artifact или mismatch (другой размер/grade), не opportunity.
_MAP_MAX_GAP_PCT = 80.0  # gap >= этого — не флагуем (битая цена / bad match)
_MIN_PLAUSIBLE_PRICE = 0.20  # цена ниже — считаем «нет реальной цены» (placeholder)


@dataclass
class ActionItem:
    """Одно конкретное действие, которое клиент может предпринять."""

    type: ActionType
    severity: Literal["info", "opportunity", "warning", "critical"]
    title: str
    detail: str
    product_name: str | None = None
    product_url: str | None = None
    current_value_azn: float | None = None
    target_value_azn: float | None = None
    # Разница цены за ЕДИНИЦУ товара (положительное = профит при подъёме,
    # отрицательное = маржа которую теряем если опустим до конкурента).
    unit_gap_azn: float | None = None
    # % спред между ценами. Положительный для price_raise (мы дешевле),
    # отрицательный для undercut (конкурент дешевле).
    spread_pct: float | None = None
    # DEPRECATED 2026-05-13: всегда 0.0. Раньше = unit_gap × assumed_volume=30,
    # но volume был placeholder без основания. Поле остаётся для backward-compat
    # с telegram_bot.py + старого dashboard.py. Не использовать в новом коде.
    estimated_monthly_impact_azn: float = 0.0
    competitor_site: str | None = None
    competitor_url: str | None = None
    extra: dict = field(default_factory=dict)


def _preload_snapshots(
    session: Session,
    run_id: int,
    *,
    tenant_id: int = 1,
) -> dict[int, "PriceSnapshot"]:
    """Загрузить latest snapshot per product, для всех matched товаров.

    Diff-only-aware (2026-05-09): после оптимизации persist'а snapshot
    пишется только при изменении цены. «Цена в последнем прогоне» больше
    не имеет смысла per-product — нужна latest snapshot регардлес of run.

    Аргумент `run_id` оставлен для совместимости интерфейса, но семантика
    теперь — latest globally (внутренне ограничено матчевыми product_ids).
    """
    from src.storage import latest_snapshots_per_product

    matched_pids = session.scalars(
        select(Product.id).where(
            Product.canonical_id.is_not(None),
            Product.tenant_id == tenant_id,
        )
    ).all()
    if not matched_pids:
        return {}
    return latest_snapshots_per_product(
        session,
        matched_pids,
        financially_eligible_only=True,
        tenant_id=tenant_id,
    )


# ─── Persistent cache ────────────────────────────────────────────────────────
# compute_actions для 3000+ матчей занимает 15-30с. HTTP-handler с таймаутом
# 15с возвращал 408 на 4 экранах из 11 (P0.1 PO Audit). Решение:
# pre-compute после scrape success → DB-cache → serve из кэша.

# Cache freshness threshold. Старше — игнорируем и показываем unavailable;
# inline fallback запрещён, потому что он может прочитать непроверенные данные.
_CACHE_MAX_AGE_HOURS = 26


def financial_inputs_are_fresh(session: Session, *, tenant_id: int) -> bool:
    """Every site must have verified lineage within its real scrape cadence."""
    from src.health import _SITE_MAX_AGE_HOURS

    run_ids = storage.latest_financial_run_ids_by_site(
        session,
        ALL_SITES,
        tenant_id=tenant_id,
    )
    if set(run_ids) != set(ALL_SITES):
        log.warning(
            "roi_financial_inputs_missing_lineage",
            expected=list(ALL_SITES),
            found=run_ids,
            tenant_id=tenant_id,
        )
        return False
    attempts = storage.latest_full_catalog_attempts_by_site(
        session,
        ALL_SITES,
        tenant_id=tenant_id,
    )
    if set(attempts) != set(ALL_SITES):
        log.warning(
            "roi_financial_inputs_missing_attempts",
            expected=list(ALL_SITES),
            found=sorted(attempts.keys()),
            tenant_id=tenant_id,
        )
        return False
    if any(attempts[site].id != run_ids[site] for site in ALL_SITES):
        log.warning(
            "roi_financial_inputs_attempt_mismatch",
            attempts={site: attempts[site].id for site in attempts},
            run_ids=run_ids,
            tenant_id=tenant_id,
        )
        return False
    runs = {
        run.id: run
        for run in session.scalars(
            select(Run).where(Run.id.in_(set(run_ids.values())))
        ).all()
    }
    now = utcnow()
    for site, run_id in run_ids.items():
        run = runs.get(run_id)
        if run is None:
            return False
        max_age_hours = _SITE_MAX_AGE_HOURS.get(site, _CACHE_MAX_AGE_HOURS)
        if now - run.started_at > timedelta(hours=max_age_hours):
            return False
    return True


def _action_to_dict(a: "ActionItem") -> dict:
    """Сериализатор для кэша. Структура зеркалит HTTP response в api.py."""
    return {
        "type": a.type,
        "severity": a.severity,
        "title": a.title,
        "detail": a.detail,
        "product_name": a.product_name,
        "product_url": a.product_url,
        "current_value_azn": a.current_value_azn,
        "target_value_azn": a.target_value_azn,
        "unit_gap_azn": a.unit_gap_azn,
        "spread_pct": a.spread_pct,
        "estimated_monthly_impact_azn": a.estimated_monthly_impact_azn,
        "competitor_site": a.competitor_site,
        "competitor_url": a.competitor_url,
        "extra": a.extra,
    }


def cache_actions(
    session: Session,
    client_site: str,
    actions: list["ActionItem"],
    *,
    run_id: int | None = None,
    tenant_id: int = 1,
) -> None:
    """Upsert pre-computed actions в `roi_actions_cache` для (tenant, client_site).

    Безопасно вызывать многократно — UNIQUE(tenant_id, client_site) гарантирует
    один row per срез. Никаких внешних зависимостей, sync операция.
    """
    from src.storage import RoiActionsCache

    run = session.get(Run, run_id) if run_id is not None else None
    if run is None or run.tenant_id != tenant_id or not storage.run_is_financially_eligible(run):
        raise ValueError("ROI cache requires a financially eligible full-catalog run")
    if not financial_inputs_are_fresh(session, tenant_id=tenant_id):
        raise ValueError("ROI cache requires fresh verified full-catalog inputs for every site")
    from src.product_policy import policy_fingerprint, trusted_catalog_epoch

    payload = [_action_to_dict(a) for a in actions]
    fingerprint = policy_fingerprint()
    epoch = trusted_catalog_epoch(session, tenant_id=tenant_id)
    if epoch is None:
        raise RuntimeError("trusted full-catalog epoch is unavailable")
    existing = session.scalar(
        select(RoiActionsCache).where(
            RoiActionsCache.tenant_id == tenant_id,
            RoiActionsCache.client_site == client_site,
        )
    )
    if existing:
        existing.payload = payload
        existing.computed_at = utcnow()
        existing.run_id = run_id
        existing.policy_fingerprint = fingerprint
        existing.trust_epoch = epoch
    else:
        session.add(
            RoiActionsCache(
                tenant_id=tenant_id,
                client_site=client_site,
                payload=payload,
                computed_at=utcnow(),
                run_id=run_id,
                policy_fingerprint=fingerprint,
                trust_epoch=epoch,
            )
        )
    session.commit()
    log.info(
        "roi_actions_cached",
        client_site=client_site,
        count=len(payload),
        run_id=run_id,
    )


def get_cached_actions(
    session: Session,
    client_site: str,
    *,
    tenant_id: int = 1,
    max_age_hours: int = _CACHE_MAX_AGE_HOURS,
) -> list[dict] | None:
    """Прочитать кэш или вернуть None если stale/missing.

    None означает, что caller должен fail closed и скрыть рекомендации.
    """
    snapshot = get_cached_actions_snapshot(
        session,
        client_site,
        tenant_id=tenant_id,
        max_age_hours=max_age_hours,
    )
    return snapshot[0] if snapshot is not None else None


def get_cached_actions_snapshot(
    session: Session,
    client_site: str,
    *,
    tenant_id: int = 1,
    max_age_hours: int = _CACHE_MAX_AGE_HOURS,
) -> tuple[list[dict], storage.RoiActionsCache, Run] | None:
    """Return payload and provenance from the same validated cache row.

    Keeping the selected row object with its payload prevents an HTTP caller
    from labelling recommendations from run A with metadata from a concurrently
    refreshed run B.  Newer full-catalog attempts still fail the snapshot
    closed through the lineage checks below.
    """
    from src.product_policy import policy_fingerprint, trusted_catalog_epoch

    row = session.scalar(
        select(storage.RoiActionsCache).where(
            storage.RoiActionsCache.tenant_id == tenant_id,
            storage.RoiActionsCache.client_site == client_site,
        )
    )
    if row is None:
        return None
    run = session.get(Run, row.run_id) if row.run_id is not None else None
    if run is None or run.tenant_id != tenant_id or not storage.run_is_financially_eligible(run):
        log.warning(
            "roi_actions_cache_unverified",
            client_site=client_site,
            run_id=row.run_id,
        )
        return None
    latest_attempts = storage.latest_full_catalog_attempts_by_site(
        session,
        ALL_SITES,
        tenant_id=tenant_id,
    )
    cache_completed_at = run.finished_at or run.started_at
    for site, latest_attempt in latest_attempts.items():
        if latest_attempt.id == run.id:
            continue
        attempt_completed_at = latest_attempt.finished_at or latest_attempt.started_at
        if (attempt_completed_at, latest_attempt.id) > (cache_completed_at, run.id):
            log.warning(
                "roi_actions_cache_superseded",
                client_site=client_site,
                cache_run_id=run.id,
                superseding_site=site,
                latest_full_attempt_id=latest_attempt.id,
                latest_full_attempt_status=latest_attempt.status,
            )
            return None
    if not financial_inputs_are_fresh(session, tenant_id=tenant_id):
        log.warning(
            "roi_actions_cache_inputs_unverified",
            client_site=client_site,
            run_id=row.run_id,
        )
        return None
    current_epoch = trusted_catalog_epoch(session, tenant_id=tenant_id)
    if current_epoch is None or row.trust_epoch != current_epoch:
        log.info(
            "roi_actions_cache_epoch_mismatch",
            client_site=client_site,
            cached=row.trust_epoch,
            current=current_epoch,
        )
        return None
    current_fingerprint = policy_fingerprint()
    if row.policy_fingerprint != current_fingerprint:
        log.info(
            "roi_actions_cache_policy_mismatch",
            client_site=client_site,
            cached=row.policy_fingerprint,
            current=current_fingerprint,
        )
        return None
    age = utcnow() - row.computed_at
    if age > timedelta(hours=max_age_hours):
        log.info(
            "roi_actions_cache_stale",
            client_site=client_site,
            age_hours=age.total_seconds() / 3600,
        )
        return None
    payload = list(row.payload) if row.payload else []
    return payload, row, run


def get_cached_action_items(
    session: Session,
    client_site: str,
    *,
    tenant_id: int = 1,
    max_age_hours: int = _CACHE_MAX_AGE_HOURS,
) -> list[ActionItem] | None:
    """Validated ActionItem view for non-HTTP consumers such as Telegram."""
    payload = get_cached_actions(
        session,
        client_site,
        tenant_id=tenant_id,
        max_age_hours=max_age_hours,
    )
    if payload is None:
        return None
    try:
        return [ActionItem(**row) for row in payload]
    except (TypeError, ValueError) as exc:
        log.warning(
            "roi_actions_cache_invalid_payload",
            client_site=client_site,
            error=str(exc),
        )
        return None


def refresh_all_cached_actions(
    session: Session,
    *,
    run_id: int | None = None,
    tenant_id: int = 1,
) -> dict[str, int]:
    """Пересчитать кэш для всех 3 сайтов. Вызывается из main.py после
    подтверждённого full-catalog run. Возвращает {site: count}.
    """
    run = session.get(Run, run_id) if run_id is not None else None
    if run is None or run.tenant_id != tenant_id or not storage.run_is_financially_eligible(run):
        raise ValueError("ROI refresh requires a financially eligible full-catalog run")
    if not financial_inputs_are_fresh(session, tenant_id=tenant_id):
        raise ValueError("ROI refresh requires fresh verified full-catalog inputs for every site")
    out: dict[str, int] = {}
    for site in ALL_SITES:
        try:
            actions = _compute_actions_locked(session, client_site=site, tenant_id=tenant_id)
            cache_actions(session, site, actions, run_id=run_id, tenant_id=tenant_id)
            out[site] = len(actions)
        except Exception as e:
            log.warning(
                "roi_actions_cache_failed",
                client_site=site,
                error=str(e),
            )
            out[site] = -1
            # Never retain a financially stale payload after a failed refresh.
            session.query(storage.RoiActionsCache).filter(
                storage.RoiActionsCache.tenant_id == tenant_id,
                storage.RoiActionsCache.client_site == site,
            ).delete(synchronize_session=False)
            session.commit()
    return out


def compute_actions(
    session: Session,
    *,
    client_site: str | None = None,
    tenant_id: int = 1,
    raise_threshold_pct: float | None = None,
    undercut_threshold_pct: float | None = None,
    max_spread_pct: float | None = None,
    max_per_type: int | None = None,
    min_margin_pct: float | None = None,
) -> list[ActionItem]:
    """External live computation guarded against concurrent scrape writes."""
    with try_shared_scrape_read_lock(session) as acquired:
        if not acquired:
            log.warning(
                "roi_compute_scrape_in_progress",
                client_site=client_site or CLIENT_SITE,
                tenant_id=tenant_id,
            )
            return []
        return _compute_actions_locked(
            session,
            client_site=client_site,
            tenant_id=tenant_id,
            raise_threshold_pct=raise_threshold_pct,
            undercut_threshold_pct=undercut_threshold_pct,
            max_spread_pct=max_spread_pct,
            max_per_type=max_per_type,
            min_margin_pct=min_margin_pct,
        )


def _compute_actions_locked(
    session: Session,
    *,
    client_site: str | None = None,
    tenant_id: int = 1,
    raise_threshold_pct: float | None = None,
    undercut_threshold_pct: float | None = None,
    max_spread_pct: float | None = None,
    max_per_type: int | None = None,
    min_margin_pct: float | None = None,
) -> list[ActionItem]:
    """Главная точка: собрать все действия, отсортировать по spread desc.

    `client_site` (optional) — какой сайт рассматривать как «свой» (с perspective
    которого считаем undercut/raise/assortment-gap). По умолчанию используется
    CLIENT_SITE; выбранная перспектива передаётся во все расчёты явно и не
    мутирует module-level state, поэтому параллельные tenant-запросы изолированы.

    Phase 4.1 (2026-05-27): thresholds теперь грузятся из `pricing_config` table
    через `storage.load_pricing_config(session, tenant_id)`. Explicit kwarg
    overrides DB value. None → use DB. Используем кэш-row для соответствующего
    tenant; первый запрос создаёт row с дефолтами (5/3/80/10/10).

    Сортируем по |spread_pct| desc — самые большие разрывы вверху. Не по
    «месячному impact'у» потому что объёмы продаж нам неизвестны (см.
    module docstring).
    """
    from src.product_policy import policy_rollout_eligibility

    rollout = policy_rollout_eligibility(session, tenant_id=tenant_id)
    if not rollout.eligible:
        log.warning("roi_policy_gate_closed", reason=rollout.reason, tenant_id=tenant_id)
        return []

    # Load per-tenant config (creates default row if missing).
    cfg = storage.load_pricing_config(session, tenant_id)
    # Apply explicit overrides (kwargs win over DB).
    raise_pct = raise_threshold_pct if raise_threshold_pct is not None else cfg.raise_threshold_pct
    undercut_pct = (
        undercut_threshold_pct if undercut_threshold_pct is not None else cfg.undercut_threshold_pct
    )
    max_spread = max_spread_pct if max_spread_pct is not None else cfg.max_spread_pct
    per_type = max_per_type if max_per_type is not None else cfg.max_per_type
    min_margin = min_margin_pct if min_margin_pct is not None else cfg.min_margin_pct

    if storage.has_unfinished_run(session, tenant_id=tenant_id):
        log.warning(
            "roi_compute_run_in_progress",
            client_site=client_site or CLIENT_SITE,
            tenant_id=tenant_id,
        )
        return []

    if not financial_inputs_are_fresh(session, tenant_id=tenant_id):
        log.warning(
            "roi_compute_inputs_unverified",
            client_site=client_site or CLIENT_SITE,
            tenant_id=tenant_id,
        )
        return []

    effective_client = client_site or CLIENT_SITE
    if effective_client not in ALL_SITES:
        raise ValueError(f"unknown client_site: {effective_client!r}")
    competitors = tuple(site for site in ALL_SITES if site != effective_client)
    actions: list[ActionItem] = []
    actions += _price_raise_opportunities(
        session,
        raise_pct,
        max_spread,
        per_type,
        tenant_id=tenant_id,
        client_site=effective_client,
        competitor_sites=competitors,
    )
    actions += _undercut_threats(
        session,
        undercut_pct,
        max_spread,
        per_type,
        min_margin_pct=min_margin,
        tenant_id=tenant_id,
        client_site=effective_client,
        competitor_sites=competitors,
    )
    actions += _assortment_gaps(
        session,
        per_type,
        tenant_id=tenant_id,
        competitor_sites=competitors,
    )
    actions += _map_violations(
        session,
        per_type,
        tenant_id=tenant_id,
        client_site=effective_client,
        competitor_sites=competitors,
    )
    actions += _promo_responses(
        session,
        per_type,
        tenant_id=tenant_id,
        competitor_sites=competitors,
    )

    # Сортировка: critical → warning → opportunity → info; внутри — по |spread_pct| desc
    sev_order = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
    actions.sort(
        key=lambda a: (
            sev_order.get(a.severity, 9),
            -abs(a.spread_pct or 0),
        )
    )
    return actions


def _latest_run_id(session: Session, *, tenant_id: int = 1) -> int | None:
    eligible_ids = storage.financially_eligible_run_ids(session, tenant_id=tenant_id)
    if not eligible_ids:
        return None
    return session.scalar(
        select(Run.id).where(Run.id.in_(eligible_ids)).order_by(desc(Run.id)).limit(1)
    )


def _price_raise_opportunities(
    session: Session,
    threshold_pct: float,
    max_spread_pct: float,
    max_n: int,
    *,
    tenant_id: int,
    client_site: str,
    competitor_sites: tuple[str, ...],
) -> list[ActionItem]:
    """Где клиент дешевле всех конкурентов более чем на threshold%.

    Пропускаем gap >= `max_spread_pct` (80% по умолчанию) — почти наверняка
    bad match (например, поштучный товар склеен с упаковкой 10 шт), советовать
    «подними с 0.20 до 7.60 ₼ — будешь в 3800% дороже» бесполезно.
    """
    run_id = _latest_run_id(session, tenant_id=tenant_id)
    if not run_id:
        return []

    matches = session.scalars(
        select(Match).where(Match.tenant_id == tenant_id)
    ).all()
    snaps_cache = _preload_snapshots(session, run_id, tenant_id=tenant_id)
    out: list[ActionItem] = []

    for m in matches:
        prices_by_site = _prices_for_match(
            session, m, run_id, snaps_cache, client_site=client_site
        )
        client_price = prices_by_site.get(client_site)
        if client_price is None or client_price <= 0:
            continue  # цена 0 → иначе деление на 0 в gap_pct (fail-soft, но всё равно гардим)
        comp_prices = [
            (s, p)
            for s, p in prices_by_site.items()
            if s in competitor_sites and p is not None
        ]
        if not comp_prices:
            continue

        median_comp = sorted(p for _, p in comp_prices)[len(comp_prices) // 2]
        if median_comp <= client_price:
            continue  # клиент не дешевле — не наш кейс
        gap_pct = (median_comp - client_price) / client_price * 100
        if gap_pct < threshold_pct:
            continue  # разница слишком мелкая
        if gap_pct >= max_spread_pct:
            continue  # подозрительно — likely bad match (e.g. pack-size mismatch)

        # Целевая цена = медиана конкурентов − 2% буфер (чтоб остаться лучшим)
        target = round(median_comp * 0.98, 2)
        delta = round(target - client_price, 2)

        client_product = _client_product(m, client_site)
        # Если товара нет на складе — нет смысла советовать поднимать
        if client_product and not _is_in_stock(session, client_product.id):
            continue

        out.append(
            ActionItem(
                type="price_raise",
                severity="opportunity",
                title=f"Подними цену: {m.canonical_name}",
                detail=(
                    f"Ты дешевле конкурентов на {gap_pct:.1f}%. "
                    f"Подними с {client_price:.2f} до {target:.2f} ₼ — "
                    f"останешься самым дешёвым, но получишь +{delta:.2f} ₼/ед."
                ),
                product_name=m.canonical_name,
                product_url=client_product.url if client_product else None,
                current_value_azn=client_price,
                target_value_azn=target,
                unit_gap_azn=delta,
                spread_pct=round(gap_pct, 1),
                extra={"median_competitor": median_comp},
            )
        )

    out.sort(key=lambda a: -(a.spread_pct or 0))
    return out[:max_n]


def _is_in_stock(session: Session, product_id: int) -> bool:
    """True если у клиента есть остаток на складе (или нет данных stock — считаем что есть)."""
    from src.inventory import get_stock_for_product

    stock = get_stock_for_product(session, product_id)
    return True if stock is None else bool(stock.is_in_stock)


def _undercut_threats(
    session: Session,
    threshold_pct: float,
    max_spread_pct: float,
    max_n: int,
    *,
    min_margin_pct: float,
    tenant_id: int,
    client_site: str,
    competitor_sites: tuple[str, ...],
) -> list[ActionItem]:
    """Где конкурент опустил цену ниже клиента.

    Пропускаем спреды >= `max_spread_pct` (90% по умолчанию) — такое отклонение
    обычно говорит не о настоящем undercut'е, а о бракованном matching'е
    (например, поштучный товар слепился с упаковкой 10шт). Реальные ценовые
    войны не дают 99% дисконт.
    """
    run_id = _latest_run_id(session, tenant_id=tenant_id)
    if not run_id:
        return []

    matches = session.scalars(
        select(Match).where(Match.tenant_id == tenant_id)
    ).all()
    snaps_cache = _preload_snapshots(session, run_id, tenant_id=tenant_id)
    out: list[ActionItem] = []

    for m in matches:
        prices_by_site = _prices_for_match(
            session, m, run_id, snaps_cache, client_site=client_site
        )
        client_price = prices_by_site.get(client_site)
        if client_price is None:
            continue

        cheapest_comp_site = None
        cheapest_comp_price = None
        for site in competitor_sites:
            p = prices_by_site.get(site)
            if p is None:
                continue
            if cheapest_comp_price is None or p < cheapest_comp_price:
                cheapest_comp_site = site
                cheapest_comp_price = p

        if cheapest_comp_price is None or cheapest_comp_price >= client_price:
            continue
        diff_pct = (client_price - cheapest_comp_price) / client_price * 100
        if diff_pct < threshold_pct:
            continue
        if diff_pct >= max_spread_pct:
            # Подозрительный спред — почти наверняка bad match. Скрываем
            # чтобы не вводить клиента в заблуждение «опусти до 0.25 ₼».
            continue

        # Рекомендованная новая цена = match competitor − 0.01 (быть на копейку дешевле)
        target = round(cheapest_comp_price - 0.01, 2)
        # Lost margin per unit (assuming we have to match)
        lost_per_unit = round(client_price - target, 2)

        client_product = _client_product(m, client_site)
        comp_url = next((p.url for p in m.products if p.site == cheapest_comp_site), None)

        # Stock-aware: если у нас нет товара на складе — undercut неактуален
        if client_product and not _is_in_stock(session, client_product.id):
            continue

        severity: Literal["warning", "critical"] = "critical" if diff_pct >= 10 else "warning"

        # Margin-aware: если опускаем ниже purchase price — критически
        margin_warning = ""
        if client_product:
            from src.inventory import get_min_purchase_price

            purchase = get_min_purchase_price(session, client_product.id)
            if purchase is not None:
                if target <= purchase:
                    severity = "critical"
                    margin_warning = (
                        f" ⚠️ Целевая цена {target:.2f} ₼ ниже закупки {purchase:.2f} ₼ — "
                        f"продажа в убыток. Лучше держать текущую и принять потерю объёма."
                    )
                else:
                    # Phase 4.1: configurable min_margin_pct (was hardcoded 10%)
                    margin_threshold = min_margin_pct / 100.0
                    if (target - purchase) / target < margin_threshold:
                        margin_warning = (
                            f" ⚠️ Маржа после снижения < {min_margin_pct:.0f}% "
                            f"(закупка {purchase:.2f} ₼)."
                        )

        out.append(
            ActionItem(
                type="undercut",
                severity=severity,
                title=f"Конкурент дешевле: {m.canonical_name}",
                detail=(
                    f"{cheapest_comp_site} продаёт за {cheapest_comp_price:.2f} ₼, "
                    f"ты — за {client_price:.2f} ₼ (−{diff_pct:.1f}%). "
                    f"Опусти до {target:.2f} чтобы остаться конкурентным." + margin_warning
                ),
                product_name=m.canonical_name,
                product_url=client_product.url if client_product else None,
                current_value_azn=client_price,
                target_value_azn=target,
                unit_gap_azn=-lost_per_unit,  # отрицательное — потерянная маржа на единицу
                spread_pct=-round(diff_pct, 1),  # отрицательный = конкурент дешевле
                competitor_site=cheapest_comp_site,
                competitor_url=comp_url,
            )
        )

    # Сортируем по |spread_pct| desc — самые серьёзные разрывы сверху
    out.sort(key=lambda a: -abs(a.spread_pct or 0))
    return out[:max_n]


def _assortment_gaps(
    session: Session,
    max_n: int,
    *,
    tenant_id: int,
    competitor_sites: tuple[str, ...],
) -> list[ActionItem]:
    """Товары на конкурентах которых нет у клиента (canonical_id is None).

    Diff-only-aware (2026-05-09): берём competitor unmatched products + их
    latest snapshot (а не «snapshot последнего прогона», который после
    diff-only пропускает продукты без price-changes).
    """
    from src.product_policy import policy_offer_eligibility
    from src.storage import latest_snapshots_per_product

    products = session.scalars(
        select(Product).where(
            Product.tenant_id == tenant_id,
            Product.site.in_(competitor_sites),
            Product.canonical_id.is_(None),
            Product.tenant_id == tenant_id,
        )
    ).all()
    products = [p for p in products if policy_offer_eligibility(p).eligible]
    if not products:
        return []

    snaps_by_pid = latest_snapshots_per_product(
        session,
        [p.id for p in products],
        financially_eligible_only=True,
        tenant_id=tenant_id,
    )
    rows: list[tuple[Product, "PriceSnapshot"]] = []
    for p in products:
        snap = snaps_by_pid.get(p.id)
        if snap is None:
            continue
        rows.append((p, snap))
    # Сортируем по цене desc — самые дорогие unmatched товары вверху
    rows.sort(key=lambda r: r[1].discount_price or r[1].price or 0, reverse=True)

    out: list[ActionItem] = []
    for product, snap in rows[:max_n]:
        price = snap.discount_price or snap.price
        if price is None or price < 1.0:
            continue
        out.append(
            ActionItem(
                type="assortment_gap",
                severity="opportunity",
                title=f"Расширь ассортимент: {product.name}",
                detail=(
                    f"Этот товар есть у {product.site} ({price:.2f} ₼), но нет у тебя. "
                    f"Возможный новый SKU для каталога."
                ),
                product_name=product.name,
                competitor_site=product.site,
                competitor_url=product.url,
                current_value_azn=price,
                extra={"category": product.category},
            )
        )
    return out


def _map_violations(
    session: Session,
    max_n: int,
    *,
    tenant_id: int,
    client_site: str,
    competitor_sites: tuple[str, ...],
) -> list[ActionItem]:
    """Phase 4.5: Detect client products priced below brand-floor.

    Brand-floor = min observed competitor price for that brand over last
    `_MAP_FLOOR_WINDOW_DAYS` days. If client's current price is below this
    floor by more than `_MAP_MIN_GAP_PCT`, flag it.

    This is an OPPORTUNITY to raise price: вся конкуренция держит флор выше,
    значит клиент может поднять без потери конкурентоспособности и оставить
    себе margin. Отличается от `_price_raise_opportunities` тем, что:
      - smоtrит на BRAND-уровне через ВСЕ продукты бренда, не один матч
      - использует 30-дневное окно, не текущий snapshot (стабильнее)
      - не требует point-to-point matched pair (есть бренд → есть signal)

    Severity buckets:
      - critical: gap >= _MAP_CRITICAL_GAP_PCT (20%)
      - warning:  gap >= _MAP_WARNING_GAP_PCT  (10%)
      - info:     gap >= _MAP_MIN_GAP_PCT       (5%)
    """
    from sqlalchemy import func

    from src.product_policy import (
        OFFER_OUT_OF_STOCK,
        availability_policy_enforced,
        current_offer_sql,
        policy_offer_eligibility,
    )
    from src.storage import latest_snapshots_per_product

    floor_window_start = utcnow() - timedelta(days=_MAP_FLOOR_WINDOW_DAYS)

    # Step 1: brand-floor per brand, computed across competitor snapshots over window.
    # Используем discount_price если есть (как effective price), иначе price.
    effective_price = func.coalesce(PriceSnapshot.discount_price, PriceSnapshot.price)
    eligible_run_ids = storage.financially_eligible_run_ids(session, tenant_id=tenant_id)
    if not eligible_run_ids:
        return []
    offer_predicate = (
        current_offer_sql(Product)
        if availability_policy_enforced()
        else Product.offer_availability_status != OFFER_OUT_OF_STOCK
    )
    rows = session.execute(
        select(
            Product.brand,
            func.min(effective_price).label("floor"),
            func.count(PriceSnapshot.id).label("samples"),
        )
        .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
        .where(
            Product.tenant_id == tenant_id,
            Product.site.in_(competitor_sites),
            Product.brand.is_not(None),
            offer_predicate,
            PriceSnapshot.captured_at >= floor_window_start,
            PriceSnapshot.run_id.in_(eligible_run_ids),
            effective_price.is_not(None),
            effective_price >= _MIN_PLAUSIBLE_PRICE,
        )
        .group_by(Product.brand)
    ).all()

    # Минимум 3 snapshot-семпла per бренд чтобы floor был статистически валиден.
    # Иначе одна цена-аномалия может дать ложный floor.
    brand_floors: dict[str, float] = {b: float(f) for b, f, n in rows if f is not None and n >= 3}

    if not brand_floors:
        return []

    # Step 2: client products в тех же брендах + их current effective price.
    client_products = session.scalars(
        select(Product).where(
            Product.tenant_id == tenant_id,
            Product.site == client_site,
            Product.brand.in_(list(brand_floors.keys())),
        )
    ).all()
    client_products = [
        product
        for product in client_products
        if policy_offer_eligibility(product).eligible
    ]
    if not client_products:
        return []

    client_pids = [p.id for p in client_products]
    snaps_by_pid = latest_snapshots_per_product(
        session,
        client_pids,
        financially_eligible_only=True,
        tenant_id=tenant_id,
    )

    map_violations: list[ActionItem] = []
    for p in client_products:
        snap = snaps_by_pid.get(p.id)
        if snap is None:
            continue
        client_price = snap.discount_price or snap.price
        if client_price is None or client_price < _MIN_PLAUSIBLE_PRICE:
            continue  # нет цены / placeholder (~0.01) — не реальная цена, не флагуем

        floor = brand_floors.get(p.brand or "")
        if floor is None or floor <= 0:
            continue

        # Только если клиент ДЕШЕВЛЕ floor (потенциал поднять).
        if client_price >= floor:
            continue

        gap_pct = (floor - client_price) / floor * 100.0
        if gap_pct < _MAP_MIN_GAP_PCT:
            continue
        if gap_pct >= _MAP_MAX_GAP_PCT:
            continue  # клиент в разы ниже floor → data-artifact / mismatch, не opportunity

        if gap_pct >= _MAP_CRITICAL_GAP_PCT:
            sev = "critical"
        elif gap_pct >= _MAP_WARNING_GAP_PCT:
            sev = "warning"
        else:
            sev = "info"

        target = round(floor * 0.99, 2)  # чуть-чуть ниже floor → всё ещё cheapest
        unit_gap = round(target - float(client_price), 2)

        map_violations.append(
            ActionItem(
                type="map_violation",
                severity=sev,
                title=f"MAP: {p.brand} — поднять {p.name}",
                detail=(
                    f"Floor конкурентов по бренду {p.brand} за {_MAP_FLOOR_WINDOW_DAYS}д = "
                    f"{floor:.2f} ₼. Ты — {client_price:.2f} ₼ (−{gap_pct:.1f}%). "
                    f"Подними до {target:.2f} ₼ — останешься самым дешёвым, +{unit_gap:.2f} ₼/ед."
                ),
                product_name=p.name,
                product_url=p.url,
                current_value_azn=float(client_price),
                target_value_azn=target,
                unit_gap_azn=unit_gap,
                spread_pct=gap_pct,
                extra={"brand": p.brand, "floor_window_days": _MAP_FLOOR_WINDOW_DAYS},
            )
        )

    # Sort by gap_pct desc — самые большие violations вверху.
    map_violations.sort(key=lambda a: -(a.spread_pct or 0))
    return map_violations[:max_n]


def _promo_responses(
    session: Session,
    max_n: int,
    *,
    tenant_id: int,
    competitor_sites: tuple[str, ...],
) -> list[ActionItem]:
    """Активные промо у конкурентов — могут потребовать ответа."""
    from sqlalchemy import and_, or_

    run_ids = storage.latest_financial_run_ids_by_site(
        session,
        competitor_sites,
        tenant_id=tenant_id,
    )
    if not run_ids:
        return []
    promos = session.scalars(
        select(Promo).where(
            Promo.tenant_id == tenant_id,
            or_(
                *(
                    and_(Promo.site == site, Promo.run_id == run_id)
                    for site, run_id in run_ids.items()
                )
            ),
        )
    ).all()
    out: list[ActionItem] = []
    for promo in promos[:max_n]:
        out.append(
            ActionItem(
                type="promo_response",
                severity="warning",
                title=f"Промо у {promo.site}: {promo.title[:60]}",
                detail=(
                    f"Конкурент {promo.site} запустил акцию. "
                    "Проверь, не задевает ли твои топ-категории."
                ),
                competitor_site=promo.site,
                competitor_url=promo.landing_url,
            )
        )
    return out


def _prices_for_match(
    session: Session,
    match: Match,
    run_id: int,
    snapshots_cache: dict[int, "PriceSnapshot"] | None = None,
    *,
    client_site: str = CLIENT_SITE,
) -> dict[str, float | None]:
    """Эффективная цена для каждого сайта в Match.

    Если передан `snapshots_cache` (preloaded {product_id: PriceSnapshot}) —
    используется он вместо отдельного SQL на каждый product. Это убирает N+1.
    """
    out: dict[str, float | None] = {
        client_site: None,
        **{site: None for site in ALL_SITES if site != client_site},
    }
    from src.product_policy import policy_identity_eligibility, policy_offer_eligibility

    if not policy_identity_eligibility(list(match.products)).eligible:
        return out
    for p in match.products:
        if not policy_offer_eligibility(p).eligible:
            continue
        if snapshots_cache is not None:
            snap = snapshots_cache.get(p.id)
        else:
            snap = session.scalar(
                select(PriceSnapshot).where(
                    PriceSnapshot.product_id == p.id, PriceSnapshot.run_id == run_id
                )
            )
        if snap is None:
            continue
        eff = snap.discount_price if snap.discount_price else snap.price
        if eff is not None:
            out[p.site] = eff
    return out


def _client_product(match: Match, client_site: str = CLIENT_SITE) -> Product | None:
    return next((p for p in match.products if p.site == client_site), None)


def aggregate_impact(actions: list[ActionItem]) -> dict[str, float]:
    """Свёртка по unit-gap'ам — для информативных карточек.

    Возвращаем сумму unit_gap_azn по типам (положительное = возможный
    профит на единицу, отрицательное = потерянная маржа на единицу).
    Это per-unit агрегат, не «в месяц» — реальный месячный impact зависит
    от объёма продаж которого у нас нет.
    """
    out = {"opportunity": 0.0, "loss": 0.0, "total": 0.0, "count": 0}
    for a in actions:
        out["count"] += 1
        gap = a.unit_gap_azn or 0.0
        if gap > 0:
            out["opportunity"] += gap
        elif gap < 0:
            out["loss"] += gap
        out["total"] += gap
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in out.items()}


# ─── Localisation ─────────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, dict[str, str]]] = {
    "ru": {
        "undercut": {
            "title": "Конкурент дешевле: {name}",
            "detail": "{site} продаёт за {comp:.2f} ₼, ты — за {client:.2f} ₼ (−{pct:.1f}%). Опусти до {target:.2f} чтобы остаться конкурентным.",
        },
        "price_raise": {
            "title": "Подними цену: {name}",
            "detail": "Ты дешевле конкурентов на {pct:.1f}%. Подними с {client:.2f} до {target:.2f} ₼ — останешься самым дешёвым, но получишь +{delta:.2f} ₼/ед.",
        },
        "assortment_gap": {
            "title": "Расширь ассортимент: {name}",
            "detail": "Этот товар есть у {site} ({price:.2f} ₼), но нет у тебя. Возможный новый SKU для каталога.",
        },
        "promo_response": {
            "title": "Промо у {site}: {promo}",
            "detail": "Конкурент {site} запустил акцию. Проверь, не задевает ли твои топ-категории.",
        },
        "map_violation": {
            "title": "MAP: {brand} — поднять {name}",
            "detail": "Floor конкурентов по бренду {brand} за {days}д = {target:.2f} ₼. Ты — {client:.2f} ₼ (−{pct:.1f}%). Подними до {target:.2f} ₼ — останешься самым дешёвым, +{delta:.2f} ₼/ед.",
        },
    },
    "az": {
        "undercut": {
            "title": "Rəqib daha ucuzdur: {name}",
            "detail": "{site} {comp:.2f} ₼-ə satır, siz — {client:.2f} ₼-ə (−{pct:.1f}%). Rəqabətdə qalmaq üçün {target:.2f} ₼-ə endirin.",
        },
        "price_raise": {
            "title": "Qiyməti qaldırın: {name}",
            "detail": "Rəqiblərdən {pct:.1f}% ucuzsunuz. {client:.2f}-dən {target:.2f} ₼-ə qaldırın — ən ucuz qalacaqsınız, +{delta:.2f} ₼/vahid qazanacaqsınız.",
        },
        "assortment_gap": {
            "title": "Çeşidi genişləndirin: {name}",
            "detail": "Bu məhsul {site}-da ({price:.2f} ₼) mövcuddur, sizdə yoxdur. Kataloqunuza yeni SKU əlavə edilə bilər.",
        },
        "promo_response": {
            "title": "{site}-da aksiya: {promo}",
            "detail": "Rəqib {site} aksiya başlatdı. Əsas kateqoriyalarınıza təsir edib-etmədiyini yoxlayın.",
        },
        "map_violation": {
            "title": "MAP: {brand} — {name} qaldırın",
            "detail": "{brand} brendi üçün rəqib floor-u ({days} gün) = {target:.2f} ₼. Siz — {client:.2f} ₼ (−{pct:.1f}%). {target:.2f} ₼-ə qaldırın — ən ucuz qalacaqsınız, +{delta:.2f} ₼/vahid.",
        },
    },
    "en": {
        "undercut": {
            "title": "Competitor cheaper: {name}",
            "detail": "{site} sells for {comp:.2f} ₼, you sell for {client:.2f} ₼ (−{pct:.1f}%). Drop to {target:.2f} ₼ to stay competitive.",
        },
        "price_raise": {
            "title": "Raise price: {name}",
            "detail": "You're {pct:.1f}% cheaper than competitors. Raise from {client:.2f} to {target:.2f} ₼ — stay cheapest and gain +{delta:.2f} ₼/unit.",
        },
        "assortment_gap": {
            "title": "Expand assortment: {name}",
            "detail": "This product is at {site} ({price:.2f} ₼) but not in your catalog. Possible new SKU.",
        },
        "promo_response": {
            "title": "Promo at {site}: {promo}",
            "detail": "Competitor {site} launched a promotion. Check if it affects your top categories.",
        },
        "map_violation": {
            "title": "MAP: {brand} — raise {name}",
            "detail": "Competitor floor for brand {brand} over {days}d = {target:.2f} ₼. You — {client:.2f} ₼ (−{pct:.1f}%). Raise to {target:.2f} ₼ — stay cheapest and gain +{delta:.2f} ₼/unit.",
        },
    },
}


def translate_action(action: dict, locale: str) -> dict:
    """Перевести title/detail в action-словаре на указанный locale.

    Использует структурные поля (type, product_name, competitor_site и т.д.)
    для реконструкции строк без перегенерации кэша.
    Неизвестный locale → возвращает оригинал (ru fallback).
    """
    strings = _STRINGS.get(locale)
    if strings is None or locale == "ru":
        return action  # ru — оригинал, другие неизвестные — без изменений

    action_type = action.get("type", "")
    tmpl = strings.get(action_type)
    if tmpl is None:
        return action

    name = action.get("product_name") or ""
    site = action.get("competitor_site") or ""
    current = float(action.get("current_value_azn") or 0)
    target = float(action.get("target_value_azn") or 0)
    pct = abs(float(action.get("spread_pct") or 0))
    delta = float(action.get("unit_gap_azn") or 0)

    try:
        if action_type == "undercut":
            comp = round(target + 0.01, 2)
            title = tmpl["title"].format(name=name)
            detail = tmpl["detail"].format(
                site=site, comp=comp, client=current, pct=pct, target=target
            )
        elif action_type == "price_raise":
            title = tmpl["title"].format(name=name)
            detail = tmpl["detail"].format(pct=pct, client=current, target=target, delta=abs(delta))
        elif action_type == "assortment_gap":
            title = tmpl["title"].format(name=name)
            detail = tmpl["detail"].format(site=site, price=current)
        elif action_type == "promo_response":
            raw_title = action.get("title", "")
            promo = raw_title.split(": ", 1)[1] if ": " in raw_title else raw_title
            title = tmpl["title"].format(site=site, promo=promo)
            detail = tmpl["detail"].format(site=site)
        elif action_type == "map_violation":
            extra = action.get("extra") or {}
            brand = extra.get("brand") or ""
            days = int(extra.get("floor_window_days") or _MAP_FLOOR_WINDOW_DAYS)
            title = tmpl["title"].format(brand=brand, name=name)
            detail = tmpl["detail"].format(
                brand=brand,
                days=days,
                target=target,
                client=current,
                pct=pct,
                delta=abs(delta),
            )
        else:
            return action
    except (KeyError, ValueError):
        return action  # fallback — не ломаем ответ

    result = dict(action)
    result["title"] = title
    result["detail"] = detail
    return result
