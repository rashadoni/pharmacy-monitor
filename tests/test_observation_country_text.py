"""Текст страны в журнале наблюдений не длиннее своей колонки.

Страну сайт отдаёт свободным текстом (у aptekonline — поле `olke`). В карточке
товара текст обрезался до ширины колонки, а в `offer_observations` писался как
есть: подпись длиннее колонки PostgreSQL не принимал, и пачка сбора не
записывалась вовсе. SQLite длину колонки не проверяет, поэтому сам отказ виден
только на PostgreSQL.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from src import storage
from src._time import utcnow
from src.main import persist_results
from src.product_observations import apply_product_observation
from src.product_policy import COUNTRY_INVALID, COUNTRY_RESOLVED, country_resolution
from src.scrapers.base import ScrapedProduct, ScrapeResult

_PRODUCT_COLUMN = storage.Product.__table__.c.manufacturer_country_raw
_OBSERVATION_COLUMN = storage.OfferObservation.__table__.c.country_raw
_LONG_COUNTRY = "Türkiyə, Almaniya, Fransa, İtaliya, İspaniya; " * 12


def _without_postgresql(reason: str) -> None:
    """Локально — пропуск. В CI пропуск молча убрал бы единственный вариант,
    в котором база отказывает в записи."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, db_session):
    if request.param == "sqlite":
        yield db_session
        return
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        _without_postgresql("PostgreSQL DATABASE_URL is required")
    # `src.main` подгружает `.env`: тест не создаёт схемы в базе, которая не тестовая.
    if not (make_url(database_url).database or "").endswith("_test"):
        _without_postgresql("PostgreSQL DATABASE_URL must point at a *_test database")
    # Своя схема на тест: общая база CI уже размечена миграциями.
    schema = f"country_text_{uuid4().hex[:12]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _scraped(external_id: str, country: str | None) -> ScrapedProduct:
    return ScrapedProduct(
        site="aptekonline",
        external_id=external_id,
        url=f"https://aptekonline.az/product/{external_id}",
        name=f"Товар {external_id}",
        price=10.0,
        manufacturer_country_raw=country,
        country_source="aptek_api_olke" if country else None,
    )


def _stored(db_session) -> dict[str, tuple[str | None, str | None]]:
    """external_id → (текст страны в карточке товара, текст страны в наблюдении)."""
    rows = db_session.execute(
        select(
            storage.Product.external_id,
            storage.Product.manufacturer_country_raw,
            storage.OfferObservation.country_raw,
        ).join(storage.OfferObservation, storage.OfferObservation.product_id == storage.Product.id)
    ).all()
    assert len(rows) == len({row.external_id for row in rows}), "одно наблюдение на товар"
    return {row.external_id: (row[1], row[2]) for row in rows}


def test_country_text_longer_than_the_column_does_not_refuse_the_batch(db_session):
    assert len(_LONG_COUNTRY) > _OBSERVATION_COLUMN.type.length

    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    count = persist_results(
        db_session,
        run,
        [
            ScrapeResult(
                site="aptekonline",
                products=[
                    _scraped("before", "Latvia"),
                    _scraped("long", _LONG_COUNTRY),
                    _scraped("after", None),
                ],
            )
        ],
    )

    assert count == 3
    db_session.expire_all()
    fitted = _LONG_COUNTRY[: _OBSERVATION_COLUMN.type.length]
    assert _stored(db_session) == {
        "before": ("Latvia", "Latvia"),
        "long": (fitted, fitted),
        "after": (None, None),
    }


def test_both_country_columns_hold_the_same_text():
    """Обрезка одна на обе колонки: разойдутся ширины — тест напомнит о второй."""
    product = storage.Product(
        site="aptekonline",
        external_id="x",
        url="https://aptekonline.az/product/x",
        name="Товар",
        name_normalized="товар",
    )

    observation = apply_product_observation(
        product, _scraped("x", _LONG_COUNTRY), run_id=1, observed_at=utcnow()
    )

    assert len(observation.country_raw) == _OBSERVATION_COLUMN.type.length
    assert len(product.manufacturer_country_raw) == _PRODUCT_COLUMN.type.length
    assert observation.country_raw == product.manufacturer_country_raw


def test_country_is_resolved_from_the_whole_text_not_from_the_stored_part():
    """В колонку помещается только «Türkiyə», а сайт написал две страны."""
    two_countries = "Türkiyə" + " " * _OBSERVATION_COLUMN.type.length + "Almaniya"
    product = storage.Product(
        site="aptekonline",
        external_id="x",
        url="https://aptekonline.az/product/x",
        name="Товар",
        name_normalized="товар",
    )

    observation = apply_product_observation(
        product, _scraped("x", two_countries), run_id=1, observed_at=utcnow()
    )

    # Сохранённая часть сама по себе — страна: иначе тест ничего не различает.
    assert country_resolution(observation.country_raw) == ("tr", COUNTRY_RESOLVED)
    assert (observation.country_code, observation.country_resolution_status) == (
        None,
        COUNTRY_INVALID,
    )
    assert (product.manufacturer_country_code, product.country_resolution_status) == (
        None,
        COUNTRY_INVALID,
    )


def test_missing_country_text_stays_missing_and_keeps_the_known_one():
    product = storage.Product(
        site="aptekonline",
        external_id="x",
        url="https://aptekonline.az/product/x",
        name="Товар",
        name_normalized="товар",
    )
    apply_product_observation(product, _scraped("x", "Latvia"), run_id=1, observed_at=utcnow())

    observation = apply_product_observation(
        product, _scraped("x", None), run_id=2, observed_at=utcnow()
    )

    assert observation.country_raw is None
    assert product.manufacturer_country_raw == "Latvia"
