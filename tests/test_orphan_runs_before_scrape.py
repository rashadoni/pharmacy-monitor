"""Осиротевшие прогоны снимает сам сбор — под блокировкой, до своего прогона.

Каждый, кто создаёт Run, держит эксклюзивную блокировку сбора до выхода из
команды. Значит, сбор, получивший блокировку, видит в незавершённых прогонах
только сирот упавших процессов — и не ждёт, пока их снимет watcher: всё это
время сирота не давала посчитать рекомендации.

Доказательство — сама блокировка, поэтому главное здесь идёт на настоящем
PostgreSQL. На SQLite блокировок нет, и там чужой прогон не трогают.
"""

from __future__ import annotations

import itertools
import os
import threading
from datetime import timedelta

import click
import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from src import main as main_mod
from src import roi, roi_refresh, storage
from src._time import utcnow
from src.run_lock import try_shared_scrape_read_lock
from src.storage import RoiActionsCache, RoiRefreshRequest, Run
from tests.test_main_helpers import _FakeLockConnection, _FakeLockFactory
from tests.test_roi_refresh import _cluster, _full_run
from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

_FULL_ALOE_RUN = ["run", "--site", "aloe", "--mode", "category", "--no-alerts", "--force"]
_database_numbers = itertools.count()
_scan_processes = main_mod._other_run_owner_processes


@pytest.fixture(autouse=True)
def no_other_scrape_processes(monkeypatch):
    """Что запущено на машине, где идут тесты, на их исход влиять не должно."""
    monkeypatch.setattr(main_mod, "_other_run_owner_processes", lambda: [])


def _postgres_url() -> str:
    base_url = os.environ.get("DATABASE_URL", "")
    if not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    return base_url


def _postgres_database(base_url: str):
    """Своя база на тест: advisory-блокировка одна на базу, а не на схему."""
    database = f"orphan_reap_{os.getpid()}_{next(_database_numbers)}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        connection.execute(text(f"CREATE DATABASE {database}"))
    engine = create_engine(base_url.rsplit("/", 1)[0] + f"/{database}")
    try:
        storage.Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def pg_sessions():
    yield from _postgres_database(_postgres_url())


@pytest.fixture(params=["sqlite", "postgresql"])
def sessions(request):
    """Проверки, которым блокировка не нужна, — на обеих базах.

    На PostgreSQL «другой процесс» здесь — настоящее второе соединение со своей
    транзакцией; на SQLite с StaticPool соединение одно на всех.
    """
    if request.param == "postgresql":
        yield from _postgres_database(_postgres_url())
        return
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    storage.Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


@pytest.fixture
def reaps(monkeypatch):
    """Что видел каждый вызов снятия: сколько прогонов было в таблице и сколько снято."""
    seen: list[tuple[int, int]] = []
    reap = main_mod.reap_stale_running_runs

    def recording(session, **kwargs):
        rows_before = session.scalar(select(func.count(Run.id)))
        reaped = reap(session, **kwargs)
        seen.append((rows_before, reaped))
        return reaped

    monkeypatch.setattr(main_mod, "reap_stale_running_runs", recording)
    return seen


def _trusted_catalog(session, *, hours_ago: float = 0) -> Run:
    """Подтверждённый каталог всех сайтов и пара, по которой есть что советовать."""
    trusted = _full_run(session, finished_at=utcnow() - timedelta(hours=hours_ago))
    _cluster(session, trusted, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    return trusted


def _unfinished_run(session, *, minutes_ago: float = 20, **fields) -> int:
    run = Run(
        tenant_id=1,
        started_at=utcnow() - timedelta(minutes=minutes_ago),
        finished_at=None,
        status="running",
        **fields,
    )
    session.add(run)
    session.commit()
    return run.id


def _invoke(args: list[str]):
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(main_mod.cli, args)


def _run_count(session) -> int:
    return session.scalar(select(func.count(Run.id)))


def _latest_run(session) -> Run:
    return session.scalar(select(Run).order_by(Run.id.desc()))


def _full_aloe_run(session) -> Run:
    return session.scalar(
        select(Run).where(Run.full_catalog_sites == "aloe").order_by(Run.id.desc())
    )


def _cache_signers(session) -> set[int]:
    return {row.run_id for row in session.scalars(select(RoiActionsCache))}


def _assert_reaped(run: Run) -> None:
    assert run.status == "failed"
    # Порядок истории сохранён: снятие не делает старый прогон свежим.
    assert run.finished_at == run.started_at
    assert "reaped stale" in run.error_message
    assert "start of the next scrape" in run.error_message


def _assert_untouched(run: Run) -> None:
    assert (run.status, run.finished_at, run.error_message) == ("running", None, None)


# ─── Настоящая блокировка: сирот снимаем на старте ───────────────────────────


def test_verified_run_reaps_an_orphan_first_and_computes_recommendations_itself(
    pg_sessions, monkeypatch, reaps
):
    """Сирота упавшего тика больше не откладывает рекомендации на шесть часов."""
    with pg_sessions() as session:
        _trusted_catalog(session)
        orphan_id = _unfinished_run(session)
        runs_before = _run_count(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    # Снята одна строка, и своей в таблице ещё не было: себя сбор не снимает.
    assert reaps == [(runs_before, 1)]
    with pg_sessions() as session:
        _assert_reaped(session.get(Run, orphan_id))
        run = _latest_run(session)
        assert run.id != orphan_id
        assert (run.status, run.error_message) == ("ok", None)
        # Посчитано в конце самого сбора: кэш подписан им, заявка не понадобилась.
        cached = roi.get_cached_actions(session, "pharmonline")
        assert cached is not None
        assert [item["type"] for item in cached] == ["price_raise"]
        assert _cache_signers(session) == {run.id}
        assert session.scalars(select(RoiRefreshRequest)).all() == []
        # Блокировка отпущена вместе с командой.
        with try_shared_scrape_read_lock(session) as free:
            assert free


def test_run_skipped_by_the_cadence_guard_still_reaps_orphans(pg_sessions, monkeypatch):
    """Ночной запуск, которому собирать нечего, сирот всё равно убирает."""
    with pg_sessions() as session:
        _trusted_catalog(session)
        orphan_id = _unfinished_run(session)
        runs_before = _run_count(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        skipped = _invoke(["run", "--site", "aloe"])

    assert skipped.exit_code == 0, skipped.output
    assert "полный сбор пропущен" in skipped.output
    with pg_sessions() as session:
        assert _run_count(session) == runs_before
        _assert_reaped(session.get(Run, orphan_id))


def test_reaped_full_run_of_another_site_hides_recommendations_instead_of_promising_them(
    pg_sessions, monkeypatch
):
    """Сиротой был полный сбор другого сайта: после снятия доверия к нему нет.

    Сбор остаётся `ok`, кэш не пишет и заявку не оставляет — считать не от чего,
    пока тот сайт не соберут заново. Раньше дашборд шесть часов обещал
    «пересчитываются», а потом заявка закрывалась `skipped`.
    """
    with pg_sessions() as session:
        trusted = _trusted_catalog(session, hours_ago=2)
        assert roi_refresh.refresh_from_trusted_epoch(session).outcome == "refreshed"
        orphan_id = _unfinished_run(
            session,
            minutes_ago=60,
            catalog_scope="full",
            full_catalog_sites="aptekonline",
            catalog_verified=False,
        )
        _patch_verified_aloe_pipeline(session, monkeypatch)

        finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    with pg_sessions() as session:
        _assert_reaped(session.get(Run, orphan_id))
        run = _latest_run(session)
        assert (run.status, run.catalog_verified, run.error_message) == ("ok", True, None)
        # Кэш остался от прошлой эпохи и не отдаётся; нового сбор не писал.
        assert _cache_signers(session) == {trusted.id}
        assert roi.get_cached_actions(session, "pharmonline") is None
        assert session.scalars(select(RoiRefreshRequest)).all() == []
        # Пересчёт без сбора называет, какой сайт мешает.
        refusal = roi_refresh.refresh_from_trusted_epoch(session)
        assert refusal.outcome == "untrusted"
        assert f"aptekonline:run={orphan_id},status=failed" in refusal.reason
        # Гвард ритма соберёт этот сайт на ближайшем запуске его таймера.
        due, _ = main_mod._sites_due_for_full_scan(session, ["aptekonline"], tenant_id=1)
        assert due == ["aptekonline"]


def test_orphan_older_than_a_later_verified_scan_of_its_site_does_not_cancel_trust(
    pg_sessions, monkeypatch
):
    """Снятая сирота встаёт в историю по своему началу, а не по моменту снятия."""
    with pg_sessions() as session:
        orphan_id = _unfinished_run(
            session,
            minutes_ago=180,
            catalog_scope="full",
            full_catalog_sites="aptekonline",
            catalog_verified=False,
        )
        # aptekonline после неё уже собран заново и подтверждён.
        _trusted_catalog(session, hours_ago=1)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    with pg_sessions() as session:
        _assert_reaped(session.get(Run, orphan_id))
        run = _latest_run(session)
        assert run.status == "ok"
        assert _cache_signers(session) == {run.id}
        assert roi.get_cached_actions(session, "pharmonline") is not None


def test_scrape_reaps_an_orphan_before_its_own_run(pg_sessions, monkeypatch, reaps):
    """Тик собирает несколько раз в день: дневная сирота не ждёт ночного сбора."""
    monkeypatch.delenv("PHARMONLINE_PUBLIC_API", raising=False)
    monkeypatch.delenv("PHARMONLINE_LEGACY_ID_BRIDGE", raising=False)
    with pg_sessions() as session:
        orphan_id = _unfinished_run(session, minutes_ago=1)
        runs_before = _run_count(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        scraped = _invoke(["scrape", "--site", "aloe"])

    assert scraped.exit_code == 0, scraped.output
    assert reaps == [(runs_before, 1)]
    with pg_sessions() as session:
        _assert_reaped(session.get(Run, orphan_id))
        own = _latest_run(session)
        assert own.id != orphan_id
        assert (own.status, own.catalog_scope, own.error_message) == ("ok", "partial", None)


def test_ai_crawl_reaps_an_orphan_before_its_own_run(pg_sessions, monkeypatch, reaps):
    from src.scrapers import ai_crawler
    from src.scrapers.base import SiteScrapeFatalError

    class _FatalAICrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def crawl(self, *args, **kwargs):
            raise SiteScrapeFatalError("site refused the crawl")

    monkeypatch.setattr(storage, "init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: pg_sessions)
    monkeypatch.setitem(ai_crawler.AI_CRAWLER_BY_SITE, "aloe", _FatalAICrawler)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pg_sessions() as session:
        orphan_id = _unfinished_run(session)
        runs_before = _run_count(session)

    crawled = _invoke(["ai-crawl", "--site", "aloe", "--max-urls", "3"])

    assert crawled.exit_code != 0
    assert reaps == [(runs_before, 1)]
    with pg_sessions() as session:
        _assert_reaped(session.get(Run, orphan_id))
        own = _latest_run(session)
        assert own.id != orphan_id
        assert own.status == "failed"


# ─── Вторая страховка: живой процесс сбора рядом ─────────────────────────────


@pytest.mark.parametrize(
    "processes, reason",
    [
        (
            [(4242, "/opt/pharmacy-monitor/.venv/bin/pharmacy-monitor run --site aptekonline")],
            "another scrape process is alive",
        ),
        (None, "process list unavailable"),
    ],
)
def test_orphan_is_left_alone_while_another_scrape_process_may_be_alive(
    pg_sessions, monkeypatch, reaps, processes, reason
):
    """Блокировка у нас, но рядом жив процесс сбора — возможно, хозяин прогона.

    Так выглядит сбор, у которого оборвалось соединение с блокировкой. Его
    прогон не трогаем: конец нашего сбора переживает это прежним запасным путём.
    """
    monkeypatch.setattr(main_mod, "_other_run_owner_processes", lambda: processes)
    with pg_sessions() as session:
        _trusted_catalog(session)
        foreign_id = _unfinished_run(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        with capture_logs() as events:
            with click.Context(click.Command("run")):
                assert main_mod._hold_scrape_lock_until_command_exit(pg_sessions, wait=False)
                assert main_mod._reap_orphan_runs_before_own_run(pg_sessions, command="run") == 0
        finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    assert reaps == []
    assert [
        (event["run_ids"], event["reason"], event["processes"])
        for event in events
        if event["event"] == "orphan_run_reap_skipped"
    ] == [([foreign_id], reason, processes or [])]
    with pg_sessions() as session:
        _assert_untouched(session.get(Run, foreign_id))
        run = _latest_run(session)
        assert (run.status, run.error_message) == ("ok", None)
        assert _cache_signers(session) == set()
        owed = session.scalars(select(RoiRefreshRequest)).all()
        assert [(item.reason, item.status) for item in owed] == [("full_run_deferred", "pending")]


def test_nothing_to_reap_does_not_look_at_processes(pg_sessions, monkeypatch):
    """Обычный запуск: сирот нет, и ждущий блокировку сосед в журнал не попадает."""
    monkeypatch.setattr(
        main_mod,
        "_other_run_owner_processes",
        lambda: pytest.fail("process list was read without an unfinished run"),
    )
    with pg_sessions() as session:
        _full_run(session)
        session.commit()

    with capture_logs() as events:
        with click.Context(click.Command("run")):
            assert main_mod._hold_scrape_lock_until_command_exit(pg_sessions, wait=False)
            assert main_mod._reap_orphan_runs_before_own_run(pg_sessions, command="run") == 0

    assert events == []


def _drop_the_lock_connection(sessions) -> None:
    """Перезапуск PostgreSQL глазами сбора: соединение с блокировкой оборвано."""
    with sessions() as admin:
        holders = admin.scalars(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            )
        ).all()
        terminated = [
            admin.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})
            for pid in holders
        ]
        admin.commit()
    if terminated != [True]:
        raise AssertionError(f"expected one lock holder to terminate, got {terminated}")


def test_live_run_that_lost_its_lock_is_not_reaped_by_the_next_scrape(pg_sessions, monkeypatch):
    """Блокировка сессионная: оборвалось соединение — сервер её отпустил.

    Процесс сбора этого не замечает: рабочая сессия переподключается сама.
    Следующий сбор блокировку получает, но видит живой процесс и прогон не
    снимает — тот доходит до конца и публикуется, как доходил и раньше.
    """
    seen: dict = {}
    with pg_sessions() as session:
        _trusted_catalog(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)
        scrape = main_mod.scrape_all
        monkeypatch.setattr(
            main_mod,
            "_other_run_owner_processes",
            lambda: [(os.getpid(), "pharmacy-monitor run --site aloe")],
        )

        async def scrape_while_the_lock_connection_dies(*args, **kwargs):
            try:
                _drop_the_lock_connection(pg_sessions)
                with click.Context(click.Command("run")):
                    seen["lock_was_free"] = main_mod._hold_scrape_lock_until_command_exit(
                        pg_sessions, wait=False
                    )
                    seen["reaped"] = main_mod._reap_orphan_runs_before_own_run(
                        pg_sessions, command="run"
                    )
            except BaseException as error:  # сбор проглотил бы ошибку подготовки
                seen["error"] = error
            return await scrape(*args, **kwargs)

        monkeypatch.setattr(main_mod, "scrape_all", scrape_while_the_lock_connection_dies)

        finished = _invoke(_FULL_ALOE_RUN)

    assert seen == {"lock_was_free": True, "reaped": 0}
    assert finished.exit_code == 0, finished.output
    with pg_sessions() as session:
        run = _latest_run(session)
        assert (run.status, run.error_message) == ("ok", None)
        assert _cache_signers(session) == {run.id}


def test_live_run_closed_from_outside_fails_instead_of_publishing(pg_sessions, monkeypatch):
    """Живой прогон всё же закрыли: снимающий не видел его процесса.

    Так бывает со сбором, запущенным не на сервере (ручной workflow ходит в
    базу через туннель): блокировку он потерял, а в списке процессов сервера
    его нет. Узнав по своей строке, что прогон закрыт, сбор останавливается до
    сопоставления — вместо того чтобы дописать себе `ok` поверх чужого `failed`.
    """
    seen: dict = {"matching_stages": 0}
    with pg_sessions() as session:
        trusted = _trusted_catalog(session, hours_ago=2)
        assert roi_refresh.refresh_from_trusted_epoch(session).outcome == "refreshed"
        _patch_verified_aloe_pipeline(session, monkeypatch)
        scrape = main_mod.scrape_all

        async def scrape_and_get_closed(*args, **kwargs):
            try:
                _drop_the_lock_connection(pg_sessions)
                seen["reap"] = (
                    CliRunner()
                    .invoke(main_mod.cli, ["reap-stale-runs", "--max-age-hours", "0"])
                    .output
                )
            except BaseException as error:  # сбор проглотил бы ошибку подготовки
                seen["error"] = error
            return await scrape(*args, **kwargs)

        def matching_stage(*args, **kwargs):
            seen["matching_stages"] += 1
            return {}

        monkeypatch.setattr(main_mod, "scrape_all", scrape_and_get_closed)
        monkeypatch.setattr(main_mod, "_run_matching_stage", matching_stage)

        finished = _invoke(_FULL_ALOE_RUN)

    assert "error" not in seen, seen
    assert "reaped 1 stale running run(s)" in seen["reap"]
    assert finished.exit_code != 0
    assert "closed from outside as an orphan" in finished.output
    assert "noticed before matching" in finished.output
    # Пары закрытый сбор уже не трогал.
    assert seen["matching_stages"] == 0
    with pg_sessions() as session:
        run = _latest_run(session)
        assert run.status == "failed"
        assert run.error_message.startswith("RunClosedAsOrphan:")
        assert run.finished_at > run.started_at
        # Рекомендаций он не публиковал: кэш прежний и больше не отдаётся.
        assert _cache_signers(session) == {trusted.id}
        assert roi.get_cached_actions(session, "pharmonline") is None
        assert session.scalars(select(RoiRefreshRequest)).all() == []


def _reap_command(db_session, monkeypatch, *options: str):
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: factory)
    return CliRunner().invoke(main_mod.cli, ["reap-stale-runs", *options])


@pytest.mark.parametrize(
    "processes, refusal",
    [
        (
            [(4242, "/opt/pharmacy-monitor/.venv/bin/python3 -m src.main run --site pharmonline")],
            "recovery refused: another scrape process is alive\n"
            "  pid 4242: /opt/pharmacy-monitor/.venv/bin/python3 -m src.main run --site pharmonline",
        ),
        (None, "recovery refused: process list unavailable"),
    ],
)
def test_reap_command_refuses_while_another_scrape_process_may_be_alive(
    db_session, monkeypatch, processes, refusal
):
    """Команда доказывает сама: её зовёт и watcher, и человек, и оба без выдержки."""
    foreign_id = _unfinished_run(db_session, minutes_ago=600)
    monkeypatch.setattr(main_mod, "_other_run_owner_processes", lambda: processes)

    result = _reap_command(db_session, monkeypatch)

    assert result.exit_code != 0
    assert refusal in result.output
    db_session.expire_all()
    _assert_untouched(db_session.get(Run, foreign_id))


def test_reap_command_reaps_when_no_scrape_process_is_left(db_session, monkeypatch):
    """Без флага возраста: он не доказательство, и по умолчанию выдержки нет."""
    orphan_id = _unfinished_run(db_session, minutes_ago=1)

    result = _reap_command(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "reaped 1 stale running run(s)" in result.output
    db_session.expire_all()
    orphan = db_session.get(Run, orphan_id)
    assert (orphan.status, orphan.finished_at) == ("failed", orphan.started_at)


def test_reap_command_keeps_the_age_threshold_when_asked(db_session, monkeypatch):
    """Аварийный рычаг watcher'а: `ORPHAN_RUN_MAX_AGE_HOURS` уходит сюда флагом."""
    young_id = _unfinished_run(db_session, minutes_ago=60)

    result = _reap_command(db_session, monkeypatch, "--max-age-hours", "6")

    assert result.exit_code == 0, result.output
    assert "reaped 0 stale running run(s)" in result.output
    db_session.expire_all()
    _assert_untouched(db_session.get(Run, young_id))


def test_reap_command_with_nothing_to_reap_does_not_refuse(db_session, monkeypatch):
    """Watcher зовёт её каждую минуту; процесс рядом — не повод для ошибки в журнале."""
    _full_run(db_session)
    db_session.commit()
    monkeypatch.setattr(
        main_mod,
        "_other_run_owner_processes",
        lambda: pytest.fail("process list was read without an unfinished run"),
    )

    result = _reap_command(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "reaped 0 stale running run(s)" in result.output


# ─── Без блокировки доказательства нет ───────────────────────────────────────


def test_without_a_real_lock_a_foreign_unfinished_run_is_left_alone(db_session, monkeypatch):
    """SQLite: блокировок нет, и «чужой незавершённый» может быть живым сбором."""
    foreign_id = _unfinished_run(db_session, minutes_ago=600)
    _patch_verified_aloe_pipeline(db_session, monkeypatch)

    finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    db_session.expire_all()
    _assert_untouched(db_session.get(Run, foreign_id))


def test_reaping_needs_the_lock_of_the_current_command(db_session):
    foreign_id = _unfinished_run(db_session)
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)

    # Вне команды и в команде без блокировки — не трогаем.
    assert main_mod._reap_orphan_runs_before_own_run(factory, command="run") == 0
    with click.Context(click.Command("run")):
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        assert main_mod._exclusive_scrape_lock_is_held() is False
        assert main_mod._reap_orphan_runs_before_own_run(factory, command="run") == 0

    db_session.expire_all()
    _assert_untouched(db_session.get(Run, foreign_id))


def test_reaping_under_the_lock_takes_orphans_of_any_age_and_only_them(db_session):
    """Под блокировкой возраст ничего не доказывает: снимаем и минутную сироту."""
    finished = _full_run(db_session)
    db_session.commit()
    fresh_id = _unfinished_run(db_session, minutes_ago=0.01)
    old_id = _unfinished_run(
        db_session, minutes_ago=600, catalog_scope="full", full_catalog_sites="aloe"
    )
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)

    with capture_logs() as events:
        with click.Context(click.Command("scrape")):
            assert main_mod._hold_scrape_lock_until_command_exit(
                _FakeLockFactory(_FakeLockConnection(acquired=True)), wait=False
            )
            assert main_mod._reap_orphan_runs_before_own_run(factory, command="scrape") == 2
            # Повтор ничего не находит.
            assert main_mod._reap_orphan_runs_before_own_run(factory, command="scrape") == 0

    db_session.expire_all()
    _assert_reaped(db_session.get(Run, fresh_id))
    _assert_reaped(db_session.get(Run, old_id))
    kept = db_session.get(Run, finished.id)
    assert (kept.status, kept.error_message) == ("ok", None)
    assert kept.run_quality["financially_eligible"] is True
    # По этим событиям RUNBOOK велит искать снятые прогоны в журнале сбора.
    assert {event["event"] for event in events} == {
        "stale_run_reaped",
        "orphan_runs_reaped_before_run",
    }
    assert [
        (event["run_id"], event["catalog_scope"], event["full_catalog_sites"])
        for event in events
        if event["event"] == "stale_run_reaped"
    ] == [(old_id, "full", "aloe"), (fresh_id, "unknown", None)]
    assert [
        (event["command"], event["count"])
        for event in events
        if event["event"] == "orphan_runs_reaped_before_run"
    ] == [("scrape", 2)]


def test_failed_reaping_does_not_stop_the_scrape():
    def broken_factory():
        raise RuntimeError("database is unreachable")

    with capture_logs() as events:
        with click.Context(click.Command("run")):
            assert main_mod._hold_scrape_lock_until_command_exit(
                _FakeLockFactory(_FakeLockConnection(acquired=True)), wait=False
            )
            assert main_mod._reap_orphan_runs_before_own_run(broken_factory, command="run") == 0

    assert [(event["event"], event["command"]) for event in events] == [
        ("orphan_run_reap_failed", "run")
    ]


# ─── Метка «блокировка у нас» ────────────────────────────────────────────────


def test_lock_mark_lives_exactly_as_long_as_the_lock():
    connection = _FakeLockConnection(acquired=True)
    context = click.Context(click.Command("run"))
    assert main_mod._exclusive_scrape_lock_is_held() is False
    with context:
        assert main_mod._exclusive_scrape_lock_is_held() is False
        assert main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(connection), wait=False
        )
        assert main_mod._exclusive_scrape_lock_is_held() is True
    assert connection.closed is True
    assert context not in main_mod._CONTEXTS_HOLDING_SCRAPE_LOCK
    # Тот же контекст, открытый заново, блокировку уже не держит.
    with context:
        assert main_mod._exclusive_scrape_lock_is_held() is False


def test_busy_lock_leaves_no_mark():
    with click.Context(click.Command("scrape")):
        assert not main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(_FakeLockConnection(acquired=False)), wait=False
        )
        assert main_mod._exclusive_scrape_lock_is_held() is False


def test_invoked_command_holds_its_own_lock_and_returns_it_with_its_context():
    """`watchlist-tick` зовёт `run`, а `intraday-tick` — `scrape` через `ctx.invoke`."""
    seen: dict = {}

    @click.command()
    def inner():
        main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(_FakeLockConnection(acquired=True)), wait=True
        )
        seen["inside"] = main_mod._exclusive_scrape_lock_is_held()

    @click.command()
    @click.pass_context
    def outer(ctx):
        seen["before"] = main_mod._exclusive_scrape_lock_is_held()
        ctx.invoke(inner)
        seen["after"] = main_mod._exclusive_scrape_lock_is_held()

    assert CliRunner().invoke(outer).exit_code == 0
    assert seen == {"before": False, "inside": True, "after": False}


def test_invoked_command_does_not_inherit_the_lock_of_its_caller(db_session):
    """Иначе вызванная команда сняла бы прогон, который вызвавшая уже создала."""
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)
    seen: dict = {}

    @click.command()
    def inner():
        seen["inner_holds"] = main_mod._exclusive_scrape_lock_is_held()
        seen["reaped"] = main_mod._reap_orphan_runs_before_own_run(factory, command="scrape")

    @click.command()
    @click.pass_context
    def outer(ctx):
        main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(_FakeLockConnection(acquired=True)), wait=True
        )
        seen["own_run"] = _unfinished_run(db_session, minutes_ago=0)
        ctx.invoke(inner)
        seen["outer_holds"] = main_mod._exclusive_scrape_lock_is_held()

    result = CliRunner().invoke(outer)

    assert result.exit_code == 0, result.output
    assert (seen["inner_holds"], seen["reaped"], seen["outer_holds"]) == (False, 0, True)
    db_session.expire_all()
    _assert_untouched(db_session.get(Run, seen["own_run"]))


# ─── Список процессов ────────────────────────────────────────────────────────


def _fake_proc(tmp_path, processes: dict[int, list[str | bytes] | None]):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()
    (root / "meminfo").write_text("MemTotal: 1 kB\n")
    for pid, arguments in processes.items():
        (root / str(pid)).mkdir()
        if arguments is not None:
            (root / str(pid) / "cmdline").write_bytes(
                b"".join(
                    (argument if isinstance(argument, bytes) else argument.encode()) + b"\0"
                    for argument in arguments
                )
            )
    return root


def test_process_scan_recognises_every_way_a_scrape_is_started(tmp_path):
    venv = "/opt/pharmacy-monitor/.venv/bin"
    cli = [f"{venv}/python3", f"{venv}/pharmacy-monitor"]
    root = _fake_proc(
        tmp_path,
        {
            # Юнит systemd и watcher: консольный скрипт; ядро показывает его
            # как интерпретатор из shebang и путь скрипта.
            101: [*cli, "run", "--site", "aloe"],
            102: [*cli, "intraday-tick"],
            103: [*cli, "watchlist-tick"],
            104: [*cli, "scrape", "--limit", "5"],
            105: [*cli, "ai-crawl", "--site", "aloe"],
            # Workflow pharmonline: модуль, а не скрипт.
            106: [f"{venv}/python", "-m", "src.main", "run", "--mode", "public_api"],
            107: ["python3.12", "-u", "/srv/checkout/src/main.py", "scrape"],
            108: ["python", "-msrc.main", "run"],
            111: ["python3", "-um", "src.main", "run"],
            112: ["python3", "-Bumsrc.main", "scrape"],
            # Параметр группы стоит перед командой.
            109: [*cli, "--log-level", "DEBUG", "run"],
            # Командная строка с байтами не из UTF-8.
            110: [*cli, "run", b"--note=\xff\xfe"],
            # Не владеют прогоном.
            201: [*cli, "roi", "refresh", "--pending"],
            202: [*cli, "reap-stale-runs", "--max-age-hours", "0"],
            203: [*cli, "rematch"],
            204: [*cli, "report", "--run-id", "7"],
            205: [f"{venv}/uvicorn", "src.api:app", "--workers", "2"],
            206: ["/bin/bash", "/opt/pharmacy-monitor/infra/server/watch-scrape-queue.sh"],
            207: ["/usr/bin/python3", "-m", "pytest", "tests/test_run_quality.py"],
            # Слово команды стоит до точки входа, а не после неё.
            208: [f"{venv}/python3", "-W", "run", f"{venv}/pharmacy-monitor", "roi", "refresh"],
            209: ["python3", "tool.py", "src.main", "run"],
            216: ["python3", "--form", "src.main", "run"],
            217: ["/usr/bin/python3", "-m", "pytest", "-k", "run"],
            218: ["python3", "-m", "run", "src.main"],
            # Файл с именем src.main, а не модуль.
            219: ["python3", "-u", "src.main", "run"],
            # Обёртки несут ту же строку, но сбор — их потомок, а не они сами:
            # иначе сбор, запущенный через обёртку, видел бы «соседа» в родителе.
            210: ["/usr/bin/systemd-run", "--wait", "--pipe", f"{venv}/pharmacy-monitor", "run"],
            211: ["sudo", "-u", "pm", f"{venv}/pharmacy-monitor", "run", "--force"],
            212: ["timeout", "600", f"{venv}/pharmacy-monitor", "scrape"],
            213: ["uv", "run", "pharmacy-monitor", "run", "--mode", "auto", "--force"],
            214: ["find", "/opt/pharmacy-monitor", "-name", "run"],
            215: ["/bin/bash", "-c", f"cd /opt/pharmacy-monitor && {venv}/pharmacy-monitor run"],
            # Зомби и процесс, завершившийся посреди чтения.
            301: [],
            302: None,
            # Сам себя сбор за соседа не считает.
            os.getpid(): [*cli, "run"],
        },
    )
    # Запись процесса есть, а командную строку прочитать нельзя.
    (root / "303").mkdir()
    (root / "303" / "cmdline").mkdir()

    found = _scan_processes(root)

    assert sorted(pid for pid, _ in found) == [
        *range(101, 113),
    ]
    assert dict(found)[106] == f"{venv}/python -m src.main run --mode public_api"


def test_process_scan_reports_that_the_list_is_unavailable(tmp_path):
    assert _scan_processes(tmp_path / "no-such-proc") is None
    assert _scan_processes(_fake_proc(tmp_path, {})) == []


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs a Linux process table")
def test_process_scan_reads_the_real_process_table():
    """На Linux список есть, и запущенный pytest в него не попадает."""
    found = _scan_processes()

    assert found is not None
    assert os.getpid() not in {pid for pid, _ in found}


# ─── Закрытый извне прогон не публикуется ────────────────────────────────────


def _close_from_another_connection(sessions) -> int:
    with sessions() as other:
        return main_mod.reap_stale_running_runs(other, max_age_hours=0)


def test_run_closed_during_its_scrape_stops_before_matching(sessions, monkeypatch):
    seen: dict = {"matching_stages": 0}
    with sessions() as session:
        request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
        session.add(request)
        session.commit()
        trusted = _trusted_catalog(session)
        assert roi_refresh.refresh_from_trusted_epoch(session).outcome == "refreshed"
        _patch_verified_aloe_pipeline(session, monkeypatch)
        scrape = main_mod.scrape_all

        async def scrape_and_get_closed(*args, **kwargs):
            seen["closed"] = _close_from_another_connection(sessions)
            return await scrape(*args, **kwargs)

        def matching_stage(*args, **kwargs):
            seen["matching_stages"] += 1
            return {}

        monkeypatch.setattr(main_mod, "scrape_all", scrape_and_get_closed)
        monkeypatch.setattr(main_mod, "_run_matching_stage", matching_stage)

        finished = _invoke(_FULL_ALOE_RUN[:-1] + ["--request-id", str(request.id)])

    assert seen == {"closed": 1, "matching_stages": 0}
    assert finished.exit_code != 0
    assert "noticed before matching" in finished.output
    with sessions() as session:
        run = _latest_run(session)
        assert run.status == "failed"
        assert run.error_message.startswith("RunClosedAsOrphan:")
        assert run.finished_at > run.started_at
        assert session.get(storage.ScrapeRequest, request.id).status == "failed"
        assert _cache_signers(session) == {trusted.id}
        assert session.scalars(select(RoiRefreshRequest)).all() == []


def test_run_closed_during_matching_confirms_no_prices_and_sends_no_alerts(sessions, monkeypatch):
    """Письмо не отзовёшь: закрытый прогон до алертов не доходит."""
    from src import alerts

    seen: dict = {"alerts": 0, "confirmations": 0}
    with sessions() as session:
        _trusted_catalog(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        def matching_stage_and_get_closed(*args, **kwargs):
            seen["closed"] = _close_from_another_connection(sessions)
            return {}

        def evaluate_rules(*args, **kwargs):
            seen["alerts"] += 1
            return []

        def confirm_prices(*args, **kwargs):
            seen["confirmations"] += 1
            return 0

        monkeypatch.setattr(main_mod, "_run_matching_stage", matching_stage_and_get_closed)
        monkeypatch.setattr(alerts, "evaluate_rules", evaluate_rules)
        monkeypatch.setattr(storage, "confirm_prices_of_latest_verified_runs", confirm_prices)

        finished = _invoke([arg for arg in _FULL_ALOE_RUN if arg != "--no-alerts"])

    assert seen == {"closed": 1, "alerts": 0, "confirmations": 0}
    assert finished.exit_code != 0
    assert "noticed before alerts" in finished.output
    with sessions() as session:
        run = _latest_run(session)
        assert run.status == "failed"
        assert _cache_signers(session) == set()
        # Упавший полный сбор заявку не ставит: доверие к сайту он отменил сам.
        assert session.scalars(select(RoiRefreshRequest)).all() == []


def test_run_closed_during_finalization_does_not_write_ok_over_failed(sessions, monkeypatch):
    """Самое дорогое место: `ok` поверх чужого `failed` закрыл бы окно ритма.

    Гвард недельного ритма счёл бы сайт собранным, а денежное доверие с прогона
    уже снято — рекомендаций не было бы до следующей недели.
    """
    seen: dict = {}
    with sessions() as session:
        _trusted_catalog(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)
        analyze = main_mod.analyzer.analyze

        def analyze_and_get_closed(*args, **kwargs):
            seen["closed"] = _close_from_another_connection(sessions)
            return analyze(*args, **kwargs)

        monkeypatch.setattr(main_mod.analyzer, "analyze", analyze_and_get_closed)

        finished = _invoke(_FULL_ALOE_RUN)

    assert seen == {"closed": 1}
    assert finished.exit_code != 0
    assert "noticed before publication" in finished.output
    with sessions() as session:
        run = _latest_run(session)
        assert run.status == "failed"
        assert run.error_message.startswith("RunClosedAsOrphan:")
        assert storage.run_is_financially_eligible(run) is False
        # Упавший сбор окно ритма не закрывает: следующий запуск соберёт сайт заново.
        due, covered = main_mod._sites_due_for_full_scan(session, ["aloe"], tenant_id=1)
        assert (due, covered) == (["aloe"], {})


def test_run_closed_at_the_last_moment_is_refused_by_the_write_itself(sessions, monkeypatch):
    """Между последней проверкой и записью `ok` зазора нет: отказывает сама запись."""
    seen: dict = {}
    with sessions() as session:
        _trusted_catalog(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)
        claim = main_mod._claim_run_for_publication

        def get_closed_and_claim(*args, **kwargs):
            seen["closed"] = _close_from_another_connection(sessions)
            return claim(*args, **kwargs)

        monkeypatch.setattr(main_mod, "_claim_run_for_publication", get_closed_and_claim)

        finished = _invoke(_FULL_ALOE_RUN)

    assert seen == {"closed": 1}
    assert finished.exit_code != 0
    assert "noticed before publication" in finished.output
    with sessions() as session:
        run = _latest_run(session)
        assert run.status == "failed"
        assert storage.run_is_financially_eligible(run) is False
        due, _ = main_mod._sites_due_for_full_scan(session, ["aloe"], tenant_id=1)
        assert due == ["aloe"]


def test_untouched_run_passes_every_check(sessions, monkeypatch):
    with sessions() as session:
        _trusted_catalog(session)
        _patch_verified_aloe_pipeline(session, monkeypatch)

        finished = _invoke(_FULL_ALOE_RUN)

    assert finished.exit_code == 0, finished.output
    with sessions() as session:
        run = _latest_run(session)
        assert (run.status, run.error_message) == ("ok", None)
        assert run.finished_at > run.started_at
        assert roi.get_cached_actions(session, "pharmonline") is not None


def test_claim_sets_finished_at_once_and_only_on_an_open_run(sessions):
    with sessions() as session:
        mine_id = _unfinished_run(session)
        other_id = _unfinished_run(session)
        mine = session.get(Run, mine_id)

        claimed_at = main_mod._claim_run_for_publication(session, mine)
        session.commit()

        assert claimed_at is not None
        assert session.scalar(select(Run.finished_at).where(Run.id == mine_id)) == claimed_at
        # Чужую открытую строку не трогает.
        assert session.scalar(select(Run.finished_at).where(Run.id == other_id)) is None
        # Закрытый прогон второй раз себе не присвоить.
        with pytest.raises(main_mod.RunClosedAsOrphan, match="noticed before publication"):
            main_mod._claim_run_for_publication(session, mine)


# ─── Снятие и публикация не могут выиграть обе ───────────────────────────────


def _in_thread(target):
    outcome: dict = {}

    def work():
        try:
            outcome["result"] = target()
        except BaseException as error:  # результат потока читает основной
            outcome["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, outcome


def test_publication_waits_for_a_reaper_and_then_yields_to_it(pg_sessions):
    """Снятие началось первым: публикация ждёт его и видит строку закрытой.

    «Проверил, потом записал» здесь прошло бы проверку — снятие ещё не
    зафиксировано — и затем записало бы `ok` поверх `failed`.
    """
    with pg_sessions() as setup:
        run_id = _unfinished_run(setup)

    with pg_sessions() as reaper, pg_sessions() as publisher:
        reaper.execute(text("SELECT id FROM runs WHERE finished_at IS NULL FOR UPDATE"))
        run = publisher.get(Run, run_id)
        thread, outcome = _in_thread(lambda: main_mod._claim_run_for_publication(publisher, run))
        thread.join(timeout=1.5)
        assert thread.is_alive(), outcome
        reaper.execute(
            text("UPDATE runs SET status = 'failed', finished_at = started_at WHERE id = :id"),
            {"id": run_id},
        )
        reaper.commit()
        thread.join(timeout=10)

        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), main_mod.RunClosedAsOrphan), outcome
        publisher.rollback()

    with pg_sessions() as verify:
        closed = verify.get(Run, run_id)
        assert (closed.status, closed.finished_at) == ("failed", closed.started_at)


def test_reaper_skips_a_run_that_is_being_published(pg_sessions):
    """Публикация началась первой: строка занята — значит, прогон жив.

    Снятие её не ждёт и не трогает. Без замка на строках оно увидело бы
    незавершённый прогон и записало `failed` поверх только что поставленного `ok`.
    """
    with pg_sessions() as setup:
        run_id = _unfinished_run(setup)

    with pg_sessions() as publisher, pg_sessions() as reaper:
        run = publisher.get(Run, run_id)
        run.finished_at = main_mod._claim_run_for_publication(publisher, run)
        run.status = "ok"
        thread, outcome = _in_thread(
            lambda: main_mod.reap_stale_running_runs(reaper, max_age_hours=0)
        )
        thread.join(timeout=5)
        still_waiting = thread.is_alive()
        publisher.commit()
        thread.join(timeout=10)

        assert not still_waiting, "reaper waited for the publishing transaction"
        assert outcome == {"result": 0}

    with pg_sessions() as verify:
        published = verify.get(Run, run_id)
        assert published.status == "ok"
        assert published.finished_at > published.started_at
        assert published.error_message is None


def test_reaper_skips_a_run_whose_snapshots_are_being_written(pg_sessions):
    """Открытая транзакция пишет снимки прогона — его процесс жив.

    Внешний ключ снимка держит строку прогона. Ждать её снятие не должно: оно
    идёт под блокировкой сбора, и повисшее снятие не дало бы стартовать сборам.
    """
    with pg_sessions() as setup:
        run_id = _unfinished_run(setup)
        product = storage.Product(
            tenant_id=1,
            site="aloe",
            external_id="sku",
            url="https://aloe.example/sku",
            name="SKU",
            name_normalized="sku",
        )
        setup.add(product)
        setup.commit()
        product_id = product.id

    with pg_sessions() as writer, pg_sessions() as reaper:
        writer.add(storage.PriceSnapshot(run_id=run_id, product_id=product_id, price=1.0))
        writer.flush()
        thread, outcome = _in_thread(
            lambda: main_mod.reap_stale_running_runs(reaper, max_age_hours=0)
        )
        thread.join(timeout=5)
        still_waiting = thread.is_alive()
        writer.rollback()
        thread.join(timeout=10)

        assert not still_waiting, "reaper waited for the transaction writing snapshots"
        assert outcome == {"result": 0}
        # Пропуск виден в журнале: пока транзакция жива, прогон мешает расчёту.
        with pg_sessions() as other, capture_logs() as events:
            writer.add(storage.PriceSnapshot(run_id=run_id, product_id=product_id, price=2.0))
            writer.flush()
            assert main_mod.reap_stale_running_runs(other, max_age_hours=0) == 0
            writer.rollback()
        assert [(event["event"], event["run_ids"]) for event in events] == [
            ("stale_run_reap_skipped_busy_row", [run_id])
        ]
        # Транзакции больше нет: тот же прогон — уже сирота.
        assert main_mod.reap_stale_running_runs(reaper, max_age_hours=0) == 1
