"""Шаг сопоставления упал посреди работы — недоделанное в базу не попадает.

Этап сопоставления (`main._run_matching_stage`: конец `run` и команда `rematch`)
правит состав пар несколькими шагами, и каждый шаг фиксирует свою работу сам,
в конце. До 2026-10-09 шаг, упавший на середине, оставлял сделанную половину в
сессии, а обработчик сбоя `run` вместе с итогом прогона её фиксировал: его
`session.commit()` шёл без отката. Воспроизведено настоящей командой `run` на
PostgreSQL: шаг отвязал один товар от пары и бросил `ValueError` — прогон
получил `failed`, а в базе осталась пара из одного товара, без отказа в
`match_rejections` и без записи в `match_policy_audits`. `revalidate_split`
делает `session.flush()` посреди цикла, так что его полуразобранный кластер
попадал в базу тем же путём — без записи, по которой разбор можно отменить.

Если шаг падал на ошибке SQL, обработчик падал уже сам — на записи итога в
оборванную транзакцию, — и прогон оставался `running`.

Теперь этап откатывает незавершённую транзакцию до того, как исключение шага
уйдёт вызывающему. Проверяется командой, а не функцией: что окажется в базе,
решает тот, кто после сбоя первым сделает commit.

`rematch` после сбоя закрывает сессию без commit, поэтому состав пар он не
портил и раньше: его варианты закрепляют, что команды ведут себя одинаково, а
от правки в них зависит только снятие замка (журнал без жалоб на него).

Каждый сценарий идёт на SQLite и на PostgreSQL. Прод — PostgreSQL: только там
этап держит замок на выделенном соединении, только там ошибка запроса обрывает
транзакцию и только там соединение можно потерять.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
import structlog
from click.testing import CliRunner
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import matcher, product_policy, storage, watchlist
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult
from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline, _session_factory

# Настоящие шаги: обвязка сбора подменяет их пустышками, тест возвращает нужные.
_REAL_STEPS = {
    name: getattr(matcher, name)
    for name in ("match_products", "revalidate_split", "flag_suspected_mismatches")
}
# Сеансы команды на сервере отличимы от чужих по имени приложения.
_APPLICATION = f"stage_failure_{uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def postgres_schema():
    """Своя схема с таблицами проекта — одна на файл: `create_all` дороже самих тестов."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        yield None
        return
    schema = f"stage_failure_{uuid4().hex[:12]}"
    admin = create_engine(
        database_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": f"-csearch_path={schema}"},
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        storage.Base.metadata.create_all(admin)
        yield database_url, schema, admin
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, db_session, monkeypatch, postgres_schema):
    if request.param == "sqlite":
        yield db_session
        return
    if postgres_schema is None:
        if os.environ.get("CI"):
            pytest.fail(
                "в CI сбой этапа сопоставления обязан проверяться на PostgreSQL, а "
                "DATABASE_URL — не он: замка на выделенном соединении на SQLite нет."
            )
        pytest.skip("PostgreSQL DATABASE_URL is required")
    database_url, schema, admin = postgres_schema
    # Замок сбора — один на базу, а не на схему; замок сопоставления остаётся
    # настоящим: под ним и идёт этап.
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, *, wait: True
    )
    # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а не вешает.
    options = f"-csearch_path={schema} -clock_timeout=5000 -capplication_name={_APPLICATION}"
    # Параметры пула — как в `storage.make_engine`.
    engine = create_engine(
        database_url,
        connect_args={"options": options},
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_use_lifo=True,
    )
    try:
        with engine.connect() as connection:
            # Команда пишет туда, куда смотрит search_path: не в свою схему — не начинаем.
            assert connection.scalar(text("SELECT current_schema()")) == schema
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            # Соединение замка — вне пула: упавший между взятием и снятием тест
            # оставил бы замок (и незакрытую транзакцию) всем следующим.
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = :application"
                ),
                {"application": _APPLICATION},
            )
            # Схема названа явно: чистка не должна зависеть от того, применился
            # ли search_path соединения.
            connection.execute(
                text(f'DROP FUNCTION IF EXISTS "{schema}".refuse_repartition() CASCADE')
            )
            # DELETE, а не TRUNCATE: на почти пустых таблицах он на порядок быстрее.
            for table in reversed(storage.Base.metadata.sorted_tables):
                connection.execute(text(f'DELETE FROM "{schema}"."{table.name}"'))


def _is_postgresql(db_session) -> bool:
    return db_session.get_bind().dialect.name == "postgresql"


# ─── Каталог ─────────────────────────────────────────────────────────────────


def _product(
    session, site: str, tag: str, name: str, *, country: str | None = None
) -> storage.Product:
    product = storage.Product(
        tenant_id=1,
        site=site,
        external_id=tag,
        url=f"https://{site}.example/product/{tag}",
        name=name,
        # Пересчёт выводимых полей — первый шаг этапа — заполнит его по названию.
        name_normalized="",
        manufacturer_country_code=country,
        country_resolution_status="resolved" if country else None,
    )
    session.add(product)
    return product


def _settled_pair(session) -> int:
    """Пара, к которой у правил нет вопросов: этап её не трогает."""
    match = storage.Match(tenant_id=1, canonical_name="Ferrovef", confidence=0.9, is_manual=False)
    session.add(match)
    session.flush()
    for site in ("pharmonline", "aptekonline"):
        _product(session, site, f"settled-{site}", "Ferrovef N60").canonical_id = match.id
    session.commit()
    return match.id


def _cluster_the_rules_forbid(session) -> int:
    """Кластер из двух товаров Украины и двух Сербии: `revalidate_split` делит его надвое.

    На пути: три отказа (каждый — со своим flush), отвязка всех четырёх товаров,
    новый кластер для второй группы (ещё один flush) и запись в
    `match_policy_audits`, по которой разбор можно отменить.
    """
    match = storage.Match(tenant_id=1, canonical_name="Ornafer", confidence=0.95, is_manual=False)
    session.add(match)
    session.flush()
    for site, tag, country in (
        ("pharmonline", "ua-ph", "ua"),
        ("aloe", "ua-aloe", "ua"),
        ("pharmonline", "rs-ph", "rs"),
        ("aptekonline", "rs-aptek", "rs"),
    ):
        _product(session, site, tag, "Ornafer N30", country=country).canonical_id = match.id
    session.commit()
    return match.id


def _two_products_the_matcher_pairs(session) -> None:
    """Товар клиента и конкурента без пары: `match_products` сводит их и фиксирует."""
    _product(session, "pharmonline", "fresh-ph", "Aspirin Kardio 100 mq 30 tablet")
    _product(session, "aloe", "fresh-aloe", "Aspirin Kardio 100 mq 30 tablet")
    session.commit()


def _pairing(db_session) -> dict[str, int | None]:
    """Кто в какой паре — свежим чтением, мимо всего, что помнит сессия теста."""
    with _session_factory(db_session)() as verify:
        rows = verify.execute(select(storage.Product.external_id, storage.Product.canonical_id))
        return {tag: canonical_id for tag, canonical_id in rows}


def _normalized_names(db_session) -> set[str]:
    with _session_factory(db_session)() as verify:
        return set(verify.scalars(select(storage.Product.name_normalized)))


def _count(db_session, model) -> int:
    with _session_factory(db_session)() as verify:
        return verify.scalar(select(func.count()).select_from(model))


def _latest_run(db_session) -> storage.Run:
    with _session_factory(db_session)() as verify:
        return verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))


# ─── Команды ─────────────────────────────────────────────────────────────────


async def _scrape_the_pinned_link(urls_by_site):
    return [
        ScrapeResult(
            site="aloe",
            products=[
                ScrapedProduct(
                    site="aloe",
                    external_id="pinned-aloe",
                    url="https://aloe.example/product/pinned-aloe",
                    name="Friso Gold 800 q",
                    category="watchlist",
                )
            ],
            category_counts={"watchlist": 1},
            route_statuses={
                "watchlist": RouteStatus(
                    complete=True, raw_items=1, parsed_items=1, expected_items=1
                )
            },
        )
    ]


_COMMANDS = {
    # Проверенный полный сбор aloe: сам сбор и запись подменены, этап настоящий.
    "run": ["run", "--site", "aloe", "--mode", "category", "--no-alerts"],
    # Сбор по закреплённым ссылкам: перед этапом, под тем же замком, — привязка по ним.
    "watchlist run": ["run", "--site", "aloe", "--mode", "watchlist", "--no-alerts"],
    "rematch": ["rematch"],
}


def _invoke(db_session, monkeypatch, command: str, *, steps: dict | None = None):
    """Выполнить команду; `steps` — чем подменить шаги этапа, остальные идут настоящие."""
    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    monkeypatch.setattr(main_mod, "scrape_watchlist_all", _scrape_the_pinned_link)
    for name, real in _REAL_STEPS.items():
        monkeypatch.setattr(matcher, name, (steps or {}).get(name, real))
    # Сессия теста своё зафиксировала и соединение не держит.
    db_session.rollback()
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(main_mod.cli, _COMMANDS[command])


# Журнал команды идёт в её вывод: команда настраивает его сама.
_LOCK_RELEASE_COMPLAINTS = ("matcher_lock_release_failed", "matcher_lock_not_held_at_release")


def _assert_run_failed(db_session, *reason: str) -> None:
    run = _latest_run(db_session)
    assert run.status == "failed"
    assert run.finished_at is not None
    for part in reason:
        assert part in (run.error_message or "")


def _assert_lock_left_nothing_behind(db_session) -> None:
    """Замок снят, а его соединение закрыто: на сервере остались только соединения пула."""
    if not _is_postgresql(db_session):
        return
    engine = db_session.get_bind()
    assert engine.pool.checkedout() == 0
    deadline = time.monotonic() + 5
    with engine.connect() as connection:
        # Сервер завершает сеанс чуть позже, чем клиент закрыл сокет.
        while True:
            holders, sessions = connection.execute(
                text(
                    "SELECT count(*) FILTER (WHERE EXISTS ("
                    "  SELECT 1 FROM pg_locks AS locks"
                    "  WHERE locks.pid = activity.pid AND locks.locktype = 'advisory')), "
                    "count(*) "
                    "FROM pg_stat_activity AS activity WHERE application_name = :application"
                ),
                {"application": _APPLICATION},
            ).one()
            connection.commit()
            # Это соединение само взято из пула и в `checkedin` не входит.
            if holders == 0 and sessions == engine.pool.checkedin() + 1:
                return
            assert time.monotonic() < deadline, (
                f"после команды осталось: сеансов с замком {holders}, сеансов всего {sessions}"
            )
            time.sleep(0.02)


# ─── Шаг изменил пару и упал ─────────────────────────────────────────────────


# Как далеко шаг успел зайти → что остаётся в причине отказа.
_REASON = {
    "in memory": "the step broke halfway",
    "flushed": "the step broke halfway",
    # Ошибка запроса: PostgreSQL до отката не выполняет в транзакции ничего.
    "refused statement": "no_such_table_in_this_schema",
    # Обрыв соединения, на котором лежит замок этапа, — на запросе шага.
    "lost connection": "terminating connection",
    # То же, но шаг о нём не знает: первым на мёртвое соединение приходит откат.
    "connection lost unnoticed": "the step broke halfway",
}
_NEEDS_THE_LOCK_CONNECTION = {"lost connection", "connection lost unnoticed"}


def _lose_the_lock_connection_behind_the_step(session) -> None:
    pid = session.scalar(text("SELECT pg_backend_pid()"))
    # Под замком `get_bind()` — его соединение; чужое берём из пула движка.
    with session.get_bind().engine.connect() as outsider:
        outsider.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        deadline = time.monotonic() + 5
        gone = text("SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = :pid")
        while True:
            left = outsider.scalar(gone, {"pid": pid})
            # Список сеансов сервер замораживает на транзакцию: без commit
            # следующий запрос вернул бы тот же ответ.
            outsider.commit()
            if left:
                return
            assert time.monotonic() < deadline, "сервер не завершил сеанс замка"
            time.sleep(0.02)


def _unpair_one_product_then_fail(how: str):
    """Шаг отвязывает товар от пары и падает."""

    def step(session, **kwargs):
        product = session.scalar(
            select(storage.Product).where(storage.Product.external_id == "settled-aptekonline")
        )
        product.canonical_id = None
        if how != "in memory":
            session.flush()
        if how == "refused statement":
            session.execute(text("SELECT * FROM no_such_table_in_this_schema"))
        if how == "lost connection":
            session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
        if how == "connection lost unnoticed":
            _lose_the_lock_connection_behind_the_step(session)
        raise ValueError("the step broke halfway")

    return step


@pytest.mark.parametrize(
    ("command", "step"),
    [("run", "match_products"), ("run", "revalidate_split"), ("rematch", "revalidate_split")],
)
@pytest.mark.parametrize("how", list(_REASON))
def test_half_a_failed_step_managed_to_do_is_not_saved(db_session, monkeypatch, command, step, how):
    """Пара остаётся в том составе, в каком была; `run` получает `failed` с причиной.

    Итог прогона пишется уже после этапа: замок снят, сессия снова на пуле. Это
    верно и когда соединение замка потеряно, — под замком после отката не
    выполняется ничего.
    """
    if how in _NEEDS_THE_LOCK_CONNECTION and not _is_postgresql(db_session):
        pytest.skip("соединение замка есть только на PostgreSQL")
    settled = _settled_pair(db_session)
    _two_products_the_matcher_pairs(db_session)

    result = _invoke(
        db_session, monkeypatch, command, steps={step: _unpair_one_product_then_fail(how)}
    )

    assert result.exit_code != 0
    assert _REASON[how] in result.output
    if command == "run":
        _assert_run_failed(db_session, _REASON[how])
    pairing = _pairing(db_session)
    assert (pairing["settled-pharmonline"], pairing["settled-aptekonline"]) == (settled, settled)
    assert _count(db_session, storage.MatchRejection) == 0
    assert _count(db_session, storage.MatchPolicyAudit) == 0
    if step == "revalidate_split":
        # `match_products` свою работу зафиксировал до сбоя — она остаётся,
        # а с ней пересчёт полей: его фиксирует тот же commit.
        assert pairing["fresh-ph"] is not None
        assert pairing["fresh-ph"] == pairing["fresh-aloe"]
        assert "" not in _normalized_names(db_session)
    else:
        assert (pairing["fresh-ph"], pairing["fresh-aloe"]) == (None, None)
        if _is_postgresql(db_session):
            # Упал сам `match_products`: вместе с ним ушёл и пересчёт полей. На
            # SQLite он остаётся: перед точкой сохранения транзакция ничего не
            # писала, драйвер её ещё не открывал, и RELEASE фиксирует сделанное.
            assert _normalized_names(db_session) == {""}
    _assert_lock_left_nothing_behind(db_session)
    if how == "connection lost unnoticed":
        assert "matching_work_rollback_failed" in result.output
    else:
        assert "matching_work_rolled_back" in result.output
    if how in _NEEDS_THE_LOCK_CONNECTION:
        # Замок ушёл вместе с соединением — снимать было нечего.
        assert "matcher_lock_not_held_at_release" in result.output
    else:
        # Транзакция к снятию замка уже откачена: запрос снятия проходит.
        assert not [event for event in _LOCK_RELEASE_COMPLAINTS if event in result.output]


# ─── Настоящий revalidate_split ──────────────────────────────────────────────


def _audit_that_cannot_be_built(**fields):
    raise ValueError("the audit record could not be built")


def _refuse_the_second_cluster(db_session) -> None:
    """База не принимает кластер, который `revalidate_split` создаёт для второй группы."""
    with db_session.get_bind().begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION refuse_repartition() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'the database refused the new cluster'; END "
                "$$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER refuse_repartition BEFORE INSERT ON matches FOR EACH ROW "
                "WHEN (NEW.match_strategy = 'country_repartition') "
                "EXECUTE FUNCTION refuse_repartition()"
            )
        )


@pytest.mark.parametrize("command", ["run", "rematch"])
@pytest.mark.parametrize("broken_at", ["audit record", "second cluster"])
def test_half_split_cluster_is_not_saved(db_session, monkeypatch, command, broken_at):
    """`revalidate_split` упал, разобрав кластер наполовину, — кластер остаётся целым.

    К сбою в транзакции уже лежат три отказа и отвязка товаров второй группы
    (их отправили flush самого шага). `audit record` — ошибка Python после
    этого: раньше `run` фиксировал разбор без записи, по которой его можно
    отменить. `second cluster` — отказ базы: раньше обработчик `run` падал на
    записи итога, и прогон оставался `running`.

    Пара, которую `match_products` зафиксировал до сбоя, остаётся.
    """
    forbidden = _cluster_the_rules_forbid(db_session)
    _two_products_the_matcher_pairs(db_session)
    if broken_at == "audit record":
        monkeypatch.setattr(matcher, "MatchPolicyAudit", _audit_that_cannot_be_built)
        reason = "the audit record could not be built"
    else:
        if not _is_postgresql(db_session):
            pytest.skip("отказ базы посреди шага — триггер PostgreSQL")
        _refuse_the_second_cluster(db_session)
        reason = "the database refused the new cluster"

    result = _invoke(db_session, monkeypatch, command)

    assert result.exit_code != 0
    assert "identity revalidation failed" in result.output
    assert reason in result.output
    if command == "run":
        _assert_run_failed(db_session, "identity revalidation failed", reason)
    pairing = _pairing(db_session)
    assert {pairing[tag] for tag in ("ua-ph", "ua-aloe", "rs-ph", "rs-aptek")} == {forbidden}
    assert _count(db_session, storage.MatchRejection) == 0
    assert _count(db_session, storage.MatchPolicyAudit) == 0
    assert pairing["fresh-ph"] is not None
    assert pairing["fresh-ph"] == pairing["fresh-aloe"]
    # Кластер правил и пара от `match_products` — третьего, пустого, не осталось.
    assert _count(db_session, storage.Match) == 2
    _assert_lock_left_nothing_behind(db_session)
    assert "matching_work_rolled_back" in result.output
    assert not [event for event in _LOCK_RELEASE_COMPLAINTS if event in result.output]


def test_the_split_still_lands_whole_when_nothing_fails(db_session, monkeypatch):
    """Тот же кластер без сбоя: разбор, отказы и запись о нём фиксируются вместе."""
    forbidden = _cluster_the_rules_forbid(db_session)

    result = _invoke(db_session, monkeypatch, "run")

    assert result.exit_code == 0, result.output
    assert _latest_run(db_session).status == "ok"
    pairing = _pairing(db_session)
    assert pairing["ua-ph"] == pairing["ua-aloe"] == forbidden
    assert pairing["rs-ph"] == pairing["rs-aptek"]
    assert pairing["rs-ph"] not in {None, forbidden}
    assert _count(db_session, storage.MatchRejection) == 3
    assert _count(db_session, storage.MatchPolicyAudit) == 1
    assert "matching_work_" not in result.output


# ─── Привязка закреплённых ссылок ────────────────────────────────────────────


def test_half_linked_watchlist_product_is_not_saved(db_session, monkeypatch):
    """Привязка по закреплённым ссылкам упала на втором товаре — первый не привязан.

    Она идёт под тем же замком перед этапом и так же фиксирует свою работу в
    конце: ручная пара, созданная на середине, и товар, уже привязанный к ней,
    раньше уходили в базу вместе с итогом прогона.
    """
    watchlist.add_tracked_product(
        db_session,
        canonical_name="Friso Gold 800 q",
        brand="Friso",
        pharmonline_url="https://pharmonline.example/product/pinned-ph",
        aloe_url="https://aloe.example/product/pinned-aloe",
    )
    _product(db_session, "pharmonline", "pinned-ph", "Friso Gold 800 q")
    _product(db_session, "aloe", "pinned-aloe", "Friso Gold 800 q")
    db_session.commit()
    checked: list[int] = []
    real = product_policy.policy_offer_eligibility

    def offer_check_that_breaks_on_the_second_product(product):
        checked.append(product.id)
        if len(checked) == 2:
            raise ValueError("the link check broke on the second product")
        return real(product)

    monkeypatch.setattr(
        product_policy, "policy_offer_eligibility", offer_check_that_breaks_on_the_second_product
    )

    result = _invoke(db_session, monkeypatch, "watchlist run")

    assert result.exit_code != 0
    assert len(checked) == 2
    _assert_run_failed(db_session, "the link check broke on the second product")
    pairing = _pairing(db_session)
    assert (pairing["pinned-ph"], pairing["pinned-aloe"]) == (None, None)
    assert _count(db_session, storage.Match) == 0
    _assert_lock_left_nothing_behind(db_session)
    assert "matching_work_rolled_back" in result.output
    assert not [event for event in _LOCK_RELEASE_COMPLAINTS if event in result.output]


# ─── Сам откат ───────────────────────────────────────────────────────────────


class _SessionThatRecordsRollbacks:
    def __init__(self, rollback_error: Exception | None = None) -> None:
        self.rollbacks = 0
        self._rollback_error = rollback_error

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._rollback_error is not None:
            raise self._rollback_error


def test_a_rollback_that_fails_does_not_replace_the_reason():
    """Откат отказал — наверх всё равно уходит сбой шага, а не сбой отката."""
    session = _SessionThatRecordsRollbacks(ConnectionError("the database is unreachable"))

    @main_mod._rolls_back_unfinished_matching_work
    def a_step(session):
        raise ValueError("the step broke halfway")

    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError, match="halfway"):
        a_step(session)

    assert logs == [
        {
            "event": "matching_work_rollback_failed",
            "log_level": "warning",
            "step": "a_step",
            "error": "ValueError",
            "rollback_error": "ConnectionError: the database is unreachable",
        }
    ]


def test_an_interrupt_is_left_to_the_command():
    """Ctrl-C посреди шага откат не перехватывает: за ним commit не следует.

    Команда выходит, и сессия закрывается без commit; откат на соединении,
    оборванном посреди запроса, здесь был бы лишним обращением к базе.
    """
    session = _SessionThatRecordsRollbacks()

    @main_mod._rolls_back_unfinished_matching_work
    def a_step(session):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        a_step(session)

    assert session.rollbacks == 0
