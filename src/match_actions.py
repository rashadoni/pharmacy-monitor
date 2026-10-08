"""Helper-операции для ручной коррекции матчей.

Используется UI Сравнения цен для трёх действий:
- ✓ Подтвердить матч (защита от пересматчивания)
- ✗ Не один товар (отвязать Product от кластера + создать MatchRejection)
- 🔍 Заменить (swap Product на другой кандидат с того же сайта)
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src._time import utcnow
from src.match_lock import acquire_match_mutation_xact_lock
from src.storage import Match, MatchRejection, Product

log = structlog.get_logger()


def _ordered(a: int, b: int) -> tuple[int, int]:
    """Канонический порядок пары: (min, max). Делает таблицу симметричной."""
    return (a, b) if a < b else (b, a)


def _pair_tenant_id(session: Session, product_a_id: int, product_b_id: int) -> int | None:
    """Тенант отказа — тенант товаров пары; None, если товары из разных тенантов.

    У колонки ``match_rejections.tenant_id`` значение по умолчанию 1: отказ,
    записанный без тенанта, достаётся первому тенанту, чья бы пара ни была.
    Тенант однозначно следует из товаров, поэтому у вызывающих его не спрашиваем
    — забыть его передать нельзя.
    """
    tenants = dict(
        session.execute(
            select(Product.id, Product.tenant_id).where(
                Product.id.in_((product_a_id, product_b_id))
            )
        ).all()
    )
    missing = sorted({product_a_id, product_b_id} - tenants.keys())
    if missing:
        raise ValueError(f"Cannot reject pair: product(s) {missing} not found")
    if tenants[product_a_id] != tenants[product_b_id]:
        log.error(
            "rejection_cross_tenant_skipped",
            product_ids=[product_a_id, product_b_id],
            tenant_ids=[tenants[product_a_id], tenants[product_b_id]],
        )
        return None
    return tenants[product_a_id]


def add_rejection(
    session: Session,
    product_a_id: int,
    product_b_id: int,
    reason: str | None = None,
    *,
    reason_type: str = "manual",
    metadata: dict | None = None,
) -> MatchRejection | None:
    """Создать (или вернуть существующую) запись отрицания пары.

    Запись получает тенант товаров пары. ``MatchRejection`` создаётся только
    здесь: второй писатель обошёл бы тенант (вызов конструктора в другом месте
    ловит ``tests/test_match_actions.py``).

    Для пары товаров из разных тенантов записи нет и возвращается None: матчер
    сводит товары только внутри тенанта, так что запрещать нечего, а строка не
    принадлежала бы ни одному из двух. Исключения нет намеренно: отклонение,
    отвязка и перепроверка кластера, в котором оказался чужой товар (его быть
    не должно), обязаны дойти до конца. След — событие
    ``rejection_cross_tenant_skipped`` в журнале.
    """
    acquire_match_mutation_xact_lock(session)
    if product_a_id == product_b_id:
        raise ValueError("Cannot reject pair with self")
    a, b = _ordered(product_a_id, product_b_id)
    tenant_id = _pair_tenant_id(session, a, b)
    if tenant_id is None:
        return None
    existing = session.scalar(
        select(MatchRejection).where(
            MatchRejection.product_a_id == a, MatchRejection.product_b_id == b
        )
    )
    if existing:
        # Active rows are idempotent evidence: preserve the original reason
        # and metadata on repeated calls.  A previously rolled-back system row
        # may be reactivated by a later independent violation; in that case the
        # new evidence intentionally replaces the resolved record's metadata.
        if not existing.is_active:
            existing.reason = reason or existing.reason
            existing.reason_type = reason_type
            existing.metadata_json = metadata
        existing.is_active = True
        existing.resolved_at = None
        existing.updated_at = utcnow()
        # Пара у записи одна, поэтому искать её по тенанту незачем. Строка,
        # записанная до появления тенанта у отказов, получает его здесь.
        existing.tenant_id = tenant_id
        return existing
    rej = MatchRejection(
        tenant_id=tenant_id,
        product_a_id=a,
        product_b_id=b,
        reason=reason,
        reason_type=reason_type,
        metadata_json=metadata,
        is_active=True,
    )
    session.add(rej)
    session.flush()
    return rej


def is_rejected(session: Session, product_a_id: int, product_b_id: int) -> bool:
    """Проверить — помечена ли пара как анти-матч."""
    if product_a_id == product_b_id:
        return False
    a, b = _ordered(product_a_id, product_b_id)
    return (
        session.scalar(
            select(MatchRejection.id).where(
                MatchRejection.product_a_id == a,
                MatchRejection.product_b_id == b,
                MatchRejection.is_active.is_(True),
            )
        )
        is not None
    )


def confirm_match(session: Session, match_id: int) -> Match | None:
    """Пометить Match как ручной — auto-matcher больше его не тронет."""
    acquire_match_mutation_xact_lock(session)
    m = session.get(Match, match_id)
    if not m:
        return None
    m.is_manual = True
    session.commit()
    log.info("match_confirmed", match_id=match_id)
    return m


def break_match(
    session: Session,
    match_id: int,
    detach_product_id: int,
    reason: str | None = None,
) -> int:
    """Отвязать конкретный Product от Match-кластера.

    - Создаёт MatchRejection между detach_product и каждым из оставшихся в кластере
    - Снимает canonical_id с detach_product
    - Если в кластере остался <2 Product'ов — Match удаляется

    Возвращает число пар «отвязанный — оставшийся», по которым отказ теперь в
    силе: запись создана, включена снова или уже была (тогда она подтверждена).
    Пара товаров из разных тенантов записи не получает и в счёт не входит.
    """
    acquire_match_mutation_xact_lock(session)
    m = session.get(Match, match_id)
    if not m:
        return 0
    detach = session.get(Product, detach_product_id)
    if not detach or detach.canonical_id != match_id:
        log.warning("break_match_skip", reason="product_not_in_match", product=detach_product_id)
        return 0

    # Состав кластера читаем из базы, а не из сессии. Сессии проекта не
    # сбрасывают объекты после commit, а запись в `canonical_id` список
    # `m.products` не меняет: товар, отвязанный или заменённый в этой же сессии,
    # в нём остаётся. По такому списку вторая отвязка пишет отказ с товаром,
    # которого в кластере уже нет, и не удаляет кластер, в котором остался один.
    # flush — чтобы чтение увидело и то, что сессия ещё не записала (autoflush
    # выключен).
    session.flush()
    session.expire(m, ["products"])
    remaining = [p for p in m.products if p.id != detach_product_id]
    rej_count = 0
    for other in remaining:
        if add_rejection(session, detach_product_id, other.id, reason=reason or "manual break"):
            rej_count += 1

    detach.canonical_id = None
    session.flush()

    # Если осталось <2 продукта в кластере — Match теряет смысл
    if len(remaining) < 2:
        for p in remaining:
            p.canonical_id = None
        session.delete(m)
        log.info("match_dissolved", match_id=match_id, reason="cluster_too_small")
    else:
        # Список в сессии снова устарел на отвязанный товар: следующий читатель
        # (вызывающий, другая операция над кластером) перечитает его из базы.
        session.expire(m, ["products"])
        log.info(
            "match_partial_break",
            match_id=match_id,
            detached=detach_product_id,
            remaining=len(remaining),
        )

    session.commit()
    return rej_count


def find_alternatives(
    session: Session, match_id: int, site: str, limit: int | None = 50
) -> list[tuple[Product, int]]:
    """Найти unmatched Product'ы на site, отсортированные по похожести на canonical_name.

    Возвращает список (Product, score) где score 0..100 от rapidfuzz.token_set_ratio.
    ``limit=None`` — весь список: похожесть считается для всех кандидатов в любом
    случае, предел только обрезает результат.
    """
    m = session.get(Match, match_id)
    if not m:
        return []
    # Порядок по id: при равной похожести кандидаты идут одинаково от вызова к
    # вызову, а не как их вернула база.
    candidates = session.scalars(
        select(Product)
        .where(
            Product.tenant_id == m.tenant_id,
            Product.site == site,
            Product.canonical_id.is_(None),
            Product.offer_availability_status != "out_of_stock",
        )
        .order_by(Product.id)
    ).all()
    if not candidates:
        return []

    scored = []
    name = m.canonical_name or ""
    for p in candidates:
        score = fuzz.token_set_ratio(name, p.name or "")
        scored.append((p, int(score)))
    scored.sort(key=lambda t: -t[1])
    return scored[:limit]


def swap_alternative(session: Session, match_id: int, site: str, new_product_id: int) -> bool:
    """Заменить Product этого site в Match на другой.

    - Существующий Product этого site → отвязывается + rejection с new_product
      (если их в кластере несколько, чего быть не должно, — отвязываются все)
    - Новый Product получает canonical_id = match_id

    False — замена не записана; почему, говорит `try_swap_alternative`.
    """
    return try_swap_alternative(session, match_id, site, new_product_id).accepted


@dataclass(frozen=True)
class SwapOutcome:
    """Итог `try_swap_alternative`.

    ``accepted`` — замена прошла проверки. Без ``dry_run`` это значит, что она
    записана и закоммичена; с ``dry_run`` — что была бы.
    ``reason`` — почему нет; начинается с кода: ``match_not_found``,
    ``product_not_found``, ``product_site_mismatch``, ``product_tenant_mismatch``,
    ``product_in_other_match match=<id>`` (товар уже стоит в другом кластере —
    замена его оттуда не уводит), ``identity:<код>``,
    ``offer:<код> product=<id> site=<сайт>`` (товар, из-за которого отказ),
    ``already_current``.
    """

    accepted: bool
    reason: str | None = None


def try_swap_alternative(
    session: Session,
    match_id: int,
    site: str,
    new_product_id: int,
    *,
    dry_run: bool = False,
) -> SwapOutcome:
    """То же, что `swap_alternative`, но с причиной отказа.

    Отказ ничего не меняет: кластер и товары остаются как были. При ``dry_run``
    не меняет ничего и согласие. Замок сопоставления берётся и при ``dry_run``:
    ответ верен только для состава, который в эту секунду никто не меняет.

    Всё, по чему замена решает, она читает из базы сама, под замком: кластер,
    нового товара и состав. Вызывающий мог прочитать их раньше — до ожидания
    замка или в начале долгой работы, — а сессии проекта не сбрасывают объекты
    после commit: `session.get` отдал бы прочитанное тогда. По нему замена
    уводила товар из пары, в которую его успел свести этап сопоставления, и
    писала в кластер, которого уже нет.
    """
    acquire_match_mutation_xact_lock(session)
    # flush — раньше чтения: оно берёт строки из базы и затёрло бы то, что сессия
    # изменила, но ещё не записала (autoflush выключен).
    session.flush()
    # Чтение кластера из базы заодно сбрасывает его список `m.products` в сессии:
    # после вызова — и после отказа тоже — следующий читатель получит состав из
    # базы, а не прочитанный когда-то раньше.
    m = session.get(Match, match_id, populate_existing=True)
    if not m:
        return SwapOutcome(False, "match_not_found")
    new_p = session.get(Product, new_product_id, populate_existing=True)
    if not new_p:
        return SwapOutcome(False, "product_not_found")
    if new_p.site != site:
        return SwapOutcome(False, "product_site_mismatch")
    if new_p.tenant_id != m.tenant_id:
        return SwapOutcome(False, "product_tenant_mismatch")
    # Товар из другого кластера замена не берёт: там осталась бы пара без него —
    # возможно, из одного товара. Сначала его отвязывают там.
    if new_p.canonical_id is not None and new_p.canonical_id != match_id:
        return SwapOutcome(False, f"product_in_other_match match={new_p.canonical_id}")

    from src.product_policy import (
        policy_identity_eligibility,
        policy_offer_eligibility,
    )

    # Состав — запросом, а не списком `m.products` из сессии: тот не знает о
    # товаре, привязанном или отвязанном после его чтения (причина та же, что в
    # break_match). По такому списку «текущим товаром сайта» оказывался тот, кого
    # в кластере уже нет, а настоящий оставался рядом с новым. Запрос заодно
    # обновляет и сами товары: наличие и мёртвую ссылку проверки ниже смотрят у
    # каждого. По id: от порядка зависело бы, какой из двух непригодных товаров
    # назван причиной отказа.
    members = session.scalars(
        select(Product)
        .where(Product.canonical_id == match_id)
        .order_by(Product.id)
        .execution_options(populate_existing=True)
    ).all()

    cohort = [product for product in members if product.site != site] + [new_p]
    identity = policy_identity_eligibility(cohort)
    if not identity.eligible:
        return SwapOutcome(False, f"identity:{identity.reason}")
    for product in cohort:
        offer = policy_offer_eligibility(product)
        if not offer.eligible:
            return SwapOutcome(
                False, f"offer:{offer.reason} product={product.id} site={product.site}"
            )

    # Товары этого сайта, которым новый приходит на смену. Обычно такой один.
    # Если их несколько (быть не должно: один товар сайта на кластер), уходят
    # все: оператор назвал товар этого сайта для сравнения, и после замены на
    # сайте должен остаться он один. Убирать одного из нескольких — оставить
    # кластер таким же неисправным, причём какого именно, решал бы порядок id.
    replaced = [p for p in members if p.site == site and p.id != new_product_id]
    if new_p.canonical_id == match_id and not replaced:
        return SwapOutcome(False, "already_current")
    if dry_run:
        return SwapOutcome(True)

    for old in replaced:
        # Создаём rejection между прежним и новым (чтобы matcher не вернул)
        add_rejection(session, old.id, new_product_id, reason="manual swap")
        old.canonical_id = None

    new_p.canonical_id = match_id
    # Помечаем match как manual чтобы auto-matcher не пересматчил
    m.is_manual = True
    session.commit()
    log.info(
        "match_swapped",
        match_id=match_id,
        site=site,
        old=[p.id for p in replaced],
        new=new_product_id,
    )
    return SwapOutcome(True)


def list_rejections_for_product(session: Session, product_id: int) -> list[int]:
    """Список product_id'ов с которыми этот product НЕ должен матчиться."""
    rows = session.scalars(
        select(MatchRejection).where(
            MatchRejection.is_active.is_(True),
            or_(
                MatchRejection.product_a_id == product_id,
                MatchRejection.product_b_id == product_id,
            )
        )
    ).all()
    out = []
    for r in rows:
        out.append(r.product_b_id if r.product_a_id == product_id else r.product_a_id)
    return out
