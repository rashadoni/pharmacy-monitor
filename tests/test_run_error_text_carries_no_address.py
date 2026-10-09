"""Текст ошибки прогона ложится в базу без адреса.

`runs.error_message` читают не только операторы. Страницу прогонов видит роль
viewer — сотрудник клиента; текст стоит в строке `last_run_failed`, а она идёт в
вывод `health-check`, в письмо о здоровье и в ответ Telegram-бота; его печатает
встроенный Python в workflow, чей журнал шага публичен; вместе с базой он
уходит в бэкап. А пишут туда текст пойманной ошибки как есть — ошибка базы
кладёт в текст параметры запроса.

Решение владельца 2026-10-09: чистить при записи, а не в каждом месте показа.
Точка одна — валидатор на самой колонке (`storage.stored_error_text`), а не три
обработчика в `src/main.py`: обработчики переписывают, и текст в них собирают
по-разному, а колонка одна. То же правило стоит на копии текста в
`scrape_requests` и на списке ошибок по сайтам в `runs.run_quality`.

Чего это не закрывает:

- правило видит только «@» (`logging_setup.without_addresses`). Telegram-
  идентификатор — число: ошибку базы, в параметрах запроса которой он лежит,
  оно пропустит. Запросы самого сбора таких параметров не содержат;
- запись мимо ORM — `update(Run).values(error_message=…)`, сырой SQL, правку
  руками в psql — валидатор не видит. В `src/` таких записей нет, и тест ниже
  следит, чтобы не появились;
- строки, записанные до правки. На проде 2026-10-09 «@» нет ни в одном из 276
  текстов ошибки; проверка — docs/RUNBOOK.md «Адреса в журнал не пишутся».
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src import api, health, main, storage, telegram_bot
from src.logging_setup import TEXT_WITHHELD
from src.scrapers.base import ScrapeResult
from tests.test_log_carries_no_address import cli_logging  # noqa: F401 — фикстура

SRC = Path(__file__).resolve().parent.parent / "src"
ADDRESS = "viewer@client.example"
# Ошибка базы на таблице пользователей, как её печатает SQLAlchemy: текст
# драйвера, запрос и его параметры.
_DB_ERROR = IntegrityError(
    "INSERT INTO tenant_users (tenant_id, email) VALUES (%(tenant_id)s, %(email)s)",
    {"tenant_id": 1, "email": ADDRESS},
    Exception(f"duplicate key value violates unique constraint\nDETAIL:  Key (email)=({ADDRESS})"),
)


# ─── Правило ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        (None, None),
        ("", ""),
        # Без «@» текст не меняется ни на знак.
        ("RunQualityFailure: run quality failed", "RunQualityFailure: run quality failed"),
        (
            "run quality degraded: aloe=degraded(coverage)",
            "run quality degraded: aloe=degraded(coverage)",
        ),
        # С «@» от текста остаётся класс ошибки, если текст с него начинается.
        (f"ValueError: no such user {ADDRESS}", f"ValueError: {TEXT_WITHHELD}"),
        (
            f"sqlalchemy.exc.IntegrityError: Key (email)=({ADDRESS})",
            f"sqlalchemy.exc.IntegrityError: {TEXT_WITHHELD}",
        ),
        (
            "site_fatal: proxy http://user:secret@proxy.example refused",
            f"site_fatal: {TEXT_WITHHELD}",
        ),
        (f"IntegrityError: {_DB_ERROR}", f"IntegrityError: {TEXT_WITHHELD}"),
        # Начала-класса нет — не остаётся ничего.
        (f"no such user {ADDRESS}", TEXT_WITHHELD),
        (f"reaped stale run: owner {ADDRESS}", TEXT_WITHHELD),
        (str(_DB_ERROR), TEXT_WITHHELD),
        # Адрес в самом начале началом-классом не считается.
        (f"{ADDRESS}: mailbox unavailable", TEXT_WITHHELD),
        ("viewer.name@client.example: mailbox unavailable", TEXT_WITHHELD),
        # Запись адреса, которую шаблон маски не узнаёт, скрыта так же.
        ('RuntimeError: "viewer name"@client.example rejected', f"RuntimeError: {TEXT_WITHHELD}"),
        # Не адрес, но с «@»: правило видит знак, а не смысл.
        ("TimeoutError: pharmacy-monitor-scrape@aloe.service", f"TimeoutError: {TEXT_WITHHELD}"),
    ],
)
def test_error_text_is_stored_without_an_at_sign(written, stored):
    assert storage.stored_error_text(written) == stored
    assert "@" not in (storage.stored_error_text(written) or "")


def _models_with_an_error_text() -> list[type]:
    return [
        mapper.class_
        for mapper in storage.Base.registry.mappers
        if "error_message" in mapper.columns
    ]


def test_every_model_with_an_error_text_cleans_it_on_write():
    models = _models_with_an_error_text()
    assert {storage.Run, storage.ScrapeRequest} <= set(models)
    unguarded = [
        model.__name__ for model in models if "error_message" not in model.__mapper__.validators
    ]
    assert unguarded == [], (
        f"У модели {', '.join(unguarded)} есть колонка `error_message`, но текст в "
        "неё пишется как есть. Текст ошибки видит сотрудник клиента и публичный "
        "журнал шага; поставь на колонку тот же валидатор, что у `Run`: "
        '`@validates("error_message")` → `stored_error_text(text)`.'
    )
    for model in models:
        row = model(error_message=f"ValueError: {ADDRESS}")
        assert row.error_message == f"ValueError: {TEXT_WITHHELD}", model.__name__
        row.error_message = f"gone: {ADDRESS}"
        assert row.error_message == f"gone: {TEXT_WITHHELD}", model.__name__
        # Дописывание к уже записанному тексту — такая же запись.
        row.error_message = f"{row.error_message} | reaped by {ADDRESS}"
        assert row.error_message == f"gone: {TEXT_WITHHELD}", model.__name__
        row.error_message = f"reaped by {ADDRESS}"
        assert row.error_message == TEXT_WITHHELD, model.__name__
        row.error_message = None
        assert row.error_message is None, model.__name__


def test_what_reaches_the_database_is_what_the_model_holds(db_session):
    run = storage.Run(status="failed", error_message=f"IntegrityError: {_DB_ERROR}")
    request = storage.ScrapeRequest(mode="category", status="failed", error_message=str(_DB_ERROR))
    db_session.add_all([run, request])
    db_session.commit()

    in_runs = db_session.execute(text("SELECT error_message FROM runs")).scalar_one()
    in_requests = db_session.execute(text("SELECT error_message FROM scrape_requests")).scalar_one()

    assert in_runs == f"IntegrityError: {TEXT_WITHHELD}"
    assert in_requests == TEXT_WITHHELD


# ─── Сбор падает: что ляжет в базу и что увидят читатели ─────────────────────


@pytest.fixture
def failed_run(cli_logging, db_session, monkeypatch):  # noqa: F811 — фикстура
    """`run` упал на ошибке базы, в параметрах запроса которой лежит адрес."""
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    request = storage.ScrapeRequest(mode="category", sites="aloe", status="running")
    db_session.add(request)
    db_session.commit()

    async def fail(*args, **kwargs):
        raise _DB_ERROR

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    monkeypatch.setattr(main, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main.watchlist, "categories_for_site", lambda session, site, only_category_id=None: ["cat"]
    )
    monkeypatch.setattr(main, "baselines_for_sites", lambda *args: {"aloe": None})
    monkeypatch.setattr(main, "scrape_all", fail)

    result = CliRunner().invoke(
        main.cli,
        [
            "run",
            "--site",
            "aloe",
            "--mode",
            "category",
            "--no-alerts",
            "--request-id",
            str(request.id),
        ],
    )
    assert result.exit_code != 0, result.output

    db_session.expire_all()
    return db_session.query(storage.Run).one()


def test_a_failed_run_stores_the_error_class_and_no_address(failed_run, db_session):
    assert failed_run.status == "failed"
    assert failed_run.error_message == f"IntegrityError: {TEXT_WITHHELD}"
    request = db_session.query(storage.ScrapeRequest).one()
    assert request.status == "failed"
    # Копия для очереди запросов из дашборда: её пишет `run`, когда сбор дошёл
    # до конца, и watcher — своим текстом, когда команда уже вышла.
    main.mark_scrape_request_terminal(db_session, request.id, failed_run)
    assert request.error_message == f"IntegrityError: {TEXT_WITHHELD}"
    api.internal_scrape_complete(
        request.id, api.ScrapeCompleteIn(error_message=f"watcher: killed by {ADDRESS}"), db_session
    )
    assert request.error_message == f"IntegrityError: {TEXT_WITHHELD}"
    stored = db_session.execute(
        text("SELECT error_message FROM runs UNION ALL SELECT error_message FROM scrape_requests")
    ).scalars()
    assert all(value and "@" not in value for value in stored)


def test_every_reader_of_a_failed_run_sees_no_address(failed_run, db_session):
    # Страница прогонов: её видит роль viewer.
    row = api._run_row_out(failed_run)
    assert row["error_message"] == f"IntegrityError: {TEXT_WITHHELD}"
    # Проверка здоровья: строка идёт в вывод команды, в письмо и в ответ бота.
    report = health.check_health(db_session)
    failed = [issue for issue in report.issues if issue.code == "last_run_failed"]
    assert len(failed) == 1
    assert TEXT_WITHHELD in failed[0].message
    everything = "\n".join(
        [
            *(issue.message for issue in report.issues),
            *(repr(issue.context) for issue in report.issues),
            health.render_alert_html(report),
            telegram_bot.cmd_status(db_session, "1", ""),
            repr(row),
        ]
    )
    assert "@" not in everything, everything


def test_the_site_errors_of_a_run_are_stored_without_an_address():
    """`runs.run_quality` отдаётся тем же ответом API, что и текст ошибки."""
    result = ScrapeResult(
        site="aloe",
        errors=[
            f"promos: IntegrityError: {_DB_ERROR}",
            f"url=https://aloe.az/1/: mailto:{ADDRESS}",
            "category=dermanlar: timeout",
        ],
        items_expected=1,
        items_failed=1,
    )

    _, quality = main.classify_run_quality([result], ["aloe"], mode="category")

    assert quality["sites"]["aloe"]["errors"] == [
        f"promos: {TEXT_WITHHELD}",
        TEXT_WITHHELD,
        "category=dermanlar: timeout",
    ]
    assert "@" not in repr(quality)


# ─── Мимо валидатора в `src/` не пишет никто ─────────────────────────────────

_SQL_WRITE = re.compile(r"\b(?:update|insert)\b", re.I)


def writes_past_the_model(source: str) -> list[int]:
    """Строки, где `error_message` пишут мимо ORM: `values(error_message=…)`,
    словарь для `update()` или сырой SQL. Такую запись валидатор не видит."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if called in {"values", "update", "execute"}:
                for sub in ast.walk(node):
                    named = isinstance(sub, ast.keyword) and sub.arg == "error_message"
                    keyed = isinstance(sub, ast.Dict) and any(
                        isinstance(key, ast.Constant)
                        and key.value == "error_message"
                        or isinstance(key, ast.Attribute)
                        and key.attr == "error_message"
                        for key in sub.keys
                    )
                    if named or keyed:
                        found.append(sub.value.lineno if named else sub.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "error_message" in node.value
            and _SQL_WRITE.search(node.value)
        ):
            found.append(node.lineno)
    return sorted(set(found))


def test_nothing_in_src_writes_the_error_text_past_the_model():
    found = [
        f"{path.relative_to(SRC.parent)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in writes_past_the_model(path.read_text(encoding="utf-8"))
    ]
    assert found == [], (
        f"`error_message` пишут мимо модели: {', '.join(found)}. Текст ошибки "
        "чистит валидатор колонки (`storage.stored_error_text`), а он видит "
        "только запись через ORM: `row.error_message = …` или `Model(error_message=…)`. "
        "Пиши так — или прогони текст через `storage.stored_error_text` сам."
    )


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("run.error_message = str(e)", []),
        ("storage.Run(status='failed', error_message=str(e))", []),
        ("out = {'error_message': run.error_message}", []),
        ("log.info('run_failed', error_message=1)", []),
        ("session.execute(update(Run).values(error_message=str(e)))", [1]),
        ("session.execute(\n    update(Run),\n    {'error_message': str(e)},\n)", [3]),
        ("session.query(Run).update({Run.error_message: str(e)})", [1]),
        ("session.execute(text('UPDATE runs SET error_message = :m'), {'m': str(e)})", [1]),
        ("SQL = '''insert into runs (error_message)\nvalues (:m)'''", [1]),
    ],
)
def test_the_check_sees_a_write_past_the_model(source, lines):
    assert writes_past_the_model(source) == lines
