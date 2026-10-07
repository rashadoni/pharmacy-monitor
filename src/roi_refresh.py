"""Пересчёт кэша рекомендаций ROI без нового сбора.

Кэш (`storage.RoiActionsCache`) пишет конец подтверждённого полного сбора в
`run_cmd`, а полный сбор идёт раз в неделю (`src/cadence.py`). Между сборами
рекомендации меняет не только каталог: пороги, себестоимость, правка пары.
Раньше такое изменение ждало следующей ночи, теперь ждало бы до недели.

Здесь две вещи:

* `refresh_from_trusted_epoch` — посчитать кэш заново от последней
  подтверждённой эпохи каталога. Проверки доверия те же, что перед записью кэша
  в конце сбора, и ни одна не обходится: нет подтверждённой эпохи — нет записи.
* очередь заявок (`storage.RoiRefreshRequest`): эндпоинт, меняющий входы,
  кладёт заявку в ту же транзакцию, серверный watcher раз в минуту зовёт
  `pharmacy-monitor roi refresh --pending`.

Считать в HTTP-обработчике по-прежнему нельзя (15–30 с на сайт, см. «Persistent
cache» в `src/roi.py`) — поэтому очередь, а не фоновая задача в процессе API.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Literal

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src import roi, storage
from src._time import utcnow
from src.cadence import site_max_age_hours
from src.run_lock import try_exclusive_roi_refresh_lock, try_shared_scrape_read_lock
from src.storage import RoiRefreshRequest, Run

log = structlog.get_logger()

Outcome = Literal["refreshed", "idle", "busy", "untrusted", "failed"]

# Чем закрывается заявка при каждом исходе. `busy` заявку не закрывает: сбор
# закончится, блокировка освободится — следующий тик повторит.
_REQUEST_STATUS_BY_OUTCOME = {
    "refreshed": "done",
    # Эпоха не подтверждена — считать не от чего, и повтор ничего не изменит:
    # доверие возвращает только подтверждённый полный сбор, а он в конце сам
    # пересчитывает кэш по текущим порогам, себестоимости и парам.
    "untrusted": "skipped",
    "failed": "failed",
}

# Закрытые заявки нужны только для разбора «что и когда пересчитывалось».
_CLOSED_REQUEST_RETENTION_DAYS = 30

# Заявка, которую упавший пересчёт ставит сам себе — ровно один раз.
_RETRY_REASON = "retry_after_failure"

# Изменения, после которых прежние рекомендации показывать нельзя вовсе: кэш
# сбрасывается вместе с заявкой (`drop_cache_and_request_refresh`). Правка пары
# сюда не входит — после неё прежний список остаётся виден до пересчёта.
REASON_PRICING_CONFIG = "pricing_config"
REASON_COST_IMPORT = "cost_import"
REASON_COST_IMPORT_ROLLBACK = "cost_import_rollback"
CACHE_DROPPING_REASONS = frozenset(
    {REASON_PRICING_CONFIG, REASON_COST_IMPORT, REASON_COST_IMPORT_ROLLBACK}
)


@dataclass(frozen=True)
class RefreshResult:
    outcome: Outcome
    # Машинная причина для busy / untrusted / failed.
    reason: str | None = None
    # Полный прогон, от эпохи которого посчитан кэш.
    run_id: int | None = None
    # {сайт: число рекомендаций}; -1 — пересчёт среза упал.
    counts: dict[str, int] = field(default_factory=dict)
    requests_closed: int = 0


def request_refresh(session: Session, *, tenant_id: int, reason: str) -> None:
    """Положить заявку на пересчёт в транзакцию вызывающего. Не коммитит.

    Звать ДО коммита самого изменения, чтобы заявка и изменение стали видимы
    одновременно. Заявка, видимая раньше изменения, была бы закрыта пересчётом,
    который изменения ещё не видел.
    """
    session.add(
        RoiRefreshRequest(
            tenant_id=tenant_id,
            reason=reason[:40],
            status="pending",
            requested_at=utcnow(),
        )
    )


def drop_cache_and_request_refresh(session: Session, *, tenant_id: int, reason: str) -> None:
    """Убрать рекомендации, посчитанные по старым входам, и заказать новые.

    Обе записи идут в транзакцию вызывающего и коммитятся вместе с самим
    изменением. Причина обязана быть из `CACHE_DROPPING_REASONS`: по ней пересчёт,
    шедший в момент изменения, узнаёт, что его результат уже устарел.
    """
    if reason not in CACHE_DROPPING_REASONS:
        raise ValueError(f"not a cache-dropping reason: {reason!r}")
    session.query(storage.RoiActionsCache).filter(
        storage.RoiActionsCache.tenant_id == tenant_id
    ).delete()
    request_refresh(session, tenant_id=tenant_id, reason=reason)


def has_pending_request(session: Session, *, tenant_id: int) -> bool:
    return (
        session.scalar(
            select(RoiRefreshRequest.id)
            .where(
                RoiRefreshRequest.tenant_id == tenant_id,
                RoiRefreshRequest.status == "pending",
            )
            .limit(1)
        )
        is not None
    )


def tenants_with_pending_requests(session: Session) -> list[int]:
    return sorted(
        session.scalars(
            select(RoiRefreshRequest.tenant_id)
            .where(RoiRefreshRequest.status == "pending")
            .distinct()
        ).all()
    )


def refresh_from_trusted_epoch(session: Session, *, tenant_id: int = 1) -> RefreshResult:
    """Пересчитать кэш всех срезов от последней подтверждённой эпохи каталога.

    Новый Run не создаётся. Каталог читается под shared-блокировкой сбора:
    пока она держится, ни один сбор не начнёт писать, а если сбор уже идёт —
    пересчёт не начинается вовсе (`busy`).
    """
    with try_exclusive_roi_refresh_lock(session) as only_refresher:
        if not only_refresher:
            return RefreshResult("busy", reason="another_refresh_running")
        with try_shared_scrape_read_lock(session) as catalog_readable:
            if not catalog_readable:
                return RefreshResult("busy", reason="scrape_in_progress")
            return _refresh_locked(session, tenant_id=tenant_id)


def _refresh_locked(session: Session, *, tenant_id: int) -> RefreshResult:
    from src.product_policy import policy_rollout_eligibility, trusted_catalog_epoch

    # Проверки ниже повторяют те, на которых расчёт отказывается сам
    # (`roi.RecommendationsNotComputed`). Здесь они затем, чтобы назвать исход
    # до расчёта: незавершённый прогон — подождать, нет доверия — закрыть заявку.
    if storage.has_unfinished_run(session, tenant_id=tenant_id):
        return RefreshResult("busy", reason="run_unfinished")

    anchor = _latest_full_attempt(session, tenant_id=tenant_id)
    if (
        anchor is None
        or not storage.run_is_financially_eligible(anchor)
        or not roi.financial_inputs_are_fresh(session, tenant_id=tenant_id)
        or trusted_catalog_epoch(session, tenant_id=tenant_id) is None
    ):
        return RefreshResult("untrusted", reason=_untrusted_reason(session, tenant_id=tenant_id))
    rollout = policy_rollout_eligibility(session, tenant_id=tenant_id)
    if not rollout.eligible:
        return RefreshResult("untrusted", reason=f"policy_gate:{rollout.reason}")

    try:
        counts = roi.refresh_all_cached_actions(session, run_id=anchor.id, tenant_id=tenant_id)
    except roi.RecommendationsNotComputed as not_computed:
        # Проверки выше прошли, а расчёт отказался: что-то переменилось посреди
        # него (вход перешагнул порог свежести между срезами). Это не сбой:
        # заявку не закрываем и повтор не ставим — следующий тик пройдёт те же
        # проверки заново и назовёт исход точно. Но часть срезов уже переписана,
        # а часть осталась от прошлого расчёта; такой кэш не оставляем, как и
        # при сбое ниже.
        _drop_cache(session, tenant_id=tenant_id)
        session.commit()
        return RefreshResult("busy", reason=not_computed.reason)
    except Exception as exc:
        # Ошибка уровня базы посреди расчёта (оборванное соединение, прерванная
        # транзакция) обходит разбор «срез упал» внутри
        # `refresh_all_cached_actions`: тот чистит кэш в той же, уже непригодной
        # транзакции и падает сам. Срезы, записанные до сбоя, новые, остальные —
        # от прошлого расчёта; устаревший ответ не оставляем, как и там.
        # Ловим только сам расчёт: сбой до него (блокировки, проверки доверия)
        # ничего не записал, и исправный кэш трогать незачем.
        session.rollback()
        log.exception("roi_refresh_crashed", tenant_id=tenant_id, run_id=anchor.id)
        _drop_cache(session, tenant_id=tenant_id)
        session.commit()
        return RefreshResult("failed", reason=f"error:{type(exc).__name__}", run_id=anchor.id)
    failed_sites = sorted(site for site, count in counts.items() if count < 0)
    if failed_sites:
        log.error(
            "roi_refresh_failed",
            tenant_id=tenant_id,
            run_id=anchor.id,
            failed_sites=failed_sites,
        )
        return RefreshResult(
            "failed",
            reason="sites:" + ",".join(failed_sites),
            run_id=anchor.id,
            counts=counts,
        )
    log.info("roi_cache_refreshed_without_scrape", tenant_id=tenant_id, run_id=anchor.id, **counts)
    return RefreshResult("refreshed", run_id=anchor.id, counts=counts)


def _drop_cache(session: Session, *, tenant_id: int) -> None:
    session.execute(
        delete(storage.RoiActionsCache).where(storage.RoiActionsCache.tenant_id == tenant_id)
    )


def _latest_full_attempt(session: Session, *, tenant_id: int) -> Run | None:
    """Полный прогон, которым подписывается кэш: самый поздний по завершению.

    Читатель (`roi.get_cached_actions_snapshot`) считает кэш вытесненным, если
    у любого сайта есть полная попытка, завершённая позже прогона кэша. Значит,
    подписать кэш можно только самой поздней из последних попыток — и только
    если она сама подтверждена.
    """
    attempts = storage.latest_full_catalog_attempts_by_site(
        session, roi.ALL_SITES, tenant_id=tenant_id
    )
    if set(attempts) != set(roi.ALL_SITES):
        return None
    return max(
        attempts.values(),
        key=lambda run: (run.finished_at or run.started_at, run.id),
    )


def _untrusted_reason(session: Session, *, tenant_id: int) -> str:
    """Какой сайт не даёт эпохи — для журнала и строки заявки, не для решения."""
    attempts = storage.latest_full_catalog_attempts_by_site(
        session, roi.ALL_SITES, tenant_id=tenant_id
    )
    now = utcnow()
    blockers: list[str] = []
    for site in roi.ALL_SITES:
        attempt = attempts.get(site)
        if attempt is None:
            blockers.append(f"{site}:no_full_scan")
        elif not storage.run_is_financially_eligible(attempt):
            blockers.append(f"{site}:run={attempt.id},status={attempt.status}")
        elif now - attempt.started_at > timedelta(hours=site_max_age_hours(site)):
            blockers.append(f"{site}:run={attempt.id},stale")
    return ";".join(blockers) or "inputs_not_fresh"


def _inputs_dropped_during_refresh(
    session: Session, *, tenant_id: int, seen_request_ids: list[int]
) -> bool:
    stmt = select(RoiRefreshRequest.id).where(
        RoiRefreshRequest.tenant_id == tenant_id,
        RoiRefreshRequest.status == "pending",
        RoiRefreshRequest.reason.in_(CACHE_DROPPING_REASONS),
    )
    if seen_request_ids:
        stmt = stmt.where(RoiRefreshRequest.id.not_in(seen_request_ids))
    return session.scalar(stmt.limit(1)) is not None


def run_refresh(
    session: Session,
    *,
    tenant_id: int = 1,
    only_if_requested: bool = False,
) -> RefreshResult:
    """Пересчитать кэш и закрыть заявки, которые этот пересчёт покрыл.

    `only_if_requested` — режим watcher'а: нет заявок — нет работы. Без него
    пересчёт идёт всегда (ручной запуск).
    """
    # Набор, а не «все с id не больше X»: id выдаётся при вставке, а видимой
    # заявка становится при коммите. Транзакция, которая вставила раньше, а
    # закоммитила позже, попала бы под X, хотя пересчёт её изменения не видел.
    requests = session.execute(
        select(RoiRefreshRequest.id, RoiRefreshRequest.reason).where(
            RoiRefreshRequest.tenant_id == tenant_id,
            RoiRefreshRequest.status == "pending",
        )
    ).all()
    request_ids = [request.id for request in requests]
    if only_if_requested and not request_ids:
        return RefreshResult("idle")

    result = refresh_from_trusted_epoch(session, tenant_id=tenant_id)

    if result.outcome == "failed" and any(request.reason != _RETRY_REASON for request in requests):
        # Один повтор: сбой мог быть разовым. Повтор самого повтора не ставится —
        # устойчивую поломку показывает health-check (`roi_refresh_failed`).
        request_refresh(session, tenant_id=tenant_id, reason=_RETRY_REASON)

    if result.outcome in ("refreshed", "failed") and _inputs_dropped_during_refresh(
        session, tenant_id=tenant_id, seen_request_ids=request_ids
    ):
        # Пока шёл расчёт, пороги или себестоимость сменили и кэш сбросили — а
        # расчёт дописал срезы по старым значениям поверх сброса. Убираем их:
        # до следующего тика честный ответ — «пересчитываются». (Остаётся окно
        # в миллисекунды между DELETE и коммитом эндпоинта; его закрывает тот
        # же следующий тик — заявка в нём не теряется.)
        _drop_cache(session, tenant_id=tenant_id)

    status = _REQUEST_STATUS_BY_OUTCOME.get(result.outcome)
    if status is None:
        log.info(
            "roi_refresh_deferred",
            tenant_id=tenant_id,
            reason=result.reason,
            pending=len(request_ids),
        )
        return result

    closed = 0
    if request_ids:
        closed = session.execute(
            update(RoiRefreshRequest)
            .where(
                RoiRefreshRequest.id.in_(request_ids),
                RoiRefreshRequest.status == "pending",
            )
            .values(
                status=status,
                completed_at=utcnow(),
                run_id=result.run_id,
                detail=result.reason,
            )
        ).rowcount
    session.execute(
        delete(RoiRefreshRequest).where(
            RoiRefreshRequest.tenant_id == tenant_id,
            RoiRefreshRequest.status != "pending",
            RoiRefreshRequest.completed_at
            < utcnow() - timedelta(days=_CLOSED_REQUEST_RETENTION_DAYS),
        )
    )
    session.commit()
    if result.outcome == "untrusted":
        log.warning(
            "roi_refresh_skipped_untrusted",
            tenant_id=tenant_id,
            reason=result.reason,
            requests_closed=closed,
        )
    return replace(result, requests_closed=closed)
