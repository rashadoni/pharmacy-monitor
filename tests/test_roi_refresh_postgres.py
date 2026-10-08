"""Пересчёт рекомендаций и блокировка сборов — на настоящем PostgreSQL.

На SQLite advisory-блокировок нет, и всё, что здесь проверяется, там выглядит
«всегда свободно». Требование одно: пересчёт не читает каталог посреди записи
прогона, а сбор не начинает писать посреди пересчёта.
"""

from __future__ import annotations

import os

import click
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import roi, roi_refresh, storage
from src.run_lock import try_exclusive_roi_refresh_lock, try_shared_scrape_read_lock


def _postgres_session_factory():
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    return storage.make_session(database_url)


def _scrape_can_start(session_factory) -> bool:
    """Удалось бы сбору прямо сейчас взять свою блокировку (как `scrape`)."""
    with click.Context(click.Command("scrape")):
        return main_mod._hold_scrape_lock_until_command_exit(session_factory, wait=False)


def test_refresh_does_not_read_the_catalog_while_a_scrape_is_writing(monkeypatch):
    session_factory = _postgres_session_factory()
    monkeypatch.setattr(
        roi_refresh,
        "_refresh_locked",
        lambda *args, **kwargs: pytest.fail("catalog was read during a scrape"),
    )

    with click.Context(click.Command("run")):
        assert main_mod._hold_scrape_lock_until_command_exit(session_factory, wait=False)
        with session_factory() as session:
            result = roi_refresh.refresh_from_trusted_epoch(session)

    assert (result.outcome, result.reason) == ("busy", "scrape_in_progress")


def test_scrape_cannot_start_writing_until_the_refresh_is_done(monkeypatch):
    session_factory = _postgres_session_factory()
    seen: dict[str, bool] = {}

    def compute(session, *, tenant_id):
        seen["scrape_started_mid_refresh"] = _scrape_can_start(session_factory)
        return roi_refresh.RefreshResult("refreshed")

    monkeypatch.setattr(roi_refresh, "_refresh_locked", compute)

    with session_factory() as session:
        assert roi_refresh.refresh_from_trusted_epoch(session).outcome == "refreshed"

    assert seen == {"scrape_started_mid_refresh": False}
    # Обе блокировки отпущены вместе с пересчётом.
    assert _scrape_can_start(session_factory)
    with session_factory() as session:
        with try_exclusive_roi_refresh_lock(session) as free:
            assert free


def test_refreshes_exclude_each_other_but_do_not_block_scrapes(monkeypatch):
    session_factory = _postgres_session_factory()
    monkeypatch.setattr(
        roi_refresh,
        "_refresh_locked",
        lambda *args, **kwargs: pytest.fail("second refresh must not compute"),
    )

    with session_factory() as first, session_factory() as second:
        with try_exclusive_roi_refresh_lock(first) as mine:
            assert mine
            result = roi_refresh.refresh_from_trusted_epoch(second)
            # Ключ у пересчёта свой: сам по себе он сборам не мешает.
            assert _scrape_can_start(session_factory)

    assert (result.outcome, result.reason) == ("busy", "another_refresh_running")


def test_rematch_queues_its_request_before_refreshes_can_read_pairs(monkeypatch):
    """rematch пересобирает пары минутами; посчитанное в это время было бы мусором.

    Он держит эксклюзивную блокировку сбора до выхода из команды, а заявку на
    пересчёт ставит раньше — в `finally`. Значит, пересчёт начнётся только после
    rematch и заявку увидит.
    """
    from click.testing import CliRunner

    session_factory = _postgres_session_factory()
    seen: dict = {}
    monkeypatch.setattr(main_mod.storage, "make_session", lambda *args, **kwargs: session_factory)
    monkeypatch.setattr(
        main_mod,
        "_run_matching_stage",
        lambda session, fuzzy_threshold=None: {
            "refreshed": 0,
            "relinked": 0,
            "clusters": 0,
            "revalidated": 0,
            "flagged": 0,
        },
    )
    monkeypatch.setattr(
        roi_refresh,
        "_refresh_locked",
        lambda *args, **kwargs: pytest.fail("pairs were read while rematch was rebuilding them"),
    )

    def at_the_moment_the_request_is_queued(session, *, reason, tenant_id=1):
        with session_factory() as reader:
            seen["refresh"] = roi_refresh.refresh_from_trusted_epoch(reader)

    monkeypatch.setattr(main_mod, "_request_roi_refresh", at_the_moment_the_request_is_queued)

    result = CliRunner().invoke(main_mod.cli, ["rematch"])

    assert result.exit_code == 0, result.output
    assert (seen["refresh"].outcome, seen["refresh"].reason) == ("busy", "scrape_in_progress")
    with session_factory() as reader:
        with try_shared_scrape_read_lock(reader) as free_after_exit:
            assert free_after_exit


def test_database_error_mid_refresh_is_survived_on_postgres(monkeypatch):
    """Ошибка SQL посреди пересчёта прерывает транзакцию PostgreSQL целиком.

    На SQLite следующий запрос после ошибки просто проходит, поэтому там этот
    путь выглядит исправным при любом коде. Здесь `refresh_all_cached_actions`
    ловит ошибку среза и тут же падает сам — на чистке кэша в прерванной
    транзакции. Пересчёт обязан откатиться, убрать кэш, закрыть заявку и
    оставить один повтор, а не падать на каждом тике watcher'а.
    """
    from tests.test_roi_refresh import _cluster, _full_run

    base_url = os.environ.get("DATABASE_URL", "")
    if not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    database = f"roi_refresh_crash_{os.getpid()}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        connection.execute(text(f"CREATE DATABASE {database}"))
    engine = create_engine(base_url.rsplit("/", 1)[0] + f"/{database}")
    try:
        storage.Base.metadata.create_all(engine)
        Session = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with Session() as session:
            run = _full_run(session)
            _cluster(session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
            assert roi_refresh.refresh_from_trusted_epoch(session).outcome == "refreshed"
            roi_refresh.request_refresh(session, tenant_id=1, reason="match_reject")
            session.commit()
            real_compute = roi._compute_actions_locked

            def compute(current, *, client_site=None, **kwargs):
                if client_site == "aptekonline":
                    current.execute(text("SELECT * FROM table_that_does_not_exist"))
                return real_compute(current, client_site=client_site, **kwargs)

            monkeypatch.setattr(roi, "_compute_actions_locked", compute)

            result = roi_refresh.run_refresh(session, only_if_requested=True)

            assert result.outcome == "failed"
            assert result.reason.startswith("error:")
            assert session.scalars(select(storage.RoiActionsCache)).all() == []
            requests = session.scalars(
                select(storage.RoiRefreshRequest).order_by(storage.RoiRefreshRequest.id)
            ).all()
            assert [(request.reason, request.status) for request in requests] == [
                ("match_reject", "failed"),
                ("retry_after_failure", "pending"),
            ]

            # Сбой прошёл — повтор возвращает рекомендации без участия человека.
            monkeypatch.setattr(roi, "_compute_actions_locked", real_compute)
            assert roi_refresh.run_refresh(session, only_if_requested=True).outcome == "refreshed"
            assert roi.get_cached_actions(session, "pharmonline") is not None
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        admin.dispose()


def test_unfinished_run_at_the_end_of_a_verified_run_is_survived_on_postgres(monkeypatch):
    """Чужой незавершённый прогон в конце подтверждённого сбора — настоящими командами.

    Сирот сбор снимает на старте (`tests/test_orphan_runs_before_scrape.py`),
    так что к концу незавершённый прогон остаётся, только если его не тронули
    (рядом жил другой процесс сбора) или он появился уже при сборе. Это
    запасной путь.

    На SQLite блокировок нет. Здесь `run` держит эксклюзивную блокировку сбора
    до выхода из команды, а `roi refresh` и `reap-stale-runs` берут свои: весь
    путь «сбор отложил расчёт → watcher снял сироту → тик посчитал» обязан
    пройти без того, чтобы команды заперли друг друга.
    """
    from click.testing import CliRunner

    from src._time import utcnow
    from tests.test_roi_refresh import _cluster, _full_run
    from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

    base_url = os.environ.get("DATABASE_URL", "")
    if not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    database = f"roi_run_end_orphan_{os.getpid()}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        connection.execute(text(f"CREATE DATABASE {database}"))
    engine = create_engine(base_url.rsplit("/", 1)[0] + f"/{database}")
    try:
        storage.Base.metadata.create_all(engine)
        Session = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        runner = CliRunner()
        with Session() as session:
            trusted = _full_run(session)
            _cluster(
                session, trusted, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5}
            )
            _patch_verified_aloe_pipeline(session, monkeypatch)
            scrape = main_mod.scrape_all

            async def scrape_next_to_another_producer(*args, **kwargs):
                with Session() as other:
                    other.add(
                        storage.Run(
                            tenant_id=1, started_at=utcnow(), finished_at=None, status="running"
                        )
                    )
                    other.commit()
                return await scrape(*args, **kwargs)

            monkeypatch.setattr(main_mod, "scrape_all", scrape_next_to_another_producer)
            # Что запущено на машине, где идут тесты, на исход влиять не должно.
            monkeypatch.setattr(main_mod, "_other_run_owner_processes", lambda: [])

            with runner.isolated_filesystem():
                # --force: у aloe в этом окне ритма уже есть полный сбор (фикстура).
                finished = runner.invoke(
                    main_mod.cli,
                    ["run", "--site", "aloe", "--mode", "category", "--no-alerts", "--force"],
                )
        assert finished.exit_code == 0, finished.output

        with Session() as session:
            # Последний по номеру — прогон соседа: он появился уже при сборе.
            run = session.scalar(
                select(storage.Run)
                .where(storage.Run.catalog_scope == "full")
                .order_by(storage.Run.id.desc())
            )
            assert (run.status, run.error_message) == ("ok", None)
            assert session.scalars(select(storage.RoiActionsCache)).all() == []
            owed = session.scalars(select(storage.RoiRefreshRequest)).all()
            assert [(item.reason, item.status) for item in owed] == [
                ("full_run_deferred", "pending")
            ]

        waiting = runner.invoke(main_mod.cli, ["roi", "refresh", "--pending"])
        assert waiting.exit_code == 0, waiting.output
        assert "отложено — есть незавершённый прогон" in waiting.output

        reaped = runner.invoke(main_mod.cli, ["reap-stale-runs", "--max-age-hours", "0"])
        assert reaped.exit_code == 0, reaped.output
        assert "reaped 1 stale running run(s)" in reaped.output

        computed = runner.invoke(main_mod.cli, ["roi", "refresh", "--pending"])
        assert computed.exit_code == 0, computed.output
        assert f"пересчитано по прогону #{run.id}" in computed.output

        with Session() as session:
            cached = roi.get_cached_actions(session, "pharmonline")
            assert cached is not None
            assert [item["type"] for item in cached] == ["price_raise"]
            owed = session.scalars(select(storage.RoiRefreshRequest)).all()
            assert [(item.reason, item.status) for item in owed] == [("full_run_deferred", "done")]
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        admin.dispose()
