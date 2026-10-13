"""Общие фикстуры для тестов: shared in-memory SQLite через StaticPool."""

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
def _request_counters_end_with_the_test(monkeypatch):
    """Счётчики запросов (`src.rate_limit`) не переживают тест.

    Имя счётчика — адрес клиента, а у `TestClient` он один на все тесты. В CI
    рядом стоит Redis: счётчик в нём живёт минуту и достаётся следующим тестам,
    так что одиннадцатый за минуту вход по ссылке получил бы 429 — в тесте,
    который счётчиков не касается. Поэтому в тестах счётчики лежат в памяти
    процесса и стираются до и после каждого теста.

    Работу самого Redis-пути проверяет tests/test_rate_limit.py — подставным
    клиентом; фикстуру он отключает, объявив у себя одноимённую. Отдельный
    процесс (пробы под настоящим uvicorn) фикстура не видит: там счётчики
    держит сам процесс, и своё окружение проба собирает без `REDIS_URL`.
    """
    from src import rate_limit

    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    yield
    rate_limit._reset_memory_for_tests()
