"""Скрипт, которым опускают нижнюю границу каталога pharmonline, — на PostgreSQL.

`scripts/lower_pharmonline_catalog_floor.sql` — единственная команда проекта,
которая пишет в `pharmonline_public_api_catalog_baselines` на боевой базе, и
запускают её руками, раз в несколько месяцев, обычно во время отказа сбора.
Проверяется, что она опускает ровно границу своего тенанта и отказывает, не
тронув таблицу, во всех случаях, когда команду набрали не так.

Без PostgreSQL в `DATABASE_URL` тесты пропускаются; в CI пропуск — ошибка.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.storage import PharmonlinePublicAPICatalogBaseline as Baseline

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lower_pharmonline_catalog_floor.sql"
TABLE = Baseline.__table__.name


@pytest.fixture
def connection():
    """Сеанс с временной копией таблицы: настоящую таблицу базы тест не трогает."""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        if os.environ.get("CI"):
            pytest.fail("в CI скрипт границы обязан проверяться на PostgreSQL из DATABASE_URL")
        pytest.skip("нужен PostgreSQL в DATABASE_URL")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as sa_connection:
        # Сырое соединение драйвера: файл исполняется целиком, простым
        # протоколом, как его исполнит psql, — без разбора «%» в тексте.
        raw = sa_connection.connection.driver_connection
        ddl = str(CreateTable(Baseline.__table__).compile(dialect=postgresql.dialect()))
        raw.execute(ddl.replace("CREATE TABLE", "CREATE TEMPORARY TABLE", 1))
        yield raw
    engine.dispose()


def _add(raw, minimum: int, *, tenant_id: int = 1) -> None:
    raw.execute(
        f"INSERT INTO {TABLE} (tenant_id, catalog_item_count, minimum_catalog_item_count, "
        "verified_identity_count, trusted_ddp_item_count, retired_ddp_item_count, "
        "reconciled_item_count, proof_version, source_manifest_sha256, "
        "catalog_fingerprint_sha256, source_transport, preflight_run_ref, created_at) "
        f"VALUES ({tenant_id}, 9565, {minimum}, 9565, 9859, 365, 74, 'url_continuity_v1', "
        "repeat('a', 64), repeat('b', 64), 'decodo', 'test', now())"
    )


def _floors(raw) -> list[tuple[int, int]]:
    return raw.execute(
        f"SELECT tenant_id, minimum_catalog_item_count FROM {TABLE} ORDER BY id"
    ).fetchall()


def _run(raw, *, expect: object | None, new: object | None) -> None:
    """Параметры приходят настройками сеанса — как из PGOPTIONS в шапке файла."""
    for name, value in (("pm.expect_floor", expect), ("pm.new_floor", new)):
        raw.execute("SELECT set_config(%s, %s, false)", (name, "" if value is None else str(value)))
    raw.execute(SCRIPT.read_text(encoding="utf-8"))


def test_lowers_the_floor_of_tenant_one_only(connection):
    _add(connection, 9000)
    _add(connection, 8700)  # уже ниже новой границы — не трогается
    _add(connection, 9000, tenant_id=2)

    _run(connection, expect=9000, new=8900)

    assert _floors(connection) == [(1, 8900), (1, 8700), (2, 9000)]


def test_every_row_above_the_new_floor_is_lowered(connection):
    """Действует наибольшее значение: оставь одну строку выше — граница не сдвинется."""
    _add(connection, 9000)
    _add(connection, 8950)

    _run(connection, expect=9000, new=8900)

    assert _floors(connection) == [(1, 8900), (1, 8900)]


@pytest.mark.parametrize(
    ("expect", "new", "message"),
    [
        (9000, 9100, "только опускается"),
        (9000, 9000, "только опускается"),
        (9000, 0, "только опускается"),
        (9500, 8900, "граница сейчас 9000, а названа 9500"),
        # опечатка: 900 вместо 9000 выключила бы границу совсем
        (9000, 900, "слишком большой шаг"),
        (9000, 8099, "слишком большой шаг"),
        (None, 8900, "не заданы"),
        (9000, None, "не заданы"),
        (9000, "abc", "целыми числами"),
    ],
)
def test_refusal_leaves_the_table_untouched(connection, expect, new, message):
    _add(connection, 9000)

    with pytest.raises(psycopg.errors.RaiseException, match=message):
        _run(connection, expect=expect, new=new)

    assert _floors(connection) == [(1, 9000)]


def test_largest_allowed_step_is_a_tenth(connection):
    _add(connection, 9000)

    _run(connection, expect=9000, new=8100)

    assert _floors(connection) == [(1, 8100)]


def test_second_run_of_the_same_command_is_refused(connection):
    """Команду из переписки не выполнить дважды: названная граница уже не та."""
    _add(connection, 9000)
    _run(connection, expect=9000, new=8900)

    with pytest.raises(psycopg.errors.RaiseException, match="граница сейчас 8900"):
        _run(connection, expect=9000, new=8900)

    assert _floors(connection) == [(1, 8900)]


def test_no_floor_row_is_refused(connection):
    _add(connection, 9000, tenant_id=2)

    with pytest.raises(psycopg.errors.RaiseException, match="границы нет"):
        _run(connection, expect=9000, new=8900)

    assert _floors(connection) == [(2, 9000)]
