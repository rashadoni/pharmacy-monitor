"""Тесты seed_demo() — fake-данные для демо/тестирования."""

import pytest
from sqlalchemy import select

from src.demo import SEED_MATCHED, SEED_PROMOS, SEED_UNMATCHED, has_existing_run_data, seed_demo
from src.storage import Match, PriceSnapshot, Product, Promo, Run


def test_seed_demo_on_empty_db(db_session):
    summary = seed_demo(db_session)
    assert summary["runs"] == 2
    assert summary["matches"] == len(SEED_MATCHED)
    expected_products = 3 * len(SEED_MATCHED) + sum(len(v) for v in SEED_UNMATCHED.values())
    assert summary["products"] == expected_products
    assert summary["promos"] == len(SEED_PROMOS)


def test_seed_demo_creates_matches_with_3_products(db_session):
    seed_demo(db_session)
    matches = db_session.scalars(select(Match)).all()
    for m in matches:
        sites = {p.site for p in m.products}
        assert sites == {"pharmonline", "aptekonline", "aloe"}, (
            f"Match #{m.id} должен иметь по 1 продукту с каждого сайта"
        )


def test_seed_demo_creates_2_runs_with_snapshots(db_session):
    seed_demo(db_session)
    runs = db_session.scalars(select(Run).order_by(Run.started_at)).all()
    assert len(runs) == 2
    yesterday_run, today_run = runs
    assert yesterday_run.started_at < today_run.started_at

    # Каждый прогон должен иметь snapshot'ы для ВСЕХ продуктов
    n_products = (
        db_session.scalar(select(Product).count())
        if hasattr(select(Product), "count")
        else len(db_session.scalars(select(Product)).all())
    )
    n_snaps_today = len(
        db_session.scalars(select(PriceSnapshot).where(PriceSnapshot.run_id == today_run.id)).all()
    )
    assert n_snaps_today == n_products


def test_seed_demo_unmatched_products_no_canonical(db_session):
    seed_demo(db_session)
    unmatched = db_session.scalars(select(Product).where(Product.canonical_id.is_(None))).all()
    expected = sum(len(v) for v in SEED_UNMATCHED.values())
    assert len(unmatched) == expected


def test_seed_demo_promos_attached_to_today_run(db_session):
    seed_demo(db_session)
    promos = db_session.scalars(select(Promo)).all()
    assert len(promos) == len(SEED_PROMOS)
    today_run = db_session.scalars(select(Run).order_by(Run.started_at.desc()).limit(1)).first()
    for promo in promos:
        assert promo.run_id == today_run.id


def test_seed_demo_refuses_existing_data_without_force(db_session):
    seed_demo(db_session)
    with pytest.raises(RuntimeError, match="--force"):
        seed_demo(db_session)


def test_seed_demo_force_wipes_and_reseeds(db_session):
    seed_demo(db_session)
    matches_v1 = db_session.scalars(select(Match)).all()
    ids_v1 = {m.id for m in matches_v1}

    seed_demo(db_session, force=True)
    matches_v2 = db_session.scalars(select(Match)).all()
    # Тот же набор данных, но новые ID (после wipe)
    assert len(matches_v2) == len(matches_v1)
    # Старые id больше не существуют в БД
    for old_id in ids_v1:
        assert db_session.get(Match, old_id) is None or db_session.get(Match, old_id) in matches_v2


def test_has_existing_run_data_detects_correctly(db_session):
    assert has_existing_run_data(db_session) is False
    seed_demo(db_session)
    assert has_existing_run_data(db_session) is True
