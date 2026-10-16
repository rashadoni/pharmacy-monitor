"""Real PostgreSQL rollback, competing writers and replay (required in CI)."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from src import pharmonline_identity_isolation as isolation, storage
from tests.test_pharmonline_identity_isolation import _prepare, _run, _seed
from tests.test_pharmonline_scheduled_admission import _catalog


@pytest.fixture
def pg_factory():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        if os.environ.get("CI"):
            pytest.fail("PostgreSQL is required for identity isolation concurrency in CI")
        pytest.skip("PostgreSQL CI gate; no database configured here")
    schema = "isolation_" + uuid.uuid4().hex
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, hide_parameters=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    storage.Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (
            storage.Product,
            storage.PharmonlinePublicAPIIdentityAdmission,
            storage.PharmonlineIdentityConflict,
            storage.PharmonlineIdentityConflictVariant,
        )
    )


def test_database_failure_rolls_back_admissions_markers_and_evidence(pg_factory, monkeypatch):
    with pg_factory() as session:
        _, source = _seed(session)
        run = _run(session)
        from sqlalchemy import event

        def reject_variant(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO pharmonline_identity_conflict_variants"):
                # Actual aborted PostgreSQL transaction after preceding writes.
                return "SELECT 1 / 0", {}
            return statement, parameters

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", reject_variant, retval=True)
        try:
            with pytest.raises(DBAPIError):
                _prepare(session, source, run)
        finally:
            event.remove(engine, "before_cursor_execute", reject_variant)
        assert _counts(session) == (288, 0, 0, 0)
        assert not session.scalar(
            select(storage.Product.id).where(storage.Product.identity_conflicted.is_(True))
        )
        _prepare(session, source, run)
        assert _counts(session) == (300, 13, 3, 3)


def test_competing_writers_serialize_or_refuse_then_replay_idempotently(pg_factory, monkeypatch):
    with pg_factory() as seed_session:
        _, source = _seed(seed_session)
        first, second = _run(seed_session), _run(seed_session)
        first_id, second_id = first.id, second.id
    entered, release = Event(), Event()
    classify = isolation._classify

    def held_classify(session, results, tenant_id):
        result = classify(session, results, tenant_id)
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "writer release timeout"
        return result

    monkeypatch.setattr(isolation, "_classify", held_classify)

    def write(run_id):
        with pg_factory() as session:
            try:
                isolation.prepare_catalog(
                    session, source, tenant_id=1, run_id=run_id, source_transport="direct"
                )
                return "committed"
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "40001"
                return "serialization_refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(write, first_id)
        assert entered.wait(10)
        b = pool.submit(write, second_id)
        try:
            deadline = time.monotonic() + 5
            blocked = False
            with pg_factory() as observer:
                while time.monotonic() < deadline:
                    blocked = bool(
                        observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                                "AND classid=721109 AND NOT granted"
                            )
                        )
                    )
                    if blocked:
                        break
                    time.sleep(0.02)
            assert blocked, "second writer must wait for the identity transaction lock"
        finally:
            release.set()
        assert a.result(timeout=10) == "committed"
        assert b.result(timeout=10) in {"committed", "serialization_refused"}
    assert write(second_id) == "committed"
    with pg_factory() as session:
        assert _counts(session) == (300, 13, 3, 3)
        assert set(session.scalars(select(storage.PharmonlineIdentityConflict.last_run_id))) == {
            second_id
        }


def test_partial_writer_rechecks_pending_identity_after_waiting_for_isolation(
    pg_factory, monkeypatch
):
    from src import main

    with pg_factory() as session:
        original, source = _seed(session)
        original_identity = [(p.id, p.url, p.external_id, p.name) for p in original[:3]]
        full_id = _run(session).id
        partial_run = _run(session)
        partial_run.catalog_scope = "partial"
        session.commit()
        partial_id = partial_run.id
    entered, release = Event(), Event()
    classify = isolation._classify

    def held_classify(session, results, tenant_id):
        result = classify(session, results, tenant_id)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(isolation, "_classify", held_classify)

    def isolate():
        with pg_factory() as session:
            isolation.prepare_catalog(
                session, source, tenant_id=1, run_id=full_id, source_transport="direct"
            )

    def partial():
        with pg_factory() as session:
            return main.persist_results(
                session, session.get(storage.Run, partial_id), _catalog(*source[0].products[:3])
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(isolate)
        assert entered.wait(10)
        b = pool.submit(partial)
        try:
            deadline = time.monotonic() + 5
            blocked = False
            with pg_factory() as observer:
                while time.monotonic() < deadline:
                    blocked = bool(
                        observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                                "AND classid=721109 AND NOT granted"
                            )
                        )
                    )
                    if blocked:
                        break
                    time.sleep(0.02)
            assert blocked, "partial persistence must wait and then re-read pending identities"
        finally:
            release.set()
        a.result(timeout=10)
        assert b.result(timeout=10) == 0
    with pg_factory() as session:
        rows = list(
            session.scalars(
                select(storage.Product)
                .where(storage.Product.id.in_([p[0] for p in original_identity]))
                .order_by(storage.Product.id)
            )
        )
        assert [(p.id, p.url, p.external_id, p.name) for p in rows] == original_identity
        assert all(p.identity_conflicted for p in rows)
        assert session.scalar(select(func.count()).select_from(storage.PriceSnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(storage.OfferObservation)) == 0
        assert _counts(session) == (300, 13, 3, 3)
