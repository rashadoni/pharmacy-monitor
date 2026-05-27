"""Phase 5.1 (Вариант C) — тесты intraday category rotation.

Покрывает:
  - top_volatile_categories: ранжирует категории по count(price_snapshots) за 7д
  - next_rotation_pick: stub Redis INCR, проверка wraparound по модулю
  - acquire_site_lock: SETNX atomic semantics, fail-open если Redis недоступен
  - pick_next_scrape_target: end-to-end orchestration с моками

Redis замокан unittest.mock.MagicMock для контролируемого поведения.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from src import intraday, storage
from src._time import utcnow


# ─── Helpers для setup ────────────────────────────────────────────────────────


def _add_category(
    session, key, label, ph_slug=None, apt_slug=None, aloe_slug=None
) -> storage.Category:
    c = storage.Category(
        key=key,
        label_ru=label,
        pharmonline_slug=ph_slug,
        aptekonline_slug=apt_slug,
        aloe_slug=aloe_slug,
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


def _add_product_with_category(
    session, site, ext_id, name, category_slug
) -> storage.Product:
    p = storage.Product(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        category=category_slug,
    )
    session.add(p)
    session.flush()
    return p


def _add_snaps(session, product, n_snaps, when=None):
    """Создать N snapshots для продукта (опционально с custom captured_at)."""
    when = when or utcnow()
    run = storage.Run(status="ok")
    session.add(run)
    session.flush()
    for i in range(n_snaps):
        session.add(storage.PriceSnapshot(
            run_id=run.id, product_id=product.id, price=10.0 + i,
            captured_at=when,
        ))
    session.flush()


# ─── top_volatile_categories ──────────────────────────────────────────────────


def test_top_volatile_categories_ranks_by_snapshot_count(db_session):
    """Категории с большим числом price-snapshots за 7д идут первыми."""
    # Active cat A — много snapshots, B — мало, C — нет
    a = _add_category(db_session, "a", "A", ph_slug="cat-a")
    b = _add_category(db_session, "b", "B", ph_slug="cat-b")
    c = _add_category(db_session, "c", "C", ph_slug="cat-c")

    pa = _add_product_with_category(db_session, "pharmonline", "pa", "ProdA", "cat-a")
    pb = _add_product_with_category(db_session, "pharmonline", "pb", "ProdB", "cat-b")
    # C без снапшотов

    _add_snaps(db_session, pa, n_snaps=10)
    _add_snaps(db_session, pb, n_snaps=2)
    db_session.commit()

    result = intraday.top_volatile_categories(db_session, n=5)
    assert len(result) == 2
    assert result[0].id == a.id, "A должна быть первой (больше snapshots)"
    assert result[1].id == b.id
    # C отсутствует — у неё нет ни одного snapshot
    assert c.id not in [r.id for r in result]


def test_top_volatile_categories_respects_n_limit(db_session):
    """n=2 → возвращает только 2 топовых."""
    for i in range(5):
        cat = _add_category(db_session, f"cat{i}", f"Label{i}", ph_slug=f"slug{i}")
        p = _add_product_with_category(db_session, "pharmonline", f"p{i}", f"P{i}", f"slug{i}")
        _add_snaps(db_session, p, n_snaps=10 - i)  # decreasing volatility
    db_session.commit()

    result = intraday.top_volatile_categories(db_session, n=2)
    assert len(result) == 2
    assert result[0].key == "cat0"  # самая volatile
    assert result[1].key == "cat1"


def test_top_volatile_categories_skips_old_snapshots(db_session):
    """Snapshots старше VOLATILITY_WINDOW_DAYS не учитываются."""
    a = _add_category(db_session, "a", "A", ph_slug="cat-a")
    pa = _add_product_with_category(db_session, "pharmonline", "pa", "ProdA", "cat-a")
    # старый snapshot — 30 дней назад, > VOLATILITY_WINDOW_DAYS
    _add_snaps(db_session, pa, n_snaps=10, when=utcnow() - timedelta(days=30))
    db_session.commit()

    result = intraday.top_volatile_categories(db_session)
    assert result == [], "старые snapshots не должны попадать в volatility"


def test_top_volatile_categories_returns_empty_when_no_data(db_session):
    """Нет ни одного snapshot → пустой list (graceful)."""
    _add_category(db_session, "a", "A", ph_slug="cat-a")
    db_session.commit()
    assert intraday.top_volatile_categories(db_session) == []


# ─── next_rotation_pick ──────────────────────────────────────────────────────


def test_next_rotation_pick_increments_atomically():
    """INCR вызывается, возвращается categories[(idx-1) % len]."""
    redis_mock = MagicMock()
    redis_mock.incr.side_effect = [1, 2, 3, 4]  # 4 sequential calls

    fake_cats = [MagicMock(id=i) for i in [10, 11, 12]]

    picks = [intraday.next_rotation_pick(redis_mock, fake_cats) for _ in range(4)]
    assert [p.id for p in picks] == [10, 11, 12, 10], "round-robin с wraparound"

    # Каждый INCR должен установить expire (TTL = 90д)
    assert redis_mock.expire.call_count == 4


def test_next_rotation_pick_returns_none_on_empty():
    """Empty list → None, INCR не вызывается."""
    redis_mock = MagicMock()
    assert intraday.next_rotation_pick(redis_mock, []) is None
    redis_mock.incr.assert_not_called()


def test_next_rotation_pick_returns_none_when_redis_none():
    """Redis None → None (graceful, не падаем)."""
    fake_cats = [MagicMock(id=1)]
    assert intraday.next_rotation_pick(None, fake_cats) is None


def test_next_rotation_pick_returns_none_on_redis_error():
    """INCR raises → silent None."""
    redis_mock = MagicMock()
    redis_mock.incr.side_effect = Exception("connection lost")
    fake_cats = [MagicMock(id=1)]
    assert intraday.next_rotation_pick(redis_mock, fake_cats) is None


# ─── acquire_site_lock ───────────────────────────────────────────────────────


def test_acquire_site_lock_returns_true_when_setnx_succeeds():
    """Redis SET nx=True вернул "OK" — lock acquired."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = True
    assert intraday.acquire_site_lock(redis_mock, "pharmonline") is True
    redis_mock.set.assert_called_once_with(
        "intraday:lock:site:pharmonline", "1",
        nx=True, ex=intraday.INTRADAY_PER_SITE_MIN_GAP_SEC,
    )


def test_acquire_site_lock_returns_false_when_already_locked():
    """SET nx=True вернул None (уже есть) — отказ."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = None
    assert intraday.acquire_site_lock(redis_mock, "pharmonline") is False


def test_acquire_site_lock_fails_open_on_redis_none():
    """Redis недоступен → fail-open (best-effort, без rate-limit)."""
    assert intraday.acquire_site_lock(None, "pharmonline") is True


def test_acquire_site_lock_fails_open_on_redis_exception():
    """Любая Redis exception → True (fail-open)."""
    redis_mock = MagicMock()
    redis_mock.set.side_effect = Exception("network")
    assert intraday.acquire_site_lock(redis_mock, "pharmonline") is True


# ─── pick_next_scrape_target end-to-end ───────────────────────────────────────


def test_pick_next_scrape_target_returns_site_and_cat(db_session):
    """Happy path: volatile cat есть, Redis работает, lock acquired."""
    cat = _add_category(db_session, "k1", "K1", ph_slug="ph-cat", aloe_slug="aloe-cat")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph-cat")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.incr.return_value = 1  # picks cats[0]
    redis_mock.set.return_value = True  # lock acquired

    result = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert result is not None
    site, picked_cat = result
    assert picked_cat.id == cat.id
    assert site == "pharmonline"  # первый в INTRADAY_SITES order


def test_pick_next_scrape_target_falls_back_to_aloe_when_pharm_locked(db_session):
    """Pharmonline locked → пытаемся aloe."""
    cat = _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.incr.return_value = 1
    # SET nx returns: None для pharmonline (locked), True для aloe (acquired)
    redis_mock.set.side_effect = [None, True]
    redis_mock.ttl.return_value = 3600

    result = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert result is not None
    site, _ = result
    assert site == "aloe"


def test_pick_next_scrape_target_returns_none_when_all_locked(db_session):
    """Все сайты locked → None."""
    cat = _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.incr.return_value = 1
    redis_mock.set.return_value = None  # все locked
    redis_mock.ttl.return_value = 3600

    result = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert result is None


def test_pick_next_scrape_target_returns_none_when_no_volatile_cats(db_session):
    """0 категорий с volatility → None."""
    _add_category(db_session, "k1", "K1", ph_slug="ph")  # без snapshots
    db_session.commit()

    redis_mock = MagicMock()
    result = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert result is None


def test_pick_next_scrape_target_skips_sites_without_slug(db_session):
    """Категория без pharmonline_slug → берём только aloe."""
    cat = _add_category(db_session, "k1", "K1", aloe_slug="aloe-only")
    # snapshots должны быть привязаны к aloe (slug-matching)
    p = _add_product_with_category(db_session, "aloe", "p1", "P1", "aloe-only")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.incr.return_value = 1
    redis_mock.set.return_value = True

    result = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert result is not None
    site, _ = result
    assert site == "aloe"  # pharmonline pomp пропущен — нет slug


def test_pick_next_scrape_target_commit_false_does_not_mutate_redis(db_session):
    """commit_state=False (dry-run) → НЕТ INCR и НЕТ SETNX."""
    _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = b"0"  # текущий idx
    redis_mock.exists.return_value = 0  # lock free

    result = intraday.pick_next_scrape_target(
        db_session, redis_client=redis_mock, commit_state=False
    )
    assert result is not None
    site, _ = result
    assert site == "pharmonline"

    # State не изменился: НЕ должны вызваться INCR/SET/EXPIRE
    redis_mock.incr.assert_not_called()
    redis_mock.set.assert_not_called()
    redis_mock.expire.assert_not_called()
    # Read-only ops — OK
    redis_mock.get.assert_called()
    redis_mock.exists.assert_called()


def test_pick_next_scrape_target_commit_false_detects_existing_lock(db_session):
    """В preview mode locked-сайт пропускается через EXISTS, не SET."""
    _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = b"0"
    # pharmonline lock exists, aloe free
    redis_mock.exists.side_effect = lambda k: 1 if "pharmonline" in k else 0
    redis_mock.ttl.return_value = 1234

    result = intraday.pick_next_scrape_target(
        db_session, redis_client=redis_mock, commit_state=False
    )
    assert result is not None
    site, _ = result
    assert site == "aloe"
    redis_mock.set.assert_not_called()
