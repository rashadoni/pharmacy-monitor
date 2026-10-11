"""Общие фикстуры для тестов: shared in-memory SQLite через StaticPool."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.storage import Base


@pytest.fixture
def db_session():
    """In-memory SQLite, разделяемый между всеми коннектами (StaticPool).

    Без этого `:memory:` создаёт новую БД на каждый коннект, и таблицы из init_db
    не видны последующим запросам.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with Session() as session:
        yield session
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset_catalog_search_index():
    """Индекс поиска живёт в памяти процесса — у каждого теста своя БД."""
    from src import catalog_search

    catalog_search.reset_cache()
    yield
    catalog_search.reset_cache()


@pytest.fixture(autouse=True)
def _not_a_server(monkeypatch):
    """Для health любая машина, где идут тесты, — машина разработчика.

    `health._check_env_file_next_to_code` смотрит на настоящий диск: есть ли файл
    настроек сервера и лежит ли `.env` в корне чекаута. Там, где есть оба, краснел
    бы каждый тест, ждущий чистый отчёт. Тесты самой проверки
    (`tests/test_health_env_file.py`) ставят оба пути сами.
    """
    from src import health

    monkeypatch.setattr(
        health, "_SERVER_ENV_FILE", Path(__file__).resolve().parent / "no-server-settings-here"
    )
