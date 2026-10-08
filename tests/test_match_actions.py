"""Тесты helper-операций для ручной коррекции матчей."""

import ast
import datetime
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from src import analytics, matcher
from src import match_actions as ma
from src.storage import Match, MatchPolicyAudit, MatchRejection, Product


def _make_product(s, **kw) -> Product:
    p = Product(
        tenant_id=kw.get("tenant_id", 1),
        site=kw.get("site", "pharmonline"),
        external_id=kw.get("external_id", "id-1"),
        url=kw.get("url", "http://example.com/p"),
        name=kw["name"],
        name_normalized=kw.get("name_normalized", kw["name"].lower()),
        canonical_id=kw.get("canonical_id"),
    )
    s.add(p)
    s.flush()
    return p


def _make_match_cluster(
    s, name: str, sites: list[str], *, tenant_id: int = 1
) -> tuple[Match, list[Product]]:
    m = Match(tenant_id=tenant_id, canonical_name=name, confidence=1.0, is_manual=False)
    s.add(m)
    s.flush()
    products = []
    for i, site in enumerate(sites):
        p = _make_product(
            s,
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{i}",
            name=name,
            canonical_id=m.id,
        )
        products.append(p)
    s.commit()
    return m, products


def test_add_rejection_normalizes_pair_order(db_session):
    """add_rejection(b, a) и (a, b) дают одну запись."""
    p1 = _make_product(db_session, name="Foo", external_id="1")
    p2 = _make_product(db_session, name="Bar", external_id="2")
    db_session.commit()

    r1 = ma.add_rejection(db_session, p1.id, p2.id)
    r2 = ma.add_rejection(db_session, p2.id, p1.id)
    assert r1.id == r2.id
    # min, max
    assert r1.product_a_id == min(p1.id, p2.id)
    assert r1.product_b_id == max(p1.id, p2.id)


def test_is_rejected_symmetric(db_session):
    p1 = _make_product(db_session, name="Foo", external_id="1")
    p2 = _make_product(db_session, name="Bar", external_id="2")
    db_session.commit()

    assert ma.is_rejected(db_session, p1.id, p2.id) is False
    ma.add_rejection(db_session, p1.id, p2.id, reason="test")
    db_session.commit()
    assert ma.is_rejected(db_session, p1.id, p2.id) is True
    assert ma.is_rejected(db_session, p2.id, p1.id) is True


def test_confirm_match_sets_is_manual(db_session):
    m, _ = _make_match_cluster(db_session, "Paracetamol", ["pharmonline", "aloe"])
    assert m.is_manual is False
    result = ma.confirm_match(db_session, m.id)
    assert result is not None
    assert result.is_manual is True


def test_break_match_detach_product_creates_rejections(db_session):
    """Break: detach один Product → rejection с КАЖДЫМ из остальных + clear canonical_id."""
    m, [p1, p2, p3] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )

    n_rejections = ma.break_match(db_session, m.id, p2.id, reason="wrong product")
    assert n_rejections == 2  # rejection между p2-p1 и p2-p3

    db_session.refresh(p2)
    assert p2.canonical_id is None
    # p1, p3 остались в кластере
    db_session.refresh(p1)
    db_session.refresh(p3)
    assert p1.canonical_id == m.id
    assert p3.canonical_id == m.id


def test_break_match_dissolves_cluster_if_only_one_left(db_session):
    m, [p1, p2] = _make_match_cluster(db_session, "Foo", ["pharmonline", "aloe"])
    match_id = m.id
    ma.break_match(db_session, match_id, p1.id)

    # Match удалён, оставшийся p2 тоже теряет canonical_id
    db_session.refresh(p2)
    assert p2.canonical_id is None
    assert db_session.get(Match, match_id) is None


# --- break_match: несколько отвязок от одного кластера в одной сессии ---------
#
# `db_session` собрана как `storage.make_session`: без autoflush и без
# expire_on_commit. Список участников кластера, прочитанный одной отвязкой,
# остаётся в сессии и после commit — вместе с товаром, который она отвязала.


def _stored_clusters(s) -> tuple[dict[int, int | None], set[int]]:
    """Привязки товаров и живые кластеры — как они лежат в базе, а не в сессии."""
    s.flush()
    pairing = dict(s.execute(select(Product.id, Product.canonical_id)).all())
    return pairing, set(s.scalars(select(Match.id)))


def _stored_rejections(s) -> set[tuple[int, int]]:
    s.flush()
    return set(s.execute(select(MatchRejection.product_a_id, MatchRejection.product_b_id)).all())


def _pair(a: Product, b: Product) -> tuple[int, int]:
    return (a.id, b.id) if a.id < b.id else (b.id, a.id)


def test_break_match_twice_in_one_session_dissolves_the_cluster(db_session):
    """Из кластера трёх товаров отвязаны два: остался один — кластера нет.

    Вторая отвязка считала первый товар оставшимся: писала с ним отказ ещё раз
    и видела «двоих оставшихся» — кластер из одного товара жил дальше.
    """
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )
    match_id = m.id

    assert ma.break_match(db_session, match_id, x.id) == 2
    assert ma.break_match(db_session, match_id, y.id) == 1

    assert _stored_clusters(db_session) == ({x.id: None, y.id: None, z.id: None}, set())
    assert _stored_rejections(db_session) == {_pair(x, y), _pair(x, z), _pair(y, z)}


def test_break_match_twice_in_one_session_keeps_a_cluster_of_two(db_session):
    """Из четырёх отвязаны два: кластер жив, а отказ с уже отвязанным не считается."""
    m, [x, y, z, w] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe", "fourth"]
    )

    assert ma.break_match(db_session, m.id, x.id) == 3
    assert ma.break_match(db_session, m.id, y.id) == 2

    assert _stored_clusters(db_session) == (
        {x.id: None, y.id: None, z.id: m.id, w.id: m.id},
        {m.id},
    )
    assert _stored_rejections(db_session) == {
        _pair(x, y),
        _pair(x, z),
        _pair(x, w),
        _pair(y, z),
        _pair(y, w),
    }


def test_break_match_leaves_the_current_members_in_the_session(db_session):
    """После отвязки вызывающий видит в `match.products` тех, кто остался."""
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )

    ma.break_match(db_session, m.id, x.id)

    assert {p.id for p in m.products} == {y.id, z.id}


def test_break_match_after_a_swap_in_the_same_session(db_session):
    """Состав менялся в этой сессии другой операцией — отвязка видит итог.

    Замена товара сайта список участников в сессии не обновляет: отказ
    записался бы с заменённым товаром, которого в кластере уже нет, а с
    пришедшим на его место — нет.
    """
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )
    z_new = _make_product(db_session, site="aloe", external_id="aloe-new", name="Aspirin")
    db_session.commit()
    assert ma.swap_alternative(db_session, m.id, "aloe", z_new.id) is True

    assert ma.break_match(db_session, m.id, x.id) == 2

    assert _stored_rejections(db_session) == {_pair(z, z_new), _pair(x, y), _pair(x, z_new)}
    assert _stored_clusters(db_session) == (
        {x.id: None, y.id: m.id, z.id: None, z_new.id: m.id},
        {m.id},
    )


def test_break_match_sees_a_member_detached_without_flush(db_session):
    """Состав читается из базы вместе с тем, что сессия ещё не записала."""
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )
    match_id = m.id
    x.canonical_id = None  # autoflush выключен: в базе x пока в кластере

    assert ma.break_match(db_session, match_id, y.id) == 1

    assert _stored_clusters(db_session) == ({x.id: None, y.id: None, z.id: None}, set())
    assert _stored_rejections(db_session) == {_pair(y, z)}


def test_break_match_counts_a_pair_that_was_rejected_before(db_session):
    """Число — пары, по которым отказ теперь в силе, а не новые строки.

    Отказ, существовавший до вызова, подтверждается (и включается снова, если
    был снят) — и входит в счёт наравне с созданным.
    """
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )
    ma.add_rejection(db_session, x.id, y.id, reason="earlier").is_active = False
    db_session.commit()

    assert ma.break_match(db_session, m.id, x.id) == 2

    assert _stored_rejections(db_session) == {_pair(x, y), _pair(x, z)}
    assert ma.is_rejected(db_session, x.id, y.id) is True


def test_find_alternatives_ranks_by_similarity(db_session):
    m, _ = _make_match_cluster(db_session, "Paracetamol 500mg", ["pharmonline", "aloe"])
    # Кандидаты на aptekonline (unmatched)
    closer = _make_product(
        db_session, site="aptekonline", external_id="ap-close", name="Paracetamol 500mg generic"
    )
    further = _make_product(
        db_session, site="aptekonline", external_id="ap-far", name="Aspirin Cardio 100mg"
    )
    matched_already = _make_product(
        db_session,
        site="aptekonline",
        external_id="ap-matched",
        name="Paracetamol other",
        canonical_id=m.id,  # уже сматченный — не должен попасть
    )
    db_session.commit()

    alts = ma.find_alternatives(db_session, m.id, "aptekonline")
    ids = [p.id for p, _score in alts]
    assert closer.id in ids
    assert further.id in ids
    assert matched_already.id not in ids
    # Closer должен быть выше further по score
    closer_pos = ids.index(closer.id)
    further_pos = ids.index(further.id)
    assert closer_pos < further_pos


def test_swap_alternative_replaces_product(db_session):
    m, [p1, p2] = _make_match_cluster(db_session, "Paracetamol", ["pharmonline", "aloe"])
    # Новый кандидат на aloe
    new_p = _make_product(
        db_session, site="aloe", external_id="aloe-new", name="Paracetamol Generic"
    )
    db_session.commit()

    ok = ma.swap_alternative(db_session, m.id, "aloe", new_p.id)
    assert ok is True

    db_session.refresh(new_p)
    db_session.refresh(p2)
    db_session.refresh(m)
    assert new_p.canonical_id == m.id
    assert p2.canonical_id is None
    assert m.is_manual is True

    # Между p2 (старый aloe) и new_p должен быть rejection
    assert ma.is_rejected(db_session, p2.id, new_p.id) is True


def test_swap_alternative_enforce_rejects_unknown_country(db_session, monkeypatch):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    match, products = _make_match_cluster(
        db_session, "Paracetamol", ["pharmonline", "aloe"]
    )
    for product in products:
        product.manufacturer_country_code = "rs"
        product.country_resolution_status = "resolved"
    candidate = _make_product(
        db_session,
        site="aloe",
        external_id="aloe-country-unknown",
        name="Paracetamol",
    )
    db_session.commit()

    assert ma.swap_alternative(db_session, match.id, "aloe", candidate.id) is False
    assert candidate.canonical_id is None


def test_swap_alternative_enforce_rejects_stale_offer(db_session, monkeypatch):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    match, products = _make_match_cluster(
        db_session, "Paracetamol", ["pharmonline", "aloe"]
    )
    now = datetime.datetime.utcnow()
    for product in products:
        product.offer_availability_status = "in_stock"
        product.availability_observed_at = now
    candidate = _make_product(
        db_session,
        site="aloe",
        external_id="aloe-stale",
        name="Paracetamol",
    )
    candidate.offer_availability_status = "in_stock"
    candidate.availability_observed_at = now - datetime.timedelta(days=10)
    db_session.commit()

    assert ma.swap_alternative(db_session, match.id, "aloe", candidate.id) is False
    assert candidate.canonical_id is None


# --- swap_alternative: состав кластера менялся в этой же сессии ----------------
#
# Тот же класс, что у break_match выше: запись в `canonical_id` список
# `match.products` не обновляет, и он переживает commit. Замена по такому списку
# считает «текущим товаром сайта» того, кого в кластере уже нет, и оставляет в
# кластере двух товаров одного сайта.


def test_swap_alternative_twice_in_one_session_keeps_one_product_per_site(db_session):
    """Вторая замена на том же сайте убирает товар, поставленный первой."""
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    n1 = _make_product(db_session, site="aloe", external_id="aloe-n1", name="Aspirin")
    n2 = _make_product(db_session, site="aloe", external_id="aloe-n2", name="Aspirin")
    db_session.commit()

    assert ma.swap_alternative(db_session, m.id, "aloe", n1.id) is True
    assert ma.swap_alternative(db_session, m.id, "aloe", n2.id) is True

    assert _stored_clusters(db_session) == (
        {x.id: m.id, z.id: None, n1.id: None, n2.id: m.id},
        {m.id},
    )
    assert _stored_rejections(db_session) == {_pair(z, n1), _pair(n1, n2)}


def test_swap_alternative_rereads_members_changed_by_another_writer(db_session):
    """Список прочитан раньше, а товар сайта привязан мимо него — замена это видит.

    Так пишут `dash_match_add_product` и матчер: прямо в `canonical_id`.
    """
    m, [x, y] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aptekonline"])
    n1 = _make_product(db_session, site="aloe", external_id="aloe-n1", name="Aspirin")
    n2 = _make_product(db_session, site="aloe", external_id="aloe-n2", name="Aspirin")
    db_session.commit()
    assert {p.id for p in m.products} == {x.id, y.id}
    n1.canonical_id = m.id
    db_session.commit()

    assert ma.swap_alternative(db_session, m.id, "aloe", n2.id) is True

    assert _stored_clusters(db_session) == (
        {x.id: m.id, y.id: m.id, n1.id: None, n2.id: m.id},
        {m.id},
    )
    assert _stored_rejections(db_session) == {_pair(n1, n2)}


def test_swap_alternative_sees_a_member_attached_without_flush(db_session):
    """Состав читается из базы вместе с тем, что сессия ещё не записала."""
    m, [x, y] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aptekonline"])
    n1 = _make_product(db_session, site="aloe", external_id="aloe-n1", name="Aspirin")
    n2 = _make_product(db_session, site="aloe", external_id="aloe-n2", name="Aspirin")
    db_session.commit()
    n1.canonical_id = m.id  # autoflush выключен: в базе n1 пока без кластера

    assert ma.swap_alternative(db_session, m.id, "aloe", n2.id) is True

    assert _stored_clusters(db_session) == (
        {x.id: m.id, y.id: m.id, n1.id: None, n2.id: m.id},
        {m.id},
    )
    assert _stored_rejections(db_session) == {_pair(n1, n2)}


def test_swap_alternative_leaves_the_current_members_in_the_session(db_session):
    """После замены вызывающий видит в `match.products` новый состав."""
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    n1 = _make_product(db_session, site="aloe", external_id="aloe-n1", name="Aspirin")
    db_session.commit()

    assert ma.swap_alternative(db_session, m.id, "aloe", n1.id) is True

    assert {p.id for p in m.products} == {x.id, n1.id}


# --- try_swap_alternative: почему замена не записана ---------------------------


def test_try_swap_alternative_names_the_reason_and_changes_nothing(db_session):
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    other_site = _make_product(db_session, site="aptekonline", external_id="ap", name="Aspirin")
    foreign = _make_product(
        db_session, tenant_id=2, site="aloe", external_id="aloe-foreign", name="Aspirin"
    )
    db_session.commit()
    before = _stored_clusters(db_session)

    def refusal(match_id: int, site: str, product_id: int) -> str | None:
        outcome = ma.try_swap_alternative(db_session, match_id, site, product_id)
        assert outcome.accepted is False
        # Булев вариант отвечает тем же: замены нет.
        assert ma.swap_alternative(db_session, match_id, site, product_id) is False
        return outcome.reason

    assert refusal(m.id + 1000, "aloe", z.id) == "match_not_found"
    assert refusal(m.id, "aloe", z.id + 1000) == "product_not_found"
    assert refusal(m.id, "aloe", other_site.id) == "product_site_mismatch"
    assert refusal(m.id, "aloe", foreign.id) == "product_tenant_mismatch"
    assert refusal(m.id, "aloe", z.id) == "already_current"

    assert _stored_clusters(db_session) == before
    assert _stored_rejections(db_session) == set()
    assert db_session.scalar(select(Match.is_manual)) is False


def test_try_swap_alternative_names_the_member_that_blocks_the_swap(db_session):
    """Отказ из-за другого товара кластера называет этот товар, а не кандидата."""
    m, [x, y, z] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )
    y.offer_availability_status = "out_of_stock"
    candidate = _make_product(db_session, site="aloe", external_id="aloe-new", name="Aspirin")
    db_session.commit()

    outcome = ma.try_swap_alternative(db_session, m.id, "aloe", candidate.id)

    assert outcome == ma.SwapOutcome(False, f"offer:out_of_stock product={y.id} site=aptekonline")
    assert candidate.canonical_id is None


def test_try_swap_alternative_names_a_country_conflict(db_session):
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    candidate = _make_product(db_session, site="aloe", external_id="aloe-new", name="Aspirin")
    for product, code in ((x, "tr"), (candidate, "az")):
        product.manufacturer_country_code = code
        product.country_resolution_status = "resolved"
    db_session.commit()

    outcome = ma.try_swap_alternative(db_session, m.id, "aloe", candidate.id)

    assert outcome == ma.SwapOutcome(False, "identity:country_conflict")


def test_try_swap_alternative_dry_run_checks_without_writing(db_session):
    """Пробный вызов отвечает тем же, что настоящий, и ничего не записывает."""
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    candidate = _make_product(db_session, site="aloe", external_id="aloe-new", name="Aspirin")
    db_session.commit()
    before = _stored_clusters(db_session)

    would_apply = ma.try_swap_alternative(db_session, m.id, "aloe", candidate.id, dry_run=True)
    refused = ma.try_swap_alternative(db_session, m.id, "aloe", z.id, dry_run=True)

    assert would_apply == ma.SwapOutcome(True)
    assert refused == ma.SwapOutcome(False, "already_current")
    db_session.rollback()  # несохранённого после пробного вызова быть не должно
    assert _stored_clusters(db_session) == before
    assert _stored_rejections(db_session) == set()
    assert db_session.scalar(select(Match.is_manual)) is False


@pytest.mark.parametrize("dry_run", [False, True])
def test_try_swap_alternative_takes_the_matcher_lock_in_both_modes(
    db_session, monkeypatch, dry_run
):
    """Пробный ответ верен только для состава, который в эту секунду никто не меняет."""
    m, [x, z] = _make_match_cluster(db_session, "Aspirin", ["pharmonline", "aloe"])
    candidate = _make_product(db_session, site="aloe", external_id="aloe-new", name="Aspirin")
    db_session.commit()
    seen_before_lock: list[bool] = []
    monkeypatch.setattr(
        ma,
        "_acquire_match_mutation_xact_lock",
        # Замок — раньше чтения состава: список участников ещё не загружен.
        lambda session: seen_before_lock.append("products" in m.__dict__),
    )

    outcome = ma.try_swap_alternative(db_session, m.id, "aloe", candidate.id, dry_run=dry_run)

    assert outcome.accepted is True
    # Настоящая замена берёт замок ещё раз, записывая отказ прежнему товару сайта.
    assert seen_before_lock[0] is False and len(seen_before_lock) == (1 if dry_run else 2)


def test_list_rejections_for_product(db_session):
    p1 = _make_product(db_session, name="A", external_id="1")
    p2 = _make_product(db_session, name="B", external_id="2")
    p3 = _make_product(db_session, name="C", external_id="3")
    db_session.commit()

    ma.add_rejection(db_session, p1.id, p2.id)
    ma.add_rejection(db_session, p1.id, p3.id)
    db_session.commit()

    rej_for_p1 = ma.list_rejections_for_product(db_session, p1.id)
    assert sorted(rej_for_p1) == sorted([p2.id, p3.id])
    rej_for_p2 = ma.list_rejections_for_product(db_session, p2.id)
    assert rej_for_p2 == [p1.id]


# ── Тенант отказа — тенант товаров пары ──────────────────────────────────────
# Колонка match_rejections.tenant_id по умолчанию равна 1. Пока add_rejection её
# не задавал, отказ любого тенанта доставался первому: читатель с фильтром по
# тенанту (analytics.match_quality) показывал его не тому.


def _tenant_pair(s, tenant_id: int, tag: str) -> tuple[Product, Product]:
    left = _make_product(
        s, tenant_id=tenant_id, site="pharmonline", external_id=f"{tag}-ph", name=f"{tag} left"
    )
    right = _make_product(
        s, tenant_id=tenant_id, site="aloe", external_id=f"{tag}-aloe", name=f"{tag} right"
    )
    s.commit()
    return left, right


def _rejection_tenants(s) -> list[int]:
    """Тенанты всех отказов — как они лежат в базе, а не в объектах сессии."""
    s.flush()
    return sorted(s.scalars(select(MatchRejection.tenant_id)))


def test_add_rejection_takes_tenant_from_the_pair(db_session):
    own_left, own_right = _tenant_pair(db_session, 1, "own")
    foreign_left, foreign_right = _tenant_pair(db_session, 2, "foreign")

    own = ma.add_rejection(db_session, own_left.id, own_right.id)
    foreign = ma.add_rejection(db_session, foreign_right.id, foreign_left.id)
    db_session.commit()

    by_id = dict(db_session.execute(select(MatchRejection.id, MatchRejection.tenant_id)).all())
    assert by_id == {own.id: 1, foreign.id: 2}
    # Читатель с фильтром по тенанту видит отказ у хозяина пары и только у него.
    assert analytics.match_quality(db_session, tenant_id=1).rejected_pairs == 1
    assert analytics.match_quality(db_session, tenant_id=2).rejected_pairs == 1


def _stored(s, rejection_id: int) -> tuple:
    """Строка отказа из базы: (тенант, причина, тип, активна, когда снята)."""
    s.commit()
    return tuple(
        s.execute(
            select(
                MatchRejection.tenant_id,
                MatchRejection.reason,
                MatchRejection.reason_type,
                MatchRejection.is_active,
                MatchRejection.resolved_at,
            ).where(MatchRejection.id == rejection_id)
        ).one()
    )


def test_add_rejection_repeat_and_reactivation_keep_the_pair_tenant(db_session):
    left, right = _tenant_pair(db_session, 2, "foreign")
    first = ma.add_rejection(db_session, left.id, right.id, reason="first")
    db_session.commit()

    again = ma.add_rejection(db_session, right.id, left.id, reason="second")

    assert again.id == first.id
    assert _stored(db_session, first.id) == (2, "first", "manual", True, None)

    # Откат системного отказа гасит строку, новое нарушение её возвращает.
    first.is_active = False
    first.resolved_at = datetime.datetime(2026, 10, 7)
    db_session.commit()

    revived = ma.add_rejection(
        db_session, left.id, right.id, reason="third", reason_type="system_spec"
    )

    assert revived.id == first.id
    assert _stored(db_session, first.id) == (2, "third", "system_spec", True, None)
    assert _rejection_tenants(db_session) == [2]


def test_add_rejection_corrects_a_row_written_with_the_default_tenant(db_session):
    """Строка прежнего кода (тенант по умолчанию) при повторном отказе получает тенант пары."""
    left, right = _tenant_pair(db_session, 2, "foreign")
    a_id, b_id = sorted((left.id, right.id))
    db_session.add(MatchRejection(product_a_id=a_id, product_b_id=b_id, reason="old"))
    db_session.commit()
    assert _rejection_tenants(db_session) == [1]

    rejection = ma.add_rejection(db_session, left.id, right.id, reason="new")

    assert rejection.reason == "old"  # активная запись: причина остаётся прежней
    assert _rejection_tenants(db_session) == [2]


def test_add_rejection_skips_a_pair_across_tenants(db_session):
    """Матчер сводит товары только внутри тенанта: такой отказ ничего не запрещает
    и не принадлежал бы ни одному из двух тенантов."""
    own = _make_product(db_session, name="Own", external_id="own")
    foreign = _make_product(
        db_session, tenant_id=2, site="aloe", name="Foreign", external_id="foreign"
    )
    db_session.commit()

    with capture_logs() as seen:
        assert ma.add_rejection(db_session, own.id, foreign.id) is None

    assert _rejection_tenants(db_session) == []
    assert ma.is_rejected(db_session, own.id, foreign.id) is False
    # Журнал — единственный след того, что в кластере оказался чужой товар.
    assert seen == [
        {
            "event": "rejection_cross_tenant_skipped",
            "log_level": "error",
            "product_ids": [own.id, foreign.id],
            "tenant_ids": [1, 2],
        }
    ]


def test_add_rejection_refuses_an_unknown_product(db_session):
    known = _make_product(db_session, name="Known", external_id="known")
    db_session.commit()

    with pytest.raises(ValueError, match="not found"):
        ma.add_rejection(db_session, known.id, known.id + 1000)

    assert _rejection_tenants(db_session) == []


def test_break_match_writes_rejections_for_the_cluster_tenant(db_session):
    m, [_p1, p2, _p3] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"], tenant_id=2
    )

    assert ma.break_match(db_session, m.id, p2.id) == 2

    assert _rejection_tenants(db_session) == [2, 2]


def test_swap_alternative_writes_rejection_for_the_cluster_tenant(db_session):
    m, [_p1, p2] = _make_match_cluster(
        db_session, "Paracetamol", ["pharmonline", "aloe"], tenant_id=2
    )
    new_p = _make_product(
        db_session, tenant_id=2, site="aloe", external_id="aloe-new", name="Paracetamol Generic"
    )
    db_session.commit()

    assert ma.swap_alternative(db_session, m.id, "aloe", new_p.id) is True

    assert ma.is_rejected(db_session, p2.id, new_p.id) is True
    assert _rejection_tenants(db_session) == [2]


def test_revalidate_split_writes_rejection_for_the_cluster_tenant(db_session):
    m = Match(tenant_id=2, canonical_name="combo", confidence=0.9, is_manual=False)
    db_session.add(m)
    db_session.flush()
    d3 = _make_product(
        db_session,
        tenant_id=2,
        site="pharmonline",
        external_id="c1",
        name="Venatura Vitamin D3 20 ml",
        name_normalized="venatura vitamin d3",
        canonical_id=m.id,
    )
    d3k2 = _make_product(
        db_session,
        tenant_id=2,
        site="aptekonline",
        external_id="c2",
        name="Venatura Vitamin D3 K2 20 ml",
        name_normalized="venatura vitamin d3 k2",
        canonical_id=m.id,
    )
    db_session.commit()

    actions = matcher.revalidate_split(db_session, tenant_id=2)

    assert [action["action"] for action in actions] == ["dissolve"]
    assert ma.is_rejected(db_session, d3.id, d3k2.id) is True
    assert _rejection_tenants(db_session) == [2]


def test_reject_endpoint_writes_rejections_for_the_match_tenant(db_session):
    from src import api as api_module

    m, _products = _make_match_cluster(
        db_session, "Ibuprofen", ["pharmonline", "aptekonline", "aloe"], tenant_id=2
    )
    match_id = m.id

    response = api_module.dash_match_reject(
        match_id, user=SimpleNamespace(id=7, tenant_id=2), db=db_session
    )

    assert response.status_code == 204
    assert db_session.get(Match, match_id) is None
    assert _rejection_tenants(db_session) == [2, 2, 2]
    assert analytics.match_quality(db_session, tenant_id=2).rejected_pairs == 3
    assert analytics.match_quality(db_session, tenant_id=1).rejected_pairs == 0


# Кластера с товаром чужого тенанта быть не должно (matcher._persist_match такой
# не создаст). Если он всё же есть, отклонение, отвязка и перепроверка в конце
# сбора работают с ним как до появления тенанта у отказов: не падают, отказы
# пишут только парам одного тенанта. Чужой товар, который по характеристикам ни
# с кем не конфликтует, перепроверка из кластера не убирает — как и раньше.


def _cluster_with_foreign_member(s, names: list[str]) -> tuple[Match, list[Product]]:
    """Пара тенанта 1 (pharmonline, aptekonline, …), последний товар — тенанта 2."""
    sites = ["pharmonline", "aptekonline", "aloe"][: len(names)]
    m = Match(tenant_id=1, canonical_name=names[0], confidence=0.9, is_manual=False)
    s.add(m)
    s.flush()
    products = [
        _make_product(
            s,
            tenant_id=2 if index == len(names) - 1 else 1,
            site=site,
            external_id=f"mixed-{site}",
            name=name,
            canonical_id=m.id,
        )
        for index, (site, name) in enumerate(zip(sites, names, strict=True))
    ]
    s.commit()
    return m, products


def test_reject_endpoint_dismantles_a_cluster_with_a_foreign_member(db_session):
    from src import api as api_module

    m, [own_a, own_b, foreign] = _cluster_with_foreign_member(db_session, ["Ibuprofen"] * 3)
    match_id = m.id

    response = api_module.dash_match_reject(
        match_id, user=SimpleNamespace(id=7, tenant_id=1), db=db_session
    )

    assert response.status_code == 204
    assert db_session.get(Match, match_id) is None
    linked = db_session.scalar(
        select(func.count(Product.id)).where(Product.canonical_id.is_not(None))
    )
    assert linked == 0
    assert ma.is_rejected(db_session, own_a.id, own_b.id) is True
    assert _rejection_tenants(db_session) == [1]


def test_break_match_detaches_a_foreign_member(db_session):
    m, [own_a, own_b, foreign] = _cluster_with_foreign_member(db_session, ["Ibuprofen"] * 3)

    assert ma.break_match(db_session, m.id, foreign.id) == 0

    pairing = dict(db_session.execute(select(Product.id, Product.canonical_id)).all())
    assert pairing == {own_a.id: m.id, own_b.id: m.id, foreign.id: None}
    assert _rejection_tenants(db_session) == []


def test_revalidate_split_dismantles_a_cluster_with_a_foreign_member(db_session):
    m, [own, foreign] = _cluster_with_foreign_member(
        db_session, ["Venatura Vitamin D3 20 ml", "Venatura Vitamin D3 K2 20 ml"]
    )
    match_id = m.id

    actions = matcher.revalidate_split(db_session)

    assert [action["action"] for action in actions] == ["dissolve"]
    assert db_session.get(Match, match_id) is None
    pairing = dict(db_session.execute(select(Product.id, Product.canonical_id)).all())
    assert pairing == {own.id: None, foreign.id: None}
    assert _rejection_tenants(db_session) == []
    audit = db_session.scalar(select(MatchPolicyAudit).where(MatchPolicyAudit.match_id == match_id))
    assert (audit.action, audit.payload["rejections"]) == ("spec_dissolve", [])


def test_rejections_are_written_only_by_add_rejection():
    """Тенант отказа задаётся в одном месте — второй писатель обошёл бы его.

    Ловит вызов конструктора `MatchRejection(...)` где угодно, кроме
    `add_rejection`, и сырой `INSERT INTO match_rejections`. Не ловит запись через
    Core (`insert(MatchRejection)`), конструктор под другим именем и скрипт,
    который копирует таблицы, не называя их.
    """
    root = Path(__file__).resolve().parent.parent
    raw_insert = re.compile(r"insert\s+into\s+(\w+\.)?\"?match_rejections", re.I)
    constructors: list[str] = []
    raw_inserts: list[str] = []
    for folder in ("src", "scripts", "migrations"):
        for path in sorted((root / folder).rglob("*")):
            if path.suffix not in {".py", ".sql", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8")
            relative = str(path.relative_to(root))
            if raw_insert.search(text):
                raw_inserts.append(relative)
            if path.suffix != ".py":
                continue

            def visit(node: ast.AST, function: str | None, relative: str = relative) -> None:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    function = node.name
                if isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "MatchRejection")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "MatchRejection")
                ):
                    constructors.append(f"{relative} ({function})")
                for child in ast.iter_child_nodes(node):
                    visit(child, function)

            visit(ast.parse(text), None)

    assert constructors == ["src/match_actions.py (add_rejection)"]
    assert raw_inserts == []


# ── relink_dead_members (swap мёртвого члена кластера на живую альтернативу) ──
_DEAD = datetime.datetime(2026, 5, 30, 17, 0, 0)


def _mk(s, **kw) -> Product:
    p = Product(
        site=kw["site"],
        external_id=kw["external_id"],
        url=kw.get("url", "http://x/" + kw["external_id"]),
        name=kw["name"],
        name_normalized=kw.get("name_normalized", kw["name"].lower()),
        canonical_id=kw.get("canonical_id"),
        pack_size=kw.get("pack_size"),
        url_dead_at=kw.get("url_dead_at"),
        manufacturer=kw.get("manufacturer"),
    )
    s.add(p)
    s.flush()
    return p


def _cluster_with_dead(s, *, manual=False):
    m = Match(canonical_name="Asiklovir 200 mq N20", confidence=1.0, is_manual=manual)
    s.add(m)
    s.flush()
    anchor = _mk(
        s,
        site="pharmonline",
        external_id="ph1",
        name="Asiklovir 200 mq N20",
        canonical_id=m.id,
        pack_size="N20",
    )
    dead = _mk(
        s,
        site="aptekonline",
        external_id="ap-dead",
        name="Asiklovir Terapiya 200 mq N20",
        canonical_id=m.id,
        pack_size="N20",
        url_dead_at=_DEAD,
    )
    return m, anchor, dead


def test_relink_dead_swaps_live_alternative(db_session):
    m, _anchor, dead = _cluster_with_dead(db_session)
    live = _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )  # живой, unmatched
    db_session.commit()
    res = matcher.relink_dead_members(db_session)
    assert any(r["action"] == "swap" and r["new"] == live.id and r["old"] == dead.id for r in res)
    db_session.refresh(live)
    db_session.refresh(dead)
    assert live.canonical_id == m.id  # живой подвязан
    assert dead.canonical_id is None  # мёртвый отвязан


def test_relink_dead_skips_wrong_pack(db_session):
    _cluster_with_dead(db_session)
    # единственный живой кандидат — другая упаковка (N25) → НЕ подменять
    _mk(
        db_session,
        site="aptekonline",
        external_id="ap-n25",
        name="Asiklovir 200 mq N25",
        pack_size="N25",
    )
    db_session.commit()
    res = matcher.relink_dead_members(db_session)
    assert all(r["action"] != "swap" for r in res)


def test_relink_dead_dry_run_no_change(db_session):
    _cluster_with_dead(db_session)
    live = _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()
    res = matcher.relink_dead_members(db_session, dry_run=True)
    assert any(r["action"] == "swap" for r in res)  # план показывает swap
    db_session.refresh(live)
    assert live.canonical_id is None  # но БД не тронута
    db_session.rollback()
    assert _stored_rejections(db_session) == set()
    assert db_session.scalar(select(Match.is_manual)) is False


def test_relink_dead_skips_manual_cluster(db_session):
    _cluster_with_dead(db_session, manual=True)
    live = _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()
    res = matcher.relink_dead_members(db_session)
    assert res == []  # ручной кластер не трогаем
    db_session.refresh(live)
    assert live.canonical_id is None


def _cluster_with_two_dead(s) -> tuple[Match, Product, Product, Product, Product]:
    """Мёртвые товары на двух сайтах и по живому кандидату на каждый."""
    m, _anchor, dead_apt = _cluster_with_dead(s)
    dead_aloe = _mk(
        s,
        site="aloe",
        external_id="al-dead",
        name="Asiklovir 200 mq N20",
        canonical_id=m.id,
        pack_size="N20",
        url_dead_at=_DEAD,
    )
    live_apt = _mk(
        s, site="aptekonline", external_id="ap-live", name="Asiklovir 200 mq N20", pack_size="N20"
    )
    live_aloe = _mk(
        s, site="aloe", external_id="al-live", name="Asiklovir 200 mq N20", pack_size="N20"
    )
    s.commit()
    return m, dead_apt, dead_aloe, live_apt, live_aloe


@pytest.mark.parametrize("dry_run", [False, True])
def test_relink_dead_reports_a_refused_swap_with_its_reason(db_session, dry_run):
    """Замену не пропустила проверка самой замены — в плане это не `swap`.

    Кандидат на каждый сайт есть, но второй мёртвый товар кластера делает любую
    замену недопустимой. Раньше обе строки шли как `swap`, а команда печатала
    «applied 2». Пробный прогон отвечает тем же, что настоящий.
    """
    m, dead_apt, dead_aloe, live_apt, live_aloe = _cluster_with_two_dead(db_session)
    before = _stored_clusters(db_session)

    res = matcher.relink_dead_members(db_session, dry_run=dry_run)

    assert sorted(res, key=lambda r: r["site"]) == [
        {
            "match_id": m.id,
            "site": "aloe",
            "old": dead_aloe.id,
            "new": live_aloe.id,
            "score": 100,
            "action": "swap-rejected",
            "reason": f"offer:dead_url product={dead_apt.id} site=aptekonline",
        },
        {
            "match_id": m.id,
            "site": "aptekonline",
            "old": dead_apt.id,
            "new": live_apt.id,
            "score": 100,
            "action": "swap-rejected",
            "reason": f"offer:dead_url product={dead_aloe.id} site=aloe",
        },
    ]
    assert _stored_clusters(db_session) == before
    assert _stored_rejections(db_session) == set()
    assert db_session.scalar(select(Match.is_manual)) is False


def test_relink_dead_applied_swap_carries_no_reason(db_session):
    m, _anchor, dead = _cluster_with_dead(db_session)
    live = _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()

    res = matcher.relink_dead_members(db_session)

    assert res == [
        {
            "match_id": m.id,
            "site": "aptekonline",
            "old": dead.id,
            "new": live.id,
            "score": 100,
            "action": "swap",
        }
    ]
    assert _stored_clusters(db_session)[0][live.id] == m.id


@pytest.mark.parametrize("dry_run", [False, True])
def test_relink_dead_gives_one_candidate_to_one_cluster(db_session, dry_run):
    """Кандидат один, кластеров с мёртвым товаром два — замена одна в обоих режимах.

    Пробный прогон обещал кандидата обоим кластерам: он ничего не записывает, и
    для второго кластера кандидат всё ещё выглядел свободным.
    """
    first, _anchor, _dead = _cluster_with_dead(db_session)
    second = _second_cluster_with_dead(db_session)
    live = _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()

    res = matcher.relink_dead_members(db_session, dry_run=dry_run)

    assert [(r["match_id"], r["action"], r["new"]) for r in res] == [
        (first.id, "swap", live.id),
        (second.id, "skip-no-live-alt", None),
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_relink_dead_refused_candidate_stays_available(db_session, dry_run):
    """Кандидат, чью замену отклонили, достаётся следующему кластеру — в обоих режимах."""
    refused, dead_apt, _dead_aloe, live_apt, _live_aloe = _cluster_with_two_dead(db_session)
    other = _second_cluster_with_dead(db_session)
    db_session.commit()

    res = matcher.relink_dead_members(db_session, dry_run=dry_run)

    aptekonline = [(r["match_id"], r["action"], r["new"]) for r in res if r["site"] == "aptekonline"]
    assert sorted(aptekonline) == [
        (refused.id, "swap-rejected", live_apt.id),
        (other.id, "swap", live_apt.id),
    ]


def _second_cluster_with_dead(s) -> Match:
    """Ещё один кластер с тем же названием и мёртвым товаром aptekonline."""
    m = Match(canonical_name="Asiklovir 200 mq N20", confidence=1.0, is_manual=False)
    s.add(m)
    s.flush()
    for site, tag, dead_at in (("pharmonline", "ph2", None), ("aptekonline", "ap-dead2", _DEAD)):
        _mk(
            s,
            site=site,
            external_id=tag,
            name="Asiklovir 200 mq N20",
            canonical_id=m.id,
            pack_size="N20",
            url_dead_at=dead_at,
        )
    return m


@pytest.mark.parametrize("dry_run", [False, True])
def test_relink_dead_window_skips_candidates_that_are_already_given(db_session, dry_run):
    """Окно в 25 кандидатов считается среди свободных — одинаково в обоих режимах.

    Подходящих кандидатов два, между ними 24 товара с тем же названием и другой
    фасовкой. Первый кластер берёт первого; для второго настоящий прогон первого
    уже не видит и доходит до второго кандидата. Пробный прогон первого видел
    (он ничего не записал), окно кончалось раньше второго — «замены нет».
    """
    first, _anchor, _dead = _cluster_with_dead(db_session)
    second = _second_cluster_with_dead(db_session)

    def candidate(tag: str, pack: str) -> Product:
        return _mk(
            db_session,
            site="aptekonline",
            external_id=tag,
            name="Asiklovir 200 mq N20",
            pack_size=pack,
        )

    nearest = candidate("ap-live-1", "N20")
    for index in range(24):
        candidate(f"ap-other-pack-{index}", "N25")
    beyond = candidate("ap-live-26", "N20")
    db_session.commit()

    res = matcher.relink_dead_members(db_session, dry_run=dry_run)

    assert [(r["match_id"], r["action"], r["new"]) for r in res] == [
        (first.id, "swap", nearest.id),
        (second.id, "swap", beyond.id),
    ]


def test_relink_dead_checks_the_candidate_against_every_live_member(db_session):
    """Кандидат, несовместимый хотя бы с одним товаром кластера, не подходит.

    Сверка шла с одним товаром — первым в списке участников. Здесь первым лежит
    тот, у кого в названии нет состава: с ним D3+K2 не спорит, а с D3 — спорит.
    Замена сводила D3 с D3+K2 в кластере, который после неё считается ручным.
    """
    m = Match(canonical_name="Venatura Vitamin D3 20 ml", confidence=1.0, is_manual=False)
    db_session.add(m)
    db_session.flush()
    for site, name, normalized, dead_at in (
        ("aptekonline", "Venatura Vitamin 20 ml", "venatura vitamin", None),
        ("pharmonline", "Venatura Vitamin D3 20 ml", "venatura vitamin d3", None),
        ("aloe", "Venatura Vitamin D3 20 ml", "venatura vitamin d3", _DEAD),
    ):
        _mk(
            db_session,
            site=site,
            external_id=f"{site}-member",
            name=name,
            name_normalized=normalized,
            canonical_id=m.id,
            url_dead_at=dead_at,
        )
    _mk(
        db_session,
        site="aloe",
        external_id="aloe-d3-k2",
        name="Venatura Vitamin D3 K2 20 ml",
        name_normalized="venatura vitamin d3 k2",
    )
    db_session.commit()

    res = matcher.relink_dead_members(db_session)

    assert [r["action"] for r in res] == ["skip-no-live-alt"]
    assert db_session.scalar(select(Match.is_manual)) is False


def test_relink_dead_looks_at_the_25_most_similar_candidates_only(db_session):
    """Подходящий кандидат за окном не рассматривается — как и до правки окна."""
    _cluster_with_dead(db_session)
    for index in range(25):
        _mk(
            db_session,
            site="aptekonline",
            external_id=f"ap-other-pack-{index}",
            name="Asiklovir 200 mq N20",
            pack_size="N25",
        )
    _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live-26",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()

    res = matcher.relink_dead_members(db_session)

    assert [r["action"] for r in res] == ["skip-no-live-alt"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_relink_dead_leaves_a_cluster_with_two_products_of_the_site(db_session, dry_run):
    """На сайте мёртвого товара в кластере есть ещё один — замены нет.

    Замена не выбирает, какой товар сайта убрать: убранным оказывался живой (с
    постоянным отказом), мёртвый оставался, а в плане значилась замена мёртвого.
    """
    m, _anchor, dead = _cluster_with_dead(db_session)
    _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live-member",
        name="Asiklovir 200 mq N20",
        canonical_id=m.id,
        pack_size="N20",
    )
    _mk(
        db_session,
        site="aptekonline",
        external_id="ap-live",
        name="Asiklovir 200 mq N20",
        pack_size="N20",
    )
    db_session.commit()
    before = _stored_clusters(db_session)

    res = matcher.relink_dead_members(db_session, dry_run=dry_run)

    assert res == [
        {
            "match_id": m.id,
            "site": "aptekonline",
            "old": dead.id,
            "new": None,
            "score": None,
            "action": "skip-several-on-site",
        }
    ]
    assert _stored_clusters(db_session) == before
    assert _stored_rejections(db_session) == set()

