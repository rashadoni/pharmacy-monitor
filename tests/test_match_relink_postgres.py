"""Ручная замена товара в кластере, пришедшая во время этапа сопоставления.

Этап (конец сбора, `rematch`) держит сессионный замок сопоставления полторы —
три с половиной минуты и всё это время меняет `products.canonical_id`. Запрос
оператора `POST /api/v1/dash/matches/{id}/relink` в эти минуты встаёт на тот же
замок. Пока эндпоинт сначала читал кластер и товар, а на замок вставал уже
внутри `swap_alternative`, после ожидания он работал с прочитанным до него:

* товар того же сайта, добавленный или поставленный этапом, оставался в
  кластере рядом с новым;
* кандидат, которого этап свёл в другую пару, уводился из неё — проверка «товар
  уже в другом сравнении» смотрела на строку, прочитанную до ожидания;
* по распущенному кластеру запрос падал (500) вместо «не найдено».

Держат это две правки. Эндпоинт берёт замок до первого чтения — стенд ниже
проверяет это сам: запрос обязан встать на замок, не прочитав до него ни одной
таблицы сопоставления. И замена читает кластер, кандидата и состав из базы
сама, под замком, — это видят тесты в `tests/test_match_actions.py`, где «другой
писатель» меняет строки мимо сессии.

Запрос ждёт замок недолго (`api.MATCH_EDIT_LOCK_WAIT_SECONDS`) и, не дождавшись,
отказывает — это в `tests/test_match_edits_postgres.py`. Здесь этап отпускает
замок сразу после своей записи, и запрос успевает.

Воспроизводится только на двух настоящих соединениях PostgreSQL: на SQLite
замка нет, тесты пропускаются. Ключ замка один на базу, а не на схему: два
прогона этих тестов в одной базе одновременно мешают друг другу.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from src import api, match_actions, matcher
from src.storage import Base, Match, MatchRejection, Product

_WAITING_FOR_ADVISORY_LOCK = text(
    """
    SELECT count(*) FROM pg_locks
    WHERE locktype = 'advisory' AND NOT granted
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """
)


def _fresh_schema_engines():
    """Два пула на одну свежую схему: сбор и API — разные процессы.

    В общем пуле запрос может получить то самое соединение, которое держит
    сессионный замок, и не встать на него вовсе.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    schema = f"relink_race_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # lock_timeout: замок, оставленный кем-то в этой базе, роняет тест, а не вешает его.
    options = f"-csearch_path={schema} -clock_timeout=20000"
    stage_engine, api_engine = (
        create_engine(url, connect_args={"options": options}) for _ in range(2)
    )
    Base.metadata.create_all(stage_engine)
    try:
        yield stage_engine, api_engine
    finally:
        stage_engine.dispose()
        api_engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def engines():
    yield from _fresh_schema_engines()


def _session(bind) -> Session:
    """Сессия как в `storage.make_session`."""
    return sessionmaker(bind, expire_on_commit=False, autoflush=False)()


def _product(session: Session, site: str, tag: str, canonical_id: int | None = None) -> Product:
    product = Product(
        tenant_id=1,
        site=site,
        external_id=tag,
        url=f"https://{site}.example/product/{tag}",
        name="Aspirin",
        name_normalized="aspirin",
        canonical_id=canonical_id,
    )
    session.add(product)
    session.flush()
    return product


def _cluster(session: Session, sites: list[str]) -> Match:
    match = Match(tenant_id=1, canonical_name="Aspirin", confidence=1.0, is_manual=False)
    session.add(match)
    session.flush()
    for site in sites:
        _product(session, site, f"{site}-member", canonical_id=match.id)
    return match


_APP_TABLES = ("matches", "products", "match_rejections")


def _stored(engine) -> tuple[dict[str, int | None], dict[int, bool], set[tuple[str, str]]]:
    """Привязки товаров, кластеры {id: is_manual} и пары отказов — как в базе."""
    with _session(engine) as session:
        tags = dict(session.execute(select(Product.id, Product.external_id)).all())
        pairing = dict(session.execute(select(Product.external_id, Product.canonical_id)).all())
        clusters = dict(session.execute(select(Match.id, Match.is_manual)).all())
        rejections = {
            tuple(sorted((tags[a], tags[b])))
            for a, b in session.execute(
                select(MatchRejection.product_a_id, MatchRejection.product_b_id)
            )
        }
    return pairing, clusters, rejections


def _wait_until_the_request_is_on_the_lock(stage: Session, thread: threading.Thread) -> bool:
    deadline = time.monotonic() + 15
    while thread.is_alive() and time.monotonic() < deadline:
        if stage.scalar(_WAITING_FOR_ADVISORY_LOCK):
            return True
        time.sleep(0.02)
    return False


def _request_thread(
    api_engine, call: Callable[[Session], object]
) -> tuple[threading.Thread, list[object], list[str]]:
    """Запрос оператора в своём потоке и на своём пуле соединений.

    Отдаёт поток, список с ответом (или кодом отказа, или сбоем) и запросы,
    которые этот пул выполнил, — по ним видно, что запрос успел до замка.
    """
    outcome: list[object] = []
    statements: list[str] = []

    def remember(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(api_engine, "before_cursor_execute", remember)

    def request() -> None:
        with _session(api_engine) as db:
            try:
                outcome.append(call(db))
            except HTTPException as refused:
                outcome.append(refused.status_code)
            except Exception as crashed:
                outcome.append(crashed)

    return threading.Thread(target=request, daemon=True), outcome, statements


def _assert_nothing_was_read_before_the_lock(statements: list[str]) -> None:
    """До замка запрос не трогал ни кластеры, ни товары, ни отказы.

    `statements` — всё, что запрос выполнил к моменту, когда встал на замок.
    """
    assert statements, "запрос не выполнил ни одного запроса"
    assert "pg_advisory_xact_lock" in statements[-1], statements
    read_too_early = [
        statement
        for statement in statements[:-1]
        if any(table in statement for table in _APP_TABLES)
    ]
    assert read_too_early == []


def _request_while_the_stage_holds_the_lock(
    engines,
    seed: Callable[[Session], object],
    stage_change: Callable[[Session], None],
    call: Callable[[Session, object], object],
) -> tuple[object, dict[str, int | None], dict[int, bool], set[tuple[str, str]]]:
    """Запрос приходит под замком этапа; этап меняет базу и отпускает замок.

    `call(db, seeded)` — сам запрос; `seeded` — то, что вернул `seed`. Возвращает
    ответ (или код отказа), привязки товаров, кластеры {id: is_manual} и пары
    отказов — как они лежат в базе после запроса.

    Проверяет сама: запрос встал на замок и до него не прочитал ни одной таблицы
    сопоставления.
    """
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        seeded = seed(session)
        session.commit()

    thread, outcome, statements = _request_thread(api_engine, lambda db: call(db, seeded))

    try:
        # Одно соединение на весь «этап»: сессионный замок принадлежит соединению.
        with stage_engine.connect() as connection, _session(connection) as stage:
            assert matcher.acquire_match_mutation_lock(stage, wait=False)
            thread.start()
            waiting = _wait_until_the_request_is_on_the_lock(stage, thread)
            assert waiting, f"запрос не встал на замок сопоставления: {outcome}"
            _assert_nothing_was_read_before_the_lock(list(statements))
            stage_change(stage)
            stage.commit()
            matcher.release_match_mutation_lock(stage)
            stage.commit()
    finally:
        # И при упавшей проверке: запрос, оставшийся в полёте, держит блокировки
        # в схеме, и её удаление после теста встало бы с ним в тупик.
        if thread.ident is not None:
            thread.join(30)
    assert not thread.is_alive(), "запрос не завершился после снятия замка"

    return outcome[0], *_stored(stage_engine)


def _relink_while_the_stage_holds_the_lock(
    engines,
    seed: Callable[[Session], int],
    stage_change: Callable[[Session], None],
    *,
    site: str = "aloe",
    candidate: str = "candidate",
) -> tuple[object, dict[str, int | None], dict[int, bool], set[tuple[str, str]]]:
    def relink(db: Session, match_id: object) -> object:
        return api.dash_match_relink(
            match_id,
            api.MatchRelinkIn(site=site, url=f"https://{site}.example/product/{candidate}"),
            user=SimpleNamespace(id=7, tenant_id=1),
            db=db,
        )

    return _request_while_the_stage_holds_the_lock(engines, seed, stage_change, relink)


def _by_tag(session: Session, tag: str) -> Product:
    return session.scalars(select(Product).where(Product.external_id == tag)).one()


def test_relink_replaces_a_same_site_product_the_stage_added_meanwhile(engines):
    """Этап добавил в кластер товар aloe — замена ставит новый вместо него, а не рядом."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "added-by-stage")
        _product(session, "aloe", "candidate")
        match_ids.append(match.id)
        return match.id

    def stage_adds_an_aloe_member(stage: Session) -> None:
        _by_tag(stage, "added-by-stage").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _relink_while_the_stage_holds_the_lock(
        engines, seed, stage_adds_an_aloe_member
    )

    assert response["ok"] is True
    assert pairing == {
        "pharmonline-member": match_ids[0],
        "aptekonline-member": match_ids[0],
        "added-by-stage": None,
        "candidate": match_ids[0],
    }
    assert clusters == {match_ids[0]: True}
    assert rejections == {("added-by-stage", "candidate")}


def test_relink_replaces_the_member_the_stage_put_in_meanwhile(engines):
    """Этап передал место сайта живому двойнику — заменяется двойник, а не прежний товар."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        _product(session, "aloe", "twin")
        _product(session, "aloe", "candidate")
        match_ids.append(match.id)
        return match.id

    def stage_gives_the_slot_to_the_twin(stage: Session) -> None:
        _by_tag(stage, "aloe-member").canonical_id = None
        _by_tag(stage, "twin").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _relink_while_the_stage_holds_the_lock(
        engines, seed, stage_gives_the_slot_to_the_twin
    )

    assert response["ok"] is True
    assert pairing == {
        "pharmonline-member": match_ids[0],
        "aloe-member": None,
        "twin": None,
        "candidate": match_ids[0],
    }
    assert clusters == {match_ids[0]: True}
    assert rejections == {("candidate", "twin")}


def test_relink_refuses_a_candidate_the_stage_paired_meanwhile(engines):
    """Этап свёл кандидата в другую пару — отказ 409, чужая пара цела."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        _product(session, "aptekonline", "other-pair-member")
        _product(session, "aloe", "candidate")
        match_ids.append(match.id)
        return match.id

    def stage_pairs_the_candidate(stage: Session) -> None:
        other = Match(tenant_id=1, canonical_name="Aspirin 2", confidence=0.9, is_manual=False)
        stage.add(other)
        stage.flush()
        match_ids.append(other.id)
        _by_tag(stage, "other-pair-member").canonical_id = other.id
        _by_tag(stage, "candidate").canonical_id = other.id

    response, pairing, clusters, rejections = _relink_while_the_stage_holds_the_lock(
        engines, seed, stage_pairs_the_candidate
    )

    asked, other = match_ids
    assert response == 409
    assert pairing == {
        "pharmonline-member": asked,
        "aloe-member": asked,
        "other-pair-member": other,
        "candidate": other,
    }
    assert clusters == {asked: False, other: False}
    assert rejections == set()


def test_relink_of_a_cluster_the_stage_dissolved_meanwhile_is_not_found(engines):
    """Этап распустил кластер — отказ 404, а не сбой записи в удалённую строку."""

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        _product(session, "aloe", "candidate")
        return match.id

    def stage_dissolves_the_cluster(stage: Session) -> None:
        for tag in ("pharmonline-member", "aloe-member"):
            _by_tag(stage, tag).canonical_id = None
        stage.flush()
        stage.delete(stage.scalars(select(Match)).one())

    response, pairing, clusters, rejections = _relink_while_the_stage_holds_the_lock(
        engines, seed, stage_dissolves_the_cluster
    )

    assert response == 404
    assert pairing == {"pharmonline-member": None, "aloe-member": None, "candidate": None}
    assert clusters == {}
    assert rejections == set()


def test_relink_with_a_bad_request_does_not_wait_for_the_stage(engines, monkeypatch):
    """Отказ, видный из самого запроса, приходит сразу и своим кодом."""
    monkeypatch.setattr(api, "MATCH_EDIT_LOCK_WAIT_SECONDS", 0.3)
    stage_engine, api_engine = engines
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            with _session(api_engine) as db, pytest.raises(HTTPException) as refused:
                # Встав на замок, запрос получил бы 409 «идёт сопоставление».
                api.dash_match_relink(
                    1,
                    api.MatchRelinkIn(site="unknown-site", url="https://x.example/product/y"),
                    user=SimpleNamespace(id=7, tenant_id=1),
                    db=db,
                )
        finally:
            matcher.release_match_mutation_lock(stage)

    assert refused.value.status_code == 400


# --- relink-dead: план не зависит от того, как строки лежат в таблице ----------
#
# Без ORDER BY PostgreSQL отдаёт строки в порядке хранения, а запись в строку
# переносит её в конец — сбор же пишет в товары постоянно. Пробный прогон сегодня
# и настоящий завтра обязаны дать один план: и очерёдность кластеров, и выбор
# между одинаково похожими кандидатами. На SQLite не воспроизводится: там
# порядок без ORDER BY совпадает с порядком id.


def test_relink_dead_plan_does_not_depend_on_row_storage_order(engines):
    stage_engine, _api_engine = engines
    with _session(stage_engine) as session:
        clusters = []
        for index in (1, 2):
            match = Match(tenant_id=1, canonical_name="Aspirin", confidence=1.0, is_manual=False)
            session.add(match)
            session.flush()
            _product(session, "pharmonline", f"anchor-{index}", canonical_id=match.id)
            dead = _product(session, "aloe", f"dead-{index}", canonical_id=match.id)
            dead.url_dead_at = datetime(2026, 5, 30)
            clusters.append(match.id)
        candidates = [_product(session, "aloe", f"live-{index}").id for index in (1, 2)]
        session.commit()
        # Запись в индексированную колонку (её пишет каждый сбор) — строка
        # переезжает и в таблице, и в индексах.
        session.execute(
            text(
                "UPDATE products SET availability_run_id = 1 "
                "WHERE external_id IN ('dead-1', 'live-1')"
            )
        )
        session.commit()
        # Иначе тест ничего не проверяет: те же выборки без ORDER BY должны
        # отдать строки не в порядке id.
        unordered_dead = "SELECT external_id FROM products WHERE url_dead_at IS NOT NULL"
        unordered_free = (
            "SELECT external_id FROM products WHERE site = 'aloe' AND canonical_id IS NULL"
        )
        assert list(session.scalars(text(unordered_dead))) == ["dead-2", "dead-1"]
        assert list(session.scalars(text(unordered_free))) == ["live-2", "live-1"]

        plan = matcher.relink_dead_members(session, dry_run=True)

    assert [(entry["match_id"], entry["action"], entry["new"]) for entry in plan] == [
        (clusters[0], "swap", candidates[0]),
        (clusters[1], "swap", candidates[1]),
    ]


def test_swap_refusal_names_the_same_member_whatever_the_storage_order(engines):
    """Непригодных товаров в кластере два — причиной назван один и тот же."""
    stage_engine, _api_engine = engines
    with _session(stage_engine) as session:
        match = Match(tenant_id=1, canonical_name="Aspirin", confidence=1.0, is_manual=False)
        session.add(match)
        session.flush()
        dead = _product(session, "pharmonline", "dead-member", canonical_id=match.id)
        dead.url_dead_at = datetime(2026, 5, 30)
        sold_out = _product(session, "aptekonline", "sold-out-member", canonical_id=match.id)
        sold_out.offer_availability_status = "out_of_stock"
        candidate = _product(session, "aloe", "candidate")
        session.commit()
        session.execute(
            text("UPDATE products SET availability_run_id = 1 WHERE external_id = 'dead-member'")
        )
        session.commit()
        # Иначе тест ничего не проверяет: без ORDER BY первым идёт не меньший id.
        unordered = text("SELECT external_id FROM products WHERE canonical_id = :match")
        assert list(session.scalars(unordered, {"match": match.id})) == [
            "sold-out-member",
            "dead-member",
        ]

        outcome = match_actions.try_swap_alternative(
            session, match.id, "aloe", candidate.id, dry_run=True
        )

    assert outcome == match_actions.SwapOutcome(
        False, f"offer:dead_url product={dead.id} site=pharmonline"
    )
