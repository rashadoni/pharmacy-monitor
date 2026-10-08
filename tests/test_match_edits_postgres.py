"""Правка состава кластера оператором, пришедшая во время этапа сопоставления.

Этап (конец сбора, `rematch`) держит сессионный замок сопоставления полторы —
три с половиной минуты. Товары он читает в память в начале и дальше работает с
копией, а `products.canonical_id` пишет всё это время. Отсюда два разных сбоя у
запроса, который правит состав без замка до чтения:

* **встал на замок с уже прочитанным составом** (`reject` брал замок внутри
  `add_rejection`): после ожидания писал по прежнему. Товар, который этап за
  это время свёл в другую пару, выдёргивался из неё, по распущенному кластеру
  записывались отказы, по кластеру с добавленным товаром запрос падал;
* **замок не брал вовсе** (`confirm`, `add-product`, `create-with-products`,
  `reject` кластера из одного товара): записывал посреди этапа и отвечал
  «готово», а этап, не видя правки, ставил этим же товарам свои кластеры —
  добавленный оператором товар уезжал, созданный им кластер оставался пустым.

Правило одно: замок — до первого чтения (`api._lock_match_edits`). Стенд
`_request_while_the_stage_holds_the_lock` проверяет его сам: запрос обязан
встать на замок, не прочитав до него ни одной таблицы сопоставления.

Ждёт запрос недолго (`api.MATCH_EDIT_LOCK_WAIT_SECONDS`): дашборд обрывает его
через 30 секунд и предлагает повторить, а этап идёт минуты — правка, записанная
после этого «не удалось», записалась бы повтором ещё раз. Не дождавшись, запрос
отказывает (`matching_in_progress`), ничего не прочитав и не записав. В тестах
первой половины файла этап отпускает замок сразу после своей записи, и запрос
успевает; отказ — в разделе «пока идёт этап».

Замена товара (`relink`) — в `tests/test_match_relink_postgres.py`; стенд оттуда.
Только PostgreSQL: на SQLite замка нет, тесты пропускаются.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src import api, match_actions, match_lock, matcher
from src import main as main_module
from src.storage import Match, Product
from tests.test_match_relink_postgres import (
    _APP_TABLES,
    _assert_nothing_was_read_before_the_lock,
    _by_tag,
    _cluster,
    _fresh_schema_engines,
    _product,
    _request_thread,
    _request_while_the_stage_holds_the_lock,
    _session,
    _stored,
    _wait_until_the_request_is_on_the_lock,
)

_OPERATOR = SimpleNamespace(id=7, tenant_id=1)


@pytest.fixture
def engines():
    yield from _fresh_schema_engines()


def _reject(db: Session, match_id: object) -> int:
    return api.dash_match_reject(match_id, user=_OPERATOR, db=db).status_code


def _confirm(db: Session, match_id: object) -> int:
    return api.dash_match_confirm(match_id, user=_OPERATOR, db=db).status_code


def _ids(session: Session) -> dict[str, int]:
    """Номера товаров по меткам: запрос называет товар номером, как и оператор."""
    session.flush()
    return {tag: pid for pid, tag in session.execute(select(Product.id, Product.external_id))}


def _add_product(tag: str) -> Callable[[Session, object], object]:
    """Посев такого запроса возвращает {"match": номер кластера, "ids": _ids(...)}."""

    def call(db: Session, seeded: object) -> object:
        return api.dash_match_add_product(
            seeded["match"],
            api._AddProductPayload(product_id=seeded["ids"][tag]),
            user=_OPERATOR,
            db=db,
        )

    return call


def _dissolve_the_only_cluster(stage: Session) -> None:
    for product in stage.scalars(select(Product).where(Product.canonical_id.is_not(None))):
        product.canonical_id = None
    stage.flush()
    stage.delete(stage.scalars(select(Match)).one())


def _pair_into_a_new_cluster(stage: Session, *tags: str) -> int:
    other = Match(tenant_id=1, canonical_name="Aspirin 2", confidence=0.9, is_manual=False)
    stage.add(other)
    stage.flush()
    for tag in tags:
        _by_tag(stage, tag).canonical_id = other.id
    return other.id


# --- reject: «не один товар» ---------------------------------------------------


def test_reject_takes_apart_the_cluster_with_the_product_the_stage_added(engines):
    """Этап добавил в кластер товар — отклонение разбирает кластер целиком.

    По составу, прочитанному до ожидания, запрос падал: удалить кластер, в
    котором остался не известный ему товар, база не даёт.
    """
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "added-by-stage")
        match_ids.append(match.id)
        return match.id

    def stage_adds_an_aloe_member(stage: Session) -> None:
        _by_tag(stage, "added-by-stage").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_adds_an_aloe_member, _reject
    )

    assert response == 204
    assert pairing == {
        "pharmonline-member": None,
        "aptekonline-member": None,
        "added-by-stage": None,
    }
    assert clusters == {}
    assert rejections == {
        ("aptekonline-member", "pharmonline-member"),
        ("added-by-stage", "pharmonline-member"),
        ("added-by-stage", "aptekonline-member"),
    }


def test_reject_leaves_alone_a_product_the_stage_moved_to_another_pair(engines):
    """Этап свёл товар в другую пару — отклонение его оттуда не выдёргивает."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aptekonline", "aloe"])
        _product(session, "aptekonline", "other-pair-member")
        match_ids.append(match.id)
        return match.id

    def stage_moves_the_aloe_member(stage: Session) -> None:
        match_ids.append(_pair_into_a_new_cluster(stage, "other-pair-member", "aloe-member"))

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_moves_the_aloe_member, _reject
    )

    _asked, other = match_ids
    assert response == 204
    assert pairing == {
        "pharmonline-member": None,
        "aptekonline-member": None,
        "aloe-member": other,
        "other-pair-member": other,
    }
    assert clusters == {other: False}
    assert rejections == {("aptekonline-member", "pharmonline-member")}


def test_reject_takes_apart_the_cluster_with_the_twin_the_stage_put_in(engines):
    """Этап передал место сайта живому двойнику — отказ пишется с двойником."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        _product(session, "aloe", "twin")
        match_ids.append(match.id)
        return match.id

    def stage_gives_the_slot_to_the_twin(stage: Session) -> None:
        _by_tag(stage, "aloe-member").canonical_id = None
        _by_tag(stage, "twin").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_gives_the_slot_to_the_twin, _reject
    )

    assert response == 204
    assert pairing == {"pharmonline-member": None, "aloe-member": None, "twin": None}
    assert clusters == {}
    assert rejections == {("pharmonline-member", "twin")}


def test_reject_of_a_cluster_the_stage_dissolved_is_not_found(engines):
    """Этап распустил кластер — «не найдено», и ни одного отказа от имени оператора."""

    def seed(session: Session) -> int:
        return _cluster(session, ["pharmonline", "aloe"]).id

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, _dissolve_the_only_cluster, _reject
    )

    assert response == 404
    assert pairing == {"pharmonline-member": None, "aloe-member": None}
    assert clusters == {}
    assert rejections == set()


def test_reject_of_a_one_product_cluster_waits_for_the_stage(engines):
    """Кластер из одного товара: отказов писать не о ком, но замок нужен и тут.

    Раньше замок брался только при записи отказа, и такой кластер удалялся
    посреди этапа — этап, привязывая к нему товар, падал на внешнем ключе.
    """
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline"])
        _product(session, "aloe", "added-by-stage")
        match_ids.append(match.id)
        return match.id

    def stage_adds_a_second_member(stage: Session) -> None:
        _by_tag(stage, "added-by-stage").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_adds_a_second_member, _reject
    )

    assert response == 204
    assert pairing == {"pharmonline-member": None, "added-by-stage": None}
    assert clusters == {}
    assert rejections == {("added-by-stage", "pharmonline-member")}


# --- confirm: «подтвердить» ----------------------------------------------------


def test_confirm_marks_the_cluster_as_the_stage_left_it(engines):
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        _product(session, "aloe", "twin")
        match_ids.append(match.id)
        return match.id

    def stage_gives_the_slot_to_the_twin(stage: Session) -> None:
        _by_tag(stage, "aloe-member").canonical_id = None
        _by_tag(stage, "twin").canonical_id = match_ids[0]

    response, pairing, clusters, rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_gives_the_slot_to_the_twin, _confirm
    )

    assert response == 204
    assert pairing == {
        "pharmonline-member": match_ids[0],
        "aloe-member": None,
        "twin": match_ids[0],
    }
    assert clusters == {match_ids[0]: True}
    assert rejections == set()


def test_confirm_of_a_cluster_the_stage_dissolved_is_not_found(engines):
    def seed(session: Session) -> int:
        return _cluster(session, ["pharmonline", "aloe"]).id

    response, pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, _dissolve_the_only_cluster, _confirm
    )

    assert response == 404
    assert pairing == {"pharmonline-member": None, "aloe-member": None}
    assert clusters == {}


def test_confirm_checks_the_members_the_stage_left(engines):
    """Проверка наличия смотрит на состав после этапа, а не на прочитанный до него."""
    match_ids: list[int] = []

    def seed(session: Session) -> int:
        match = _cluster(session, ["pharmonline", "aloe"])
        sold_out = _product(session, "aptekonline", "sold-out")
        sold_out.offer_availability_status = "out_of_stock"
        match_ids.append(match.id)
        return match.id

    def stage_adds_a_sold_out_member(stage: Session) -> None:
        _by_tag(stage, "sold-out").canonical_id = match_ids[0]

    response, _pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_adds_a_sold_out_member, _confirm
    )

    assert response == 409
    assert clusters == {match_ids[0]: False}


# --- add-product: «добавить товар в сравнение» ---------------------------------


def test_add_product_refuses_a_site_the_stage_filled_meanwhile(engines):
    """Этап поставил в кластер товар того же сайта — второй рядом не встаёт."""
    match_ids: list[int] = []

    def seed(session: Session) -> dict:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "added-by-stage")
        _product(session, "aloe", "chosen-by-operator")
        match_ids.append(match.id)
        return {"match": match.id, "ids": _ids(session)}

    def stage_adds_an_aloe_member(stage: Session) -> None:
        _by_tag(stage, "added-by-stage").canonical_id = match_ids[0]

    response, pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_adds_an_aloe_member, _add_product("chosen-by-operator")
    )

    assert response == 409
    assert pairing == {
        "pharmonline-member": match_ids[0],
        "aptekonline-member": match_ids[0],
        "added-by-stage": match_ids[0],
        "chosen-by-operator": None,
    }
    assert clusters == {match_ids[0]: False}


def test_add_product_to_a_cluster_the_stage_dissolved_is_not_found(engines):
    def seed(session: Session) -> dict:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "chosen-by-operator")
        return {"match": match.id, "ids": _ids(session)}

    response, pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, _dissolve_the_only_cluster, _add_product("chosen-by-operator")
    )

    assert response == 404
    assert pairing == {
        "pharmonline-member": None,
        "aptekonline-member": None,
        "chosen-by-operator": None,
    }
    assert clusters == {}


def test_add_product_answers_with_the_members_the_stage_left(engines):
    """Ответ называет состав, который лежит в базе, — вместе с двойником от этапа."""
    seeded: dict = {}

    def seed(session: Session) -> dict:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aptekonline", "twin")
        _product(session, "aloe", "chosen-by-operator")
        seeded.update(match=match.id, ids=_ids(session))
        return seeded

    def stage_gives_the_slot_to_the_twin(stage: Session) -> None:
        _by_tag(stage, "aptekonline-member").canonical_id = None
        _by_tag(stage, "twin").canonical_id = seeded["match"]

    response, pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_gives_the_slot_to_the_twin, _add_product("chosen-by-operator")
    )

    in_the_cluster = {tag for tag, match_id in pairing.items() if match_id == seeded["match"]}
    assert in_the_cluster == {"pharmonline-member", "twin", "chosen-by-operator"}
    assert {seeded["ids"][tag] for tag in in_the_cluster} == {
        product["product_id"] for product in response["products"]
    }
    assert clusters == {seeded["match"]: True}


# --- create-with-products: «создать сравнение» ---------------------------------


def _create(*tags: str) -> Callable[[Session, object], object]:
    """Посев такого запроса возвращает {"ids": _ids(...)}."""

    def call(db: Session, seeded: object) -> object:
        return api.dash_match_create_with_products(
            api._CreateMatchPayload(product_ids=[seeded["ids"][tag] for tag in tags]),
            user=_OPERATOR,
            db=db,
        )

    return call


def test_create_refuses_products_the_stage_made_incompatible(engines):
    """Проверка наличия смотрит на товары после этапа."""

    def seed(session: Session) -> dict:
        _product(session, "pharmonline", "first")
        _product(session, "aloe", "second")
        return {"ids": _ids(session)}

    def stage_sees_the_product_sold_out(stage: Session) -> None:
        _by_tag(stage, "second").offer_availability_status = "out_of_stock"

    response, pairing, clusters, _rejections = _request_while_the_stage_holds_the_lock(
        engines, seed, stage_sees_the_product_sold_out, _create("first", "second")
    )

    assert response == 409
    assert pairing == {"first": None, "second": None}
    assert clusters == {}


def test_create_with_a_repeated_product_does_not_wait_for_the_stage(engines):
    """Отказ, видный из самого запроса, приходит сразу, а не после этапа."""
    stage_engine, api_engine = engines
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            with _session(api_engine) as db, pytest.raises(HTTPException) as refused:
                # Ожидание замка здесь оборвалось бы ошибкой базы, а не отказом 404.
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                api.dash_match_create_with_products(
                    api._CreateMatchPayload(product_ids=[5, 5]), user=_OPERATOR, db=db
                )
        finally:
            matcher.release_match_mutation_lock(stage)

    assert refused.value.status_code == 404


# --- пока идёт этап: отказ, а не ожидание --------------------------------------


def _relink(db: Session, seeded: object) -> object:
    return api.dash_match_relink(
        seeded["match"],
        api.MatchRelinkIn(site="aloe", url="https://aloe.example/product/candidate"),
        user=_OPERATOR,
        db=db,
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda db, seeded: _reject(db, seeded["match"]),
        lambda db, seeded: _confirm(db, seeded["match"]),
        _relink,
        _add_product("candidate"),
        _create("spare", "candidate"),
    ],
    ids=["reject", "confirm", "relink", "add-product", "create-with-products"],
)
def test_an_edit_made_while_the_stage_runs_is_refused_and_changes_nothing(
    engines, monkeypatch, edit
):
    """Этап держит замок дольше, чем запрос готов ждать: отказ с кодом, база прежняя."""
    monkeypatch.setattr(api, "MATCH_EDIT_LOCK_WAIT_SECONDS", 0.3)
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "candidate")
        _product(session, "pharmonline", "spare")
        seeded = {"match": match.id, "ids": _ids(session)}
        session.commit()
    before = _stored(stage_engine)
    thread, outcome, statements = _request_thread(api_engine, lambda db: edit(db, seeded))

    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            thread.start()
            thread.join(10)
            refused_while_the_stage_ran = not thread.is_alive()
        finally:
            matcher.release_match_mutation_lock(stage)
            stage.commit()
    thread.join(30)

    assert refused_while_the_stage_ran, "запрос ждал этап дольше отведённого"
    assert outcome == [409]
    assert [s for s in statements if any(table in s for table in _APP_TABLES)] == []
    assert _stored(stage_engine) == before
    # Отказавший запрос ничего не держит: ни замка, ни соединения пула.
    assert api_engine.pool.checkedout() == 0


def test_the_refusal_names_its_reason_for_the_dashboard(engines, monkeypatch):
    """По коду дашборд показывает текст на языке оператора."""
    monkeypatch.setattr(api, "MATCH_EDIT_LOCK_WAIT_SECONDS", 0.3)
    stage_engine, api_engine = engines
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            with _session(api_engine) as db, pytest.raises(api.MatchEditRefused) as refused:
                api.dash_match_confirm(1, user=_OPERATOR, db=db)
        finally:
            matcher.release_match_mutation_lock(stage)

    assert (refused.value.status_code, refused.value.code) == (409, "matching_in_progress")
    assert "идёт сопоставление" in refused.value.detail


def test_the_wait_limit_covers_the_matcher_lock_only(engines, monkeypatch):
    """Срок — на замок сопоставления, а не на всю правку.

    Строку кластера держит другая транзакция (так сбор держит строки товаров).
    Правка, уже взявшая замок сопоставления, ждёт её дольше своего срока и
    записывается, а не падает.
    """
    monkeypatch.setattr(api, "MATCH_EDIT_LOCK_WAIT_SECONDS", 0.3)
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        match_id = _cluster(session, ["pharmonline", "aloe"]).id
        session.commit()
    thread, outcome, _statements = _request_thread(api_engine, lambda db: _confirm(db, match_id))
    waiting_for_the_row = text(
        """
        SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
        WHERE NOT l.granted AND l.locktype <> 'advisory' AND a.datname = current_database()
        """
    )

    with _session(stage_engine) as writer:
        writer.execute(Match.__table__.update().where(Match.id == match_id).values(confidence=0.5))
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not writer.scalar(waiting_for_the_row) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert writer.scalar(waiting_for_the_row), f"правка не дошла до строки: {outcome}"
            time.sleep(3 * api.MATCH_EDIT_LOCK_WAIT_SECONDS)
            still_waiting = thread.is_alive()
        finally:
            writer.commit()
    thread.join(30)

    assert still_waiting, f"ожидание строки оборвалось по сроку замка: {outcome}"
    assert outcome == [204]
    assert _stored(stage_engine)[1] == {match_id: True}


def test_a_lock_wait_that_ran_out_leaves_the_session_usable(engines):
    stage_engine, api_engine = engines
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            with _session(api_engine) as db:
                assert match_lock.acquire_match_mutation_xact_lock_within(db, 0.2) is False
                # Транзакция откатана, соединение вернулось в пул.
                assert api_engine.pool.checkedout() == 0
                assert db.scalar(text("SELECT 1")) == 1
        finally:
            matcher.release_match_mutation_lock(stage)

    with _session(api_engine) as db:
        assert match_lock.acquire_match_mutation_xact_lock_within(db, 0.2) is True


def test_a_lock_wait_broken_by_something_else_is_an_error_not_a_refusal(engines):
    """«Идёт сопоставление» — только про истёкший срок замка, не про любой сбой базы."""
    stage_engine, api_engine = engines
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        try:
            with _session(api_engine) as db, pytest.raises(OperationalError) as broken:
                db.execute(text("SET LOCAL statement_timeout = 200"))
                match_lock.acquire_match_mutation_xact_lock_within(db, 5)
        finally:
            matcher.release_match_mutation_lock(stage)

    assert broken.value.orig.sqlstate == "57014"  # query_canceled


def test_two_operators_adding_to_one_cluster_take_turns(engines):
    """Замок исключает и двух операторов, а не только оператора и этап.

    Оба добавляют в кластер товар aloe. Без взаимного исключения каждый видел бы
    кластер без товара aloe, и в нём оказались бы оба.
    """
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        match = _cluster(session, ["pharmonline", "aptekonline"])
        _product(session, "aloe", "first")
        _product(session, "aloe", "second")
        seeded = {"match": match.id, "ids": _ids(session)}
        session.commit()
    thread, outcome, statements = _request_thread(
        api_engine, lambda db: _add_product("second")(db, seeded)
    )

    # Первый оператор — та же правка на полпути: замок взят, товар привязан,
    # commit ещё не сделан.
    with _session(stage_engine) as first:
        match_lock.acquire_match_mutation_xact_lock(first)
        _by_tag(first, "first").canonical_id = seeded["match"]
        first.flush()
        thread.start()
        try:
            waiting = _wait_until_the_request_is_on_the_lock(first, thread)
            read_before_the_lock = list(statements)
        finally:
            first.commit()
    thread.join(30)

    assert waiting, f"второй оператор не ждал первого: {outcome}"
    _assert_nothing_was_read_before_the_lock(read_before_the_lock)
    assert outcome == [409]
    pairing, clusters, _rejections = _stored(stage_engine)
    assert pairing["first"] == seeded["match"] and pairing["second"] is None


def test_edits_are_accepted_as_soon_as_the_stage_returns(engines, monkeypatch):
    """Этап вернул управление — его замок снят, даже если он ничего не изменил.

    Перепроверка берёт транзакционный замок всегда, а коммитит, только когда
    кого-то разобрала. Без commit в конце этапа замок держался бы до следующего
    commit сбора — пока тот подтверждает цены и считает алерты.
    """
    monkeypatch.setattr(api, "MATCH_EDIT_LOCK_WAIT_SECONDS", 0.5)
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        match_id = _cluster(session, ["pharmonline", "aloe"]).id
        matcher.refresh_derived_fields(session)
        session.commit()

    with stage_engine.connect() as connection, _session(connection) as run:
        assert main_module._acquire_matcher_lock(run, wait=False)
        summary = main_module._run_matching_stage(run)
        main_module._release_matcher_lock(run)
        # Сбор идёт дальше в этой же сессии и пока ничего не коммитит.
        with _session(api_engine) as db:
            response = _confirm(db, match_id)

    assert summary["revalidated"] == 0, "этап что-то разобрал и закоммитил сам — тест пуст"
    assert response == 204


# --- правка посреди настоящего этапа -------------------------------------------
#
# Здесь этап — сам `matcher.match_products`, а не подставленная запись. Он читает
# товары в память и сводит P с Q (одинаковые названия на двух сайтах). Запрос
# оператора приходит сразу после этого чтения. Без замка он записывал правку и
# отвечал «готово», а этап, для которого P всё ещё без пары, уводил P к себе.


def _named(session: Session, site: str, tag: str, name: str, canonical_id=None) -> Product:
    product = _product(session, site, tag, canonical_id=canonical_id)
    product.name = name
    product.name_normalized = name.lower()
    return product


def _edit_during_a_real_stage(engines, monkeypatch, seed, call):
    """Ответ запроса, номера товаров и то, что лежит в базе после этапа и запроса.

    `seed` возвращает номер кластера оператора (или None); запрос получает
    {"match": этот номер, "ids": номера товаров по меткам}.
    """
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        seeded = {"match": seed(session)}
        matcher.refresh_derived_fields(session)
        seeded["ids"] = _ids(session)
        session.commit()

    thread, outcome, statements = _request_thread(api_engine, lambda db: call(db, seeded))
    products_are_in_memory = matcher.latest_snapshots_per_product
    arrived: list[bool] = []

    with stage_engine.connect() as connection, _session(connection) as stage:

        def request_arrives(session, product_ids):
            # Первое, что этап делает после чтения товаров.
            if not arrived:
                thread.start()
                arrived.append(_wait_until_the_request_is_on_the_lock(stage, thread))
            return products_are_in_memory(session, product_ids)

        monkeypatch.setattr(matcher, "latest_snapshots_per_product", request_arrives)
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        assert matcher.match_products(stage) >= 1
        stage.commit()
        matcher.release_match_mutation_lock(stage)
        stage.commit()
    assert arrived == [True], f"запрос не встал на замок сопоставления: {outcome}"
    _assert_nothing_was_read_before_the_lock(statements[: _first_lock(statements) + 1])
    thread.join(30)
    assert not thread.is_alive(), "запрос не завершился после снятия замка"
    return outcome[0], seeded, *_stored(stage_engine)


def _first_lock(statements: list[str]) -> int:
    return next(i for i, statement in enumerate(statements) if "pg_advisory_xact_lock" in statement)


def test_a_product_added_during_a_real_stage_stays_where_the_operator_put_it(engines, monkeypatch):
    def seed(session: Session) -> int:
        _named(session, "pharmonline", "P", "Aspirin Kardio 100 mq 30 tab")
        _named(session, "aptekonline", "Q", "Aspirin Kardio 100 mq 30 tab")
        match = Match(tenant_id=1, canonical_name="ASK kardio", confidence=0.8, is_manual=False)
        session.add(match)
        session.flush()
        _named(session, "aptekonline", "A", "ASK kardio 100", canonical_id=match.id)
        _named(session, "aloe", "L", "ASK kardio 100", canonical_id=match.id)
        return match.id

    response, seeded, pairing, clusters, _rejections = _edit_during_a_real_stage(
        engines, monkeypatch, seed, _add_product("P")
    )

    operators = seeded["match"]
    assert {tag for tag, match_id in pairing.items() if match_id == operators} == {"P", "A", "L"}
    assert {product["product_id"] for product in response["products"]} == {
        seeded["ids"][tag] for tag in ("P", "A", "L")
    }
    assert clusters[operators] is True


def test_a_cluster_created_during_a_real_stage_keeps_its_products(engines, monkeypatch):
    def seed(session: Session) -> None:
        _named(session, "pharmonline", "P", "Aspirin Kardio 100 mq 30 tab")
        _named(session, "aptekonline", "Q", "Aspirin Kardio 100 mq 30 tab")
        _named(session, "aloe", "C", "Aspirin Kardio 100 mq 30 tab")

    response, _seeded, pairing, clusters, _rejections = _edit_during_a_real_stage(
        engines, monkeypatch, seed, _create("P", "C")
    )

    created = response["match_id"]
    assert {tag for tag, match_id in pairing.items() if match_id == created} == {"P", "C"}
    assert clusters[created] is True


# --- ручные операции match_actions ждут тот же замок ---------------------------
#
# Замок у этапа и у ручных операций один (`src.match_lock`). Раньше помощников
# было два, с одинаковым ключом в двух модулях, и ничто не проверяло, что
# операция из `match_actions` в самом деле встаёт на замок этапа.


@pytest.mark.parametrize(
    "operation",
    [
        lambda db, ids: match_actions.add_rejection(db, ids["x"], ids["z"]),
        lambda db, ids: match_actions.confirm_match(db, ids["match"]),
        lambda db, ids: match_actions.break_match(db, ids["match"], ids["z"]),
        lambda db, ids: match_actions.try_swap_alternative(
            db, ids["match"], "aloe", ids["candidate"], dry_run=True
        ),
    ],
    ids=["add_rejection", "confirm_match", "break_match", "try_swap_alternative"],
)
def test_manual_operations_wait_for_the_stage(engines, operation):
    stage_engine, api_engine = engines
    with _session(stage_engine) as session:
        match = _cluster(session, ["pharmonline", "aloe"])
        candidate = _product(session, "aloe", "candidate")
        ids = {
            "match": match.id,
            "x": _by_tag(session, "pharmonline-member").id,
            "z": _by_tag(session, "aloe-member").id,
            "candidate": candidate.id,
        }
        session.commit()

    thread, outcome, statements = _request_thread(api_engine, lambda db: operation(db, ids))
    with stage_engine.connect() as connection, _session(connection) as stage:
        assert matcher.acquire_match_mutation_lock(stage, wait=False)
        thread.start()
        waiting = _wait_until_the_request_is_on_the_lock(stage, thread)
        read_before_the_lock = list(statements)
        matcher.release_match_mutation_lock(stage)
        stage.commit()
    thread.join(30)

    assert waiting, f"операция не встала на замок сопоставления: {outcome}"
    _assert_nothing_was_read_before_the_lock(read_before_the_lock)
    assert not thread.is_alive()
    assert not isinstance(outcome[0], Exception), outcome
