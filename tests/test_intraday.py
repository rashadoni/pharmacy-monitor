"""Phase 5.1 (Вариант C) — тесты intraday category rotation.

Покрывает:
  - top_volatile_categories: ранжирует категории по count(price_snapshots) за 7д
  - указатель ротации: чтение без сдвига, сдвиг, wraparound по модулю
  - acquire_site_lock: SETNX atomic semantics, fail-open если Redis недоступен
  - pick_next_scrape_target: end-to-end orchestration — ротация только по
    категориям, которые тик может обслужить, и причина пропуска

Redis замокан unittest.mock.MagicMock для контролируемого поведения; там, где
важна последовательность тиков, — _FakeRedis с состоянием в памяти.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import click
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import intraday, storage
from src import main as main_mod
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


def _add_product_with_category(session, site, ext_id, name, category_slug) -> storage.Product:
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
        session.add(
            storage.PriceSnapshot(
                run_id=run.id,
                product_id=product.id,
                price=10.0 + i,
                captured_at=when,
            )
        )
    session.flush()


class _FakeRedis:
    """Redis в памяти: ровно те команды, что зовёт intraday.

    TTL не истекает сам — «прошло два часа» в тесте это `delete` замка.
    """

    def __init__(self):
        self.data: dict[str, object] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.data.get(key)

    def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def ttl(self, key):
        return self.ttls.get(key, -1) if key in self.data else -2

    def exists(self, key):
        return int(key in self.data)

    def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)


ROTATION_KEY = "intraday:rotation:idx"
ALOE_LOCK = "intraday:lock:site:aloe"


def _add_volatile_category(session, key, site, slug, n_snaps) -> storage.Category:
    """Категория с разделом на одном сайте и n_snaps изменений цены за окно."""
    slug_kw = {"pharmonline": "ph_slug", "aptekonline": "apt_slug", "aloe": "aloe_slug"}[site]
    cat = _add_category(session, key, key.upper(), **{slug_kw: slug})
    product = _add_product_with_category(session, site, f"p-{key}", f"P {key}", slug)
    _add_snaps(session, product, n_snaps=n_snaps)
    return cat


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


# ─── Указатель ротации ───────────────────────────────────────────────────────


def test_rotation_pointer_walks_the_list_and_wraps():
    """Чтение + сдвиг дают round-robin с wraparound."""
    redis = _FakeRedis()
    fake_cats = [MagicMock(id=i) for i in [10, 11, 12]]

    picks = []
    for _ in range(4):
        picks.append(intraday._peek_rotation_pick(redis, fake_cats).id)
        intraday._advance_rotation(redis)

    assert picks == [10, 11, 12, 10], "round-robin с wraparound"
    # Каждый сдвиг продлевает TTL ключа (90д)
    assert redis.ttls[ROTATION_KEY] == 90 * 24 * 3600


def test_peek_rotation_pick_does_not_move_the_pointer():
    """Сколько ни читай — очередь остаётся у той же категории."""
    redis = _FakeRedis()
    redis.data[ROTATION_KEY] = b"4"  # настоящий Redis отдаёт bytes
    fake_cats = [MagicMock(id=i) for i in [10, 11, 12]]

    assert [intraday._peek_rotation_pick(redis, fake_cats).id for _ in range(3)] == [11] * 3
    assert redis.data[ROTATION_KEY] == b"4"


def test_peek_rotation_pick_returns_none_on_empty():
    """Empty list → None, Redis не трогаем."""
    redis_mock = MagicMock()
    assert intraday._peek_rotation_pick(redis_mock, []) is None
    redis_mock.get.assert_not_called()


def test_peek_rotation_pick_returns_none_when_redis_none():
    """Redis None → None (graceful, не падаем)."""
    fake_cats = [MagicMock(id=1)]
    assert intraday._peek_rotation_pick(None, fake_cats) is None


def test_peek_rotation_pick_returns_none_on_redis_error():
    """GET raises → silent None."""
    redis_mock = MagicMock()
    redis_mock.get.side_effect = Exception("connection lost")
    fake_cats = [MagicMock(id=1)]
    assert intraday._peek_rotation_pick(redis_mock, fake_cats) is None


def test_advance_rotation_swallows_redis_error():
    """INCR raises → прогон не отменяется, просто очередь не сдвинулась."""
    redis_mock = MagicMock()
    redis_mock.incr.side_effect = Exception("connection lost")
    intraday._advance_rotation(redis_mock)  # не бросает


# ─── acquire_site_lock ───────────────────────────────────────────────────────


def test_acquire_site_lock_returns_true_when_setnx_succeeds():
    """Redis SET nx=True вернул "OK" — lock acquired."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = True
    assert intraday.acquire_site_lock(redis_mock, "pharmonline") is True
    redis_mock.set.assert_called_once_with(
        "intraday:lock:site:pharmonline",
        "1",
        nx=True,
        ex=intraday.INTRADAY_PER_SITE_MIN_GAP_SEC,
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
    redis_mock.get.return_value = None  # указателя ещё нет → cats[0]
    redis_mock.set.return_value = True  # lock acquired

    decision = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert decision.skip_reason is None
    site, picked_cat = decision.target
    assert picked_cat.id == cat.id
    assert site == "aloe"
    # Прогон взят → очередь передана следующей категории.
    redis_mock.incr.assert_called_once_with(ROTATION_KEY)


def test_pick_next_scrape_target_never_attempts_pharmonline(db_session):
    """Pharmonline исключён: intraday не открывает proxy WebSocket."""
    cat = _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.set.return_value = True

    decision = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert decision.target == ("aloe", cat)
    redis_mock.set.assert_called_once_with(
        ALOE_LOCK,
        "1",
        nx=True,
        ex=intraday.INTRADAY_PER_SITE_MIN_GAP_SEC,
    )


def test_rotation_serves_only_categories_the_tick_can_scrape(db_session):
    """Сколько бы чужих категорий ни стояло выше по volatility, тик идёт по aloe.

    Прод 2026-10-05…07: полные сборы aptekonline и pharmonline заняли своими
    категориями 24–26 мест из top-30. Раздела aloe у них нет, тик выбирал их по
    очереди и выходил пропуском — 29 раз из 32.
    """
    foreign = intraday.INTRADAY_TOP_N_CATEGORIES
    for i in range(foreign):
        site = "aptekonline" if i % 2 else "pharmonline"
        _add_volatile_category(db_session, f"foreign{i}", site, f"{site}-{i}", n_snaps=20)
    for i, n_snaps in enumerate([5, 3, 1]):
        _add_volatile_category(db_session, f"aloe{i}", "aloe", f"aloe-{i}", n_snaps=n_snaps)
    db_session.commit()

    # Исходное условие с прода: в общем top-30 нет ни одной категории aloe.
    unfiltered = intraday.top_volatile_categories(db_session)
    assert len(unfiltered) == foreign
    assert not any(c.aloe_slug for c in unfiltered)

    redis = _FakeRedis()
    picked = []
    for _ in range(6):
        decision = intraday.pick_next_scrape_target(db_session, redis_client=redis)
        assert decision.target is not None, decision.skip_detail
        site, cat = decision.target
        assert site == "aloe"
        picked.append(cat.key)
        redis.delete(ALOE_LOCK)  # до следующего тика прошло больше двух часов

    assert picked == ["aloe0", "aloe1", "aloe2"] * 2


def test_skip_names_missing_section_not_a_lock(db_session):
    """Цены менялись только там, где раздела aloe нет → причина названа прямо.

    Раньше это выходило как `intraday_all_sites_locked`, хотя замка не было.
    """
    _add_volatile_category(db_session, "apt", "aptekonline", "17", n_snaps=5)
    db_session.commit()

    redis = _FakeRedis()
    decision = intraday.pick_next_scrape_target(db_session, redis_client=redis)

    assert decision.target is None
    assert decision.skip_reason == intraday.SKIP_NO_SERVABLE_CATEGORY
    assert "section on aloe" in decision.skip_detail
    assert redis.data == {}, "ни сдвига очереди, ни замка — тик ничего не взял"


def test_rate_limited_tick_keeps_the_category_turn(db_session):
    """Сайт под лимитом частоты → пропуск, но очередь категории не сгорает."""
    _add_volatile_category(db_session, "aloe0", "aloe", "aloe-0", n_snaps=5)
    _add_volatile_category(db_session, "aloe1", "aloe", "aloe-1", n_snaps=3)
    db_session.commit()
    redis = _FakeRedis()

    first = intraday.pick_next_scrape_target(db_session, redis_client=redis)
    assert first.target[1].key == "aloe0"

    # Следующий тик через час: замок aloe ещё держится.
    throttled = intraday.pick_next_scrape_target(db_session, redis_client=redis)
    assert throttled.target is None
    assert throttled.skip_reason == intraday.SKIP_SITE_RATE_LIMITED
    assert "aloe1 keeps its turn" in throttled.skip_detail
    assert f"free in {intraday.INTRADAY_PER_SITE_MIN_GAP_SEC}s" in throttled.skip_detail
    assert redis.data[ROTATION_KEY] == 1, "пропущенный тик очередь не сдвигает"

    redis.delete(ALOE_LOCK)
    third = intraday.pick_next_scrape_target(db_session, redis_client=redis)
    assert third.target[1].key == "aloe1"


def test_pick_next_scrape_target_skips_when_no_volatile_cats(db_session):
    """0 категорий с volatility → пропуск."""
    _add_category(db_session, "k1", "K1", aloe_slug="aloe")  # без snapshots
    db_session.commit()

    redis_mock = MagicMock()
    decision = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    assert decision.target is None
    assert decision.skip_reason == intraday.SKIP_NO_SERVABLE_CATEGORY


def test_skip_names_redis_when_rotation_state_is_unavailable(db_session, monkeypatch):
    """Redis не настроен → тик пропущен, и причина — Redis, а не категории."""
    _add_volatile_category(db_session, "aloe0", "aloe", "aloe-0", n_snaps=5)
    db_session.commit()
    monkeypatch.delenv("REDIS_URL", raising=False)

    decision = intraday.pick_next_scrape_target(db_session)
    assert decision.target is None
    assert decision.skip_reason == intraday.SKIP_ROTATION_UNAVAILABLE
    assert "Redis" in decision.skip_detail


def test_pick_next_scrape_target_skips_sites_without_slug(db_session):
    """Категория без pharmonline_slug → берём только aloe."""
    _add_volatile_category(db_session, "k1", "aloe", "aloe-only", n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.set.return_value = True

    decision = intraday.pick_next_scrape_target(db_session, redis_client=redis_mock)
    site, _ = decision.target
    assert site == "aloe"  # pharmonline пропущен — нет slug


def test_pick_next_scrape_target_commit_false_does_not_mutate_redis(db_session):
    """commit_state=False (dry-run) → НЕТ INCR и НЕТ SETNX."""
    _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = b"0"  # текущий idx
    redis_mock.exists.return_value = 0  # lock free

    decision = intraday.pick_next_scrape_target(
        db_session, redis_client=redis_mock, commit_state=False
    )
    site, _ = decision.target
    assert site == "aloe"

    # State не изменился: НЕ должны вызваться INCR/SET/EXPIRE
    redis_mock.incr.assert_not_called()
    redis_mock.set.assert_not_called()
    redis_mock.expire.assert_not_called()
    # Read-only ops — OK
    redis_mock.get.assert_called()
    redis_mock.exists.assert_called()


def test_pick_next_scrape_target_commit_false_detects_existing_lock(db_session):
    """В preview mode locked Aloe даёт skip, не fallback на Pharmonline."""
    _add_category(db_session, "k1", "K1", ph_slug="ph", aloe_slug="aloe")
    p = _add_product_with_category(db_session, "pharmonline", "p1", "P1", "ph")
    _add_snaps(db_session, p, n_snaps=5)
    db_session.commit()

    redis_mock = MagicMock()
    redis_mock.get.return_value = b"0"
    redis_mock.exists.side_effect = lambda k: 1 if "aloe" in k else 0
    redis_mock.ttl.return_value = 1234

    decision = intraday.pick_next_scrape_target(
        db_session, redis_client=redis_mock, commit_state=False
    )
    assert decision.target is None
    assert decision.skip_reason == intraday.SKIP_SITE_RATE_LIMITED
    assert "free in 1234s" in decision.skip_detail
    redis_mock.set.assert_not_called()


def test_intraday_tick_invokes_bounded_point_scrape(db_session, monkeypatch):
    category = _add_category(
        db_session, "bounded", "Bounded", ph_slug="bounded-ph", aloe_slug="bounded-aloe"
    )
    db_session.commit()
    SessionLocal = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: SessionLocal)
    monkeypatch.setattr(
        intraday,
        "pick_next_scrape_target",
        lambda session, commit_state: intraday.TickDecision(target=("aloe", category)),
    )
    monkeypatch.setenv("INTRADAY_PRODUCT_LIMIT", "321")
    invoked = {}

    @click.command()
    def fake_scrape(limit, site, category_id):
        invoked.update(limit=limit, site=site, category_id=category_id)

    monkeypatch.setattr(main_mod, "scrape_cmd", fake_scrape)

    result = CliRunner().invoke(main_mod.cli, ["intraday-tick"])

    assert result.exit_code == 0, result.output
    assert invoked == {"limit": 321, "site": ("aloe",), "category_id": category.id}
    assert "limit=321" in result.output


def test_intraday_tick_prints_why_it_skipped(db_session, monkeypatch):
    """В journald попадает настоящая причина пропуска, а не «all sites locked»."""
    _add_volatile_category(db_session, "apt", "aptekonline", "17", n_snaps=5)
    db_session.commit()
    SessionLocal = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: SessionLocal)
    monkeypatch.setattr(intraday, "_redis_client", _FakeRedis)

    @click.command()
    def fake_scrape(limit, site, category_id):
        raise AssertionError("пропущенный тик не должен запускать сбор")

    monkeypatch.setattr(main_mod, "scrape_cmd", fake_scrape)

    result = CliRunner().invoke(main_mod.cli, ["intraday-tick"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == (
        "intraday-tick: skipped (no category with a section on aloe "
        "had price changes in the last 7 days)"
    )
    assert "locked" not in result.output


def test_watchlist_tick_invokes_alerting_priority_run(db_session, monkeypatch):
    """The scheduled priority command uses run/watchlist/hourly without --no-alerts."""
    tracked = storage.TrackedProduct(canonical_name="Critical SKU", is_active=True)
    db_session.add(tracked)
    db_session.flush()
    db_session.add(
        storage.TrackedProductLink(
            tracked_product_id=tracked.id,
            site="aloe",
            url="https://aloe.az/product/critical",
            status="confirmed",
        )
    )
    db_session.commit()

    SessionLocal = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: SessionLocal)
    invoked = {}

    @click.command()
    def fake_run(**kwargs):
        invoked.update(kwargs)

    monkeypatch.setattr(main_mod, "run_cmd", fake_run)

    result = CliRunner().invoke(main_mod.cli, ["watchlist-tick"])

    assert result.exit_code == 0, result.output
    assert invoked == {
        "dry_run": False,
        "limit": None,
        "site": ("aloe",),
        "mode": "watchlist",
        "category_id": None,
        "hourly": True,
        "no_alerts": False,
        "request_id": None,
    }
    assert "refreshing 1 confirmed URLs" in result.output


def test_watchlist_tick_defers_pharmonline_when_public_api_is_guarded(db_session, monkeypatch):
    """Do not silently weaken the full-catalog Pharmonline identity proof."""
    for site in ("pharmonline", "aloe"):
        tracked = storage.TrackedProduct(canonical_name=f"Critical {site}", is_active=True)
        db_session.add(tracked)
        db_session.flush()
        db_session.add(
            storage.TrackedProductLink(
                tracked_product_id=tracked.id,
                site=site,
                url=f"https://{site}.example/product/critical",
                status="confirmed",
            )
        )
    db_session.commit()

    SessionLocal = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: SessionLocal)
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    invoked = {}

    @click.command()
    def fake_run(**kwargs):
        invoked.update(kwargs)

    monkeypatch.setattr(main_mod, "run_cmd", fake_run)

    result = CliRunner().invoke(main_mod.cli, ["watchlist-tick"])

    assert result.exit_code == 0, result.output
    assert invoked["site"] == ("aloe",)
    assert "1 Pharmonline URLs deferred" in result.output
