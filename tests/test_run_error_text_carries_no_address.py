"""Текст ошибки прогона ложится в базу без адреса.

`runs.error_message` читают не только операторы. Страницу прогонов видит роль
viewer — сотрудник клиента; текст стоит в строке `last_run_failed`, а она идёт в
вывод `health-check`, в письмо о здоровье и в ответ Telegram-бота; его печатает
встроенный Python в workflow, чей журнал шага публичен; вместе с базой он
уходит в бэкап. А пишут туда текст пойманной ошибки как есть — ошибка базы
кладёт в текст параметры запроса.

Решение владельца 2026-10-09: чистить при записи, а не в каждом месте показа.
Точка одна — валидаторы колонок модели (`storage.stored_error_text`,
`storage.stored_error_details`), а не обработчики в `src/main.py`: пишут в эти
колонки больше десяти мест в четырёх файлах, текст в них собирают по-разному и
переписывают, а колонок четыре: `runs.error_message`, его копия в
`scrape_requests`, причина недоверия каталогу (`runs.catalog_verification_reason`)
и подробности прогона (`runs.run_quality` — список ошибок по сайтам, ошибка
каждой категории, причина незавершённого маршрута, заметка о восстановлении).

Чего это не закрывает:

- правило видит только «@». Telegram-идентификатор — число: ошибку базы, в
  параметрах запроса которой он лежит, оно пропустит, как и имя человека.
  Запросы самого сбора таких параметров не содержат;
- запись мимо ORM — `update(Run).values(…)`, `bulk_*_mappings`, сырой SQL,
  правку руками в psql — валидатор не видит. В `src/` таких записей нет; тест
  ниже ловит прямые формы, а словарь, собранный вне вызова, не узнает;
- текст, обрезанный до записи по имени ящика, оставит его начало: «@» в нём
  уже нет. Три места в `src/main.py` (ошибка категории и список ошибок сайта в
  `classify_run_quality`, причина незавершённого маршрута — до 40 знаков) чистят
  текст до обрезки сами; остальные срезы — в скрейперах, в режиме отслеживаемых
  ссылок, в `ai-crawl`, у причины недоверия каталогу (300–500 знаков) — нет;
- то, что дописано в словарь `run_quality` уже после записи в колонку,
  валидатор не увидит; строка, записанная до правки и присвоенная заново без
  изменений, в базе останется прежней;
- ключи внутри `run_quality` (названия категорий, адреса страниц) не чистятся;
- строки, записанные до правки. На проде 2026-10-09 «@» нет ни в одном из 276
  текстов ошибки; проверка — docs/RUNBOOK.md «Текст ошибки прогона».
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest
import structlog
from click.testing import CliRunner
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src import api, health, logging_setup, main, storage, telegram_bot
from src.scrapers.base import RouteStatus, ScrapeResult

SRC = Path(__file__).resolve().parent.parent / "src"
ADDRESS = "viewer@client.example"
WITHHELD = logging_setup.TEXT_WITHHELD
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
            "run quality degraded: aloe=degraded(x) | reaped",
            "run quality degraded: aloe=degraded(x) | reaped",
        ),
        # С «@» от текста остаётся класс ошибки, если текст с него начинается.
        (f"ValueError: no such user {ADDRESS}", f"ValueError: {WITHHELD}"),
        (f"IntegrityError: {_DB_ERROR}", f"IntegrityError: {WITHHELD}"),
        (f"RunQualityFailure: aloe {ADDRESS}", f"RunQualityFailure: {WITHHELD}"),
        ("InvalidProxyStatus: http://user:secret@proxy.example", f"InvalidProxyStatus: {WITHHELD}"),
        (f"Error: page crashed at {ADDRESS}", f"Error: {WITHHELD}"),
        ("TimeoutError: pharmacy-monitor-scrape@aloe.service", f"TimeoutError: {WITHHELD}"),
        # Запись адреса, которую шаблон маски не узнаёт, скрыта так же.
        ('RuntimeError: "viewer name"@client.example rejected', f"RuntimeError: {WITHHELD}"),
        # Начала-класса нет — не остаётся ничего.
        (f"no such user {ADDRESS}", WITHHELD),
        (str(_DB_ERROR), WITHHELD),
        (f"site_fatal: KeyError: '{ADDRESS}'", WITHHELD),
        (f"sqlalchemy.exc.IntegrityError: Key (email)=({ADDRESS})", WITHHELD),
        # Слово перед двоеточием — не класс: им бывает и имя ящика, и домен.
        (f"{ADDRESS}: mailbox unavailable", WITHHELD),
        ("viewer.name: mailbox viewer.name@client.example is full", WITHHELD),
        ("client.example: recipient viewer@client.example rejected", WITHHELD),
        ("Viewer: mailbox viewer@client.example is full", WITHHELD),
        ("viewerError@client.example: bounced", WITHHELD),
        # « | » внутри одного текста его не делит: по обе стороны — та же ошибка.
        ("KeyError: 'viewer | viewer@client.example'", f"KeyError: {WITHHELD}"),
        (f"(email)=(viewer) | bounced {ADDRESS}", WITHHELD),
        (
            "RunQualityFailure: run quality failed | reaped: pharmacy-monitor-scrape@aloe.service",
            f"RunQualityFailure: {WITHHELD}",
        ),
    ],
)
def test_error_text_is_stored_without_an_at_sign(written, stored):
    assert storage.stored_error_text(written) == stored
    assert "@" not in (storage.stored_error_text(written) or "")


@pytest.mark.parametrize(
    ("before", "written", "stored"),
    [
        # Заметка, дописанная к записанному тексту, чистится отдельно от него.
        (
            "RunQualityFailure: run quality failed",
            "RunQualityFailure: run quality failed | reaped: pharmacy-monitor-scrape@aloe.service",
            f"RunQualityFailure: run quality failed | {WITHHELD}",
        ),
        (
            "aloe failed",
            f"aloe failed | post-persist: KeyError: '{ADDRESS}'",
            f"aloe failed | {WITHHELD}",
        ),
        # Строка, записанная до правки: прежний текст чистится тоже.
        (
            f"ValueError: {ADDRESS}",
            f"ValueError: {ADDRESS} | post-persist: exit 1",
            f"ValueError: {WITHHELD} | post-persist: exit 1",
        ),
        # Не дописывание — текст целиком.
        ("aloe failed", f"other | reaped by {ADDRESS}", WITHHELD),
        ("aloe", f"aloe failed | reaped by {ADDRESS}", WITHHELD),
        ("b", f"a | b | reaped by {ADDRESS}", WITHHELD),
        (None, f"KeyError: 'viewer | {ADDRESS}'", f"KeyError: {WITHHELD}"),
        ("", f"a | {ADDRESS}", WITHHELD),
    ],
)
def test_a_note_appended_to_the_stored_text_does_not_erase_it(before, written, stored):
    assert storage.stored_error_text(written, appended_to=before) == stored


def test_what_is_not_text_passes_as_it_is():
    """Атрибуту модели присваивают и выражение SQL — превращать его в текст нельзя."""
    expression = func.coalesce(storage.Run.error_message, "") + " | reaped"
    run = storage.Run(error_message=expression)
    assert run.error_message is expression
    assert storage.stored_error_text(404) == 404
    assert storage.stored_error_details({"n": 3, "ok": True, "none": None}) == {
        "n": 3,
        "ok": True,
        "none": None,
    }


def test_the_sign_and_the_mark_are_the_ones_the_commands_use():
    """В `storage.py` признак («@») и пометка повторены, а не взяты импортом —
    копии не расходятся. Само правило шире: от текста остаётся класс ошибки."""
    assert storage._ERROR_TEXT_WITHHELD == logging_setup.TEXT_WITHHELD
    for sample in ("", "plain text", f"to {ADDRESS}", "a@b", "@", '"viewer name"@client.example'):
        assert storage.stored_error_text(sample) == logging_setup.without_addresses(sample)
    for sample in ("ValueError: plain", f"ValueError: {ADDRESS}", f"x | {ADDRESS}", "a | b"):
        hidden = logging_setup.without_addresses(sample) == logging_setup.TEXT_WITHHELD
        assert (storage.stored_error_text(sample) != sample) is hidden


def _loaded_with_the_module(statements):
    """Команды, которые исполняются при загрузке файла: всё, кроме тел функций."""
    for node in statements:
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _loaded_with_the_module(getattr(node, field, []))
        for handler in getattr(node, "handlers", []):
            yield from _loaded_with_the_module(handler.body)


def taken_from_src_at_load(source: str) -> set[str]:
    taken = set()
    for node in _loaded_with_the_module(ast.parse(source).body):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "src":
            taken.add(node.module)
        elif isinstance(node, ast.Import):
            taken |= {alias.name for alias in node.names if alias.name.split(".")[0] == "src"}
    return taken


def test_storage_loads_without_the_rest_of_src():
    """Модуль моделей грузят API, миграции и блоки в workflow; файл, выложенный
    отдельно от остальных, обязан загрузиться. Импорт внутри функции не в счёт."""
    taken = taken_from_src_at_load((SRC / "storage.py").read_text(encoding="utf-8"))
    assert taken == {"src._time"}, (
        f"`src/storage.py` при загрузке берёт из `src`: {sorted(taken)}. Каждый такой "
        "модуль обязан лежать на сервере той же версии, иначе не запустится ничего: "
        "ни API, ни миграции, ни сбор. Признак и пометка для текста ошибки повторены "
        "в `storage.py` именно поэтому — не заменяй копию импортом."
    )
    assert taken_from_src_at_load(
        "from src._time import utcnow\n\ndef f():\n    from src import x\n"
    ) == {"src._time"}
    under_a_condition = "if True:\n    from src.logging_setup import TEXT_WITHHELD\n"
    in_a_class = "class A:\n    import src.tenants\n"
    under_try = "try:\n    import x\nexcept ImportError:\n    from src import notifier\n"
    assert taken_from_src_at_load(under_a_condition) == {"src.logging_setup"}
    assert taken_from_src_at_load(in_a_class) == {"src.tenants"}
    assert taken_from_src_at_load(under_try) == {"src"}


# ─── Колонки ─────────────────────────────────────────────────────────────────


def _guarded(model: type) -> set[str]:
    return set(model.__mapper__.validators)


def test_every_column_with_an_error_text_cleans_it_on_write():
    with_an_error_text = [
        mapper.class_
        for mapper in storage.Base.registry.mappers
        if "error_message" in mapper.columns
    ]
    assert {storage.Run, storage.ScrapeRequest} <= set(with_an_error_text)
    unguarded = [m.__name__ for m in with_an_error_text if "error_message" not in _guarded(m)]
    assert unguarded == [], (
        f"У модели {', '.join(unguarded)} есть колонка `error_message`, но текст в "
        "неё пишется как есть. Текст ошибки видит сотрудник клиента и публичный "
        "журнал шага; поставь на колонку тот же валидатор, что у `Run`: "
        '`@validates("error_message")` → `stored_error_text(text)`.'
    )
    assert {"error_message", "catalog_verification_reason", "run_quality"} <= _guarded(storage.Run)

    for model in with_an_error_text:
        row = model(error_message=f"ValueError: {ADDRESS}")
        assert row.error_message == f"ValueError: {WITHHELD}", model.__name__
        row.error_message = f"gone to {ADDRESS}"
        assert row.error_message == WITHHELD, model.__name__
        row.error_message = "KeyError: 'viewer | viewer@client.example'"
        assert row.error_message == f"KeyError: {WITHHELD}", model.__name__
        # Дописывание к уже записанному тексту — такая же запись.
        row.error_message = "RunQualityFailure: run quality failed"
        row.error_message = f"{row.error_message} | reaped by {ADDRESS}"
        assert row.error_message == f"RunQualityFailure: run quality failed | {WITHHELD}"
        row.error_message = None
        assert row.error_message is None, model.__name__


def test_the_reason_a_catalog_is_not_trusted_is_stored_without_an_address():
    run = storage.Run(
        catalog_verification_reason=f"incomplete_routes=aloe:1[KeyError: '{ADDRESS}'x1]"
    )
    assert run.catalog_verification_reason == WITHHELD
    run.catalog_verification_reason = "incomplete_routes=aloe:1[timeout x1]"
    assert run.catalog_verification_reason == "incomplete_routes=aloe:1[timeout x1]"


def _quality_with_addresses() -> dict:
    """Подробности прогона, как их собирает `classify_run_quality` и дописывают
    проверка каталога и восстановление: текст ошибки лежит в пяти местах."""
    result = ScrapeResult(
        site="aloe",
        errors=[f"site_fatal: KeyError: '{ADDRESS}'", "category=dermanlar: timeout"],
        site_fatal=True,
        items_expected=2,
        items_failed=2,
        item_results={
            "dermanlar": {"status": "failed", "error": f"KeyError: '{ADDRESS}'"},
            "bad": {"status": "failed", "error": "TimeoutError: 30s"},
        },
    )
    _, quality = main.classify_run_quality([result], ["aloe"], mode="category")
    site = quality["sites"]["aloe"]
    # Три места `classify_run_quality` чистит сама, до обрезки; здесь проверяется
    # колонка, поэтому текст возвращён таким, каким его записал бы другой код.
    site["errors"][0] = f"site_fatal: KeyError: '{ADDRESS}'"
    site["items"]["dermanlar"]["error"] = f"KeyError: '{ADDRESS}'"
    site["routes_incomplete"] = {"dermanlar": {"reason": f"KeyError: '{ADDRESS}'"}}
    quality["catalog_verification_reason"] = f"incomplete_routes=aloe:1[KeyError: '{ADDRESS}'x1]"
    quality["recovery"] = {"reason": "unit pharmacy-monitor-scrape@aloe.service was killed"}
    quality["checked"] = (f"by {ADDRESS}", 3)
    return quality


def test_the_details_of_a_run_are_stored_without_an_address():
    quality = _quality_with_addresses()
    assert repr(quality).count("@") == 6  # фикстура не пуста: по адресу на каждое место

    run = storage.Run(run_quality=quality)

    site = run.run_quality["sites"]["aloe"]
    assert site["errors"] == [WITHHELD, "category=dermanlar: timeout"]
    assert site["items"]["dermanlar"]["error"] == f"KeyError: {WITHHELD}"
    assert site["items"]["bad"]["error"] == "TimeoutError: 30s"
    assert site["routes_incomplete"]["dermanlar"]["reason"] == f"KeyError: {WITHHELD}"
    assert run.run_quality["catalog_verification_reason"] == WITHHELD
    assert run.run_quality["recovery"]["reason"] == WITHHELD
    assert run.run_quality["checked"] == (WITHHELD, 3)
    assert "@" not in repr(run.run_quality)
    # Не текст остаётся как был.
    assert site["status"] == "failed" and site["items_expected"] == 2 and site["site_fatal"] is True
    # Правится тот же словарь: код, записав его, читает дальше и дописывает.
    assert run.run_quality is quality
    assert main.run_quality_message("failed", quality) == (
        f"run quality failed: aloe=failed(site_fatal): {WITHHELD}"
    )
    quality["financially_eligible"] = False
    assert run.run_quality["financially_eligible"] is False


def test_the_text_is_cleaned_before_it_is_cut():
    """Срез по имени ящика оставил бы его начало без «@» — и правило бы его не узнало."""
    cut_at_the_mailbox = "RuntimeError: cannot notify  viewer.name@client.example"
    assert cut_at_the_mailbox[: main._ROUTE_CAUSE_MAX_LEN].endswith("viewer.name")
    causes = main._route_incomplete_causes(
        RouteStatus(complete=False, abort_reason=cut_at_the_mailbox)
    )
    assert causes == [f"RuntimeError: {WITHHELD}"]

    long = "KeyError: " + "x" * 478 + " viewer.name@client.example"
    assert long[:500].endswith("viewer.name")
    result = ScrapeResult(
        site="aloe",
        errors=[long],
        items_expected=1,
        items_failed=1,
        item_results={"dermanlar": {"status": "failed", "error": long}},
    )
    _, quality = main.classify_run_quality([result], ["aloe"], mode="category")
    assert quality["sites"]["aloe"]["errors"] == [f"KeyError: {WITHHELD}"]
    assert quality["sites"]["aloe"]["items"]["dermanlar"]["error"] == f"KeyError: {WITHHELD}"
    assert "viewer" not in repr(quality)


def test_a_row_written_before_the_rule_is_cleaned_when_a_note_is_added(db_session):
    db_session.add(storage.Run(status="running"))
    db_session.commit()
    db_session.execute(text("UPDATE runs SET error_message = :m"), {"m": f"ValueError: {ADDRESS}"})
    db_session.commit()
    db_session.expire_all()
    run = db_session.query(storage.Run).one()
    assert "@" in run.error_message  # чтение из базы текст не меняет

    run.error_message = f"{run.error_message} | reaped stale run"
    db_session.commit()

    assert db_session.execute(text("SELECT error_message FROM runs")).scalar_one() == (
        f"ValueError: {WITHHELD} | reaped stale run"
    )


def test_the_keys_of_the_details_are_left_alone():
    """Ключи — названия категорий и адреса страниц, которые запросили мы сами, а
    не текст ошибки; скрыть ключ значило бы склеить две записи в одну."""
    page = "https://aloe.az/@brand/"
    run = storage.Run(run_quality={"items": {page: {"error": f"KeyError: '{ADDRESS}'"}, "bad": {}}})
    assert run.run_quality == {"items": {page: {"error": f"KeyError: {WITHHELD}"}, "bad": {}}}


def test_what_reaches_the_database_is_what_the_model_holds(db_session):
    run = storage.Run(
        status="failed",
        error_message=f"IntegrityError: {_DB_ERROR}",
        catalog_verification_reason=f"aloe: {ADDRESS}",
        run_quality=_quality_with_addresses(),
    )
    request = storage.ScrapeRequest(mode="category", status="failed", error_message=str(_DB_ERROR))
    db_session.add_all([run, request])
    db_session.commit()

    in_runs = db_session.execute(
        text("SELECT error_message, catalog_verification_reason, run_quality FROM runs")
    ).one()
    in_requests = db_session.execute(text("SELECT error_message FROM scrape_requests")).scalar_one()

    assert in_runs[0] == f"IntegrityError: {WITHHELD}"
    assert in_runs[1] == WITHHELD
    assert "@" not in str(in_runs[2]) and "dermanlar" in str(in_runs[2])
    assert in_requests == WITHHELD


# ─── Сбор падает: что ляжет в базу и что увидят читатели ─────────────────────


@pytest.fixture
def cli_logging(monkeypatch, tmp_path):
    """`_setup_logging` пишет в `logs/` текущего каталога и меняет общий конфиг."""
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:], root.level = saved[0], saved[1]
    structlog.reset_defaults()


@pytest.fixture
def failed_run(cli_logging, db_session, monkeypatch):
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
    assert failed_run.error_message == f"IntegrityError: {WITHHELD}"
    request = db_session.query(storage.ScrapeRequest).one()
    assert request.status == "failed"
    # Копия для очереди запросов из дашборда: её пишет `run`, когда сбор дошёл
    # до конца, и watcher — своим текстом, когда команда уже вышла.
    main.mark_scrape_request_terminal(db_session, request.id, failed_run)
    assert request.error_message == f"IntegrityError: {WITHHELD}"
    api.internal_scrape_complete(
        request.id, api.ScrapeCompleteIn(error_message=f"watcher: killed by {ADDRESS}"), db_session
    )
    assert request.error_message == f"IntegrityError: {WITHHELD} | {WITHHELD}"
    stored = db_session.execute(
        text("SELECT error_message FROM runs UNION ALL SELECT error_message FROM scrape_requests")
    ).scalars()
    assert all(value and "@" not in value for value in stored)


def test_every_reader_of_a_failed_run_sees_no_address(failed_run, db_session):
    failed_run.run_quality = _quality_with_addresses()
    failed_run.catalog_verification_reason = f"aloe: {ADDRESS}"
    db_session.commit()
    viewer = storage.TenantUser(tenant_id=failed_run.tenant_id, email=ADDRESS, role="viewer")

    # Страница прогонов и раскрытая строка прогона: их видит роль viewer.
    row = api._run_row_out(failed_run)
    breakdown = api.dash_run_breakdown(failed_run.id, viewer, db_session)
    assert row["error_message"] == f"IntegrityError: {WITHHELD}"
    assert breakdown["run_quality"]["sites"]["aloe"]["items"]["dermanlar"]["error"]
    # Проверка здоровья: строка идёт в вывод команды, в письмо и в ответ бота.
    report = health.check_health(db_session)
    failed = [issue for issue in report.issues if issue.code == "last_run_failed"]
    assert len(failed) == 1
    assert WITHHELD in failed[0].message
    everything = "\n".join(
        [
            *(issue.message for issue in report.issues),
            *(repr(issue.context) for issue in report.issues),
            health.render_alert_html(report),
            telegram_bot.cmd_status(db_session, "1", ""),
            repr(row),
            repr(breakdown),
        ]
    )
    assert "@" not in everything, everything


# ─── Мимо валидатора в `src/` не пишет никто ─────────────────────────────────

_GUARDED_COLUMNS = {"error_message", "catalog_verification_reason", "run_quality"}
# Вызовы, которые пишут в базу мимо атрибута модели.
_WRITES_PAST_THE_MODEL = {
    "values",
    "update",
    "execute",
    "bulk_insert_mappings",
    "bulk_update_mappings",
    "on_conflict_do_update",
}
_SQL_WRITE = re.compile(
    r"\bset\b[^;]*?\b(?:{0})\s*=|\binsert\s+into\b[^;]*?\b(?:{0})\b".format(
        "|".join(sorted(_GUARDED_COLUMNS))
    ),
    re.I | re.S,
)


def writes_past_the_model(source: str) -> list[int]:
    """Строки, где колонку с текстом ошибки пишут мимо ORM.

    Видит прямые формы: `values(error_message=…)`, словарь с таким ключом прямо в
    вызове `update`/`execute`/`bulk_*_mappings` и сырой SQL (`SET error_message =`,
    `INSERT INTO … (error_message …)`). Словарь, собранный заранее
    (`values(**fields)`), не узнает. `out.update({"error_message": …})` у
    обычного словаря тоже считает записью: из текста их не различить.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if called not in _WRITES_PAST_THE_MODEL:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.keyword) and sub.arg in _GUARDED_COLUMNS:
                    found.append(sub.value.lineno)
                elif isinstance(sub, ast.Dict) and any(
                    (isinstance(key, ast.Constant) and key.value in _GUARDED_COLUMNS)
                    or (isinstance(key, ast.Attribute) and key.attr in _GUARDED_COLUMNS)
                    for key in sub.keys
                ):
                    found.append(sub.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
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
        f"Колонку с текстом ошибки пишут мимо модели: {', '.join(found)}. Текст "
        "чистит валидатор колонки (`storage.stored_error_text`), а он видит только "
        "запись через ORM: `row.error_message = …` или `Model(error_message=…)`. "
        "Пиши так — или прогони значение через `storage.stored_error_text` "
        "(`stored_error_details` для `run_quality`) сам. Если находка — не запись в "
        "базу, а `словарь.update({…})` для ответа, собери словарь иначе: из текста "
        "их не различить."
    )


def test_no_workflow_block_writes_the_error_text_past_the_model():
    """Блоки в workflow пишут эти колонки тоже (`recover-interrupted-empty-run.yml`),
    и на сервере — выложенным кодом моделей: мимо ORM валидатор их не увидит."""
    from tests.test_workflow_python_reaches_no_person import CODE, WORKFLOWS, python_runs

    found, read = [], 0
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for run in python_runs(path.read_text(encoding="utf-8")):
            if run.kind != CODE:
                continue
            try:
                lines = writes_past_the_model(run.what)
            except SyntaxError:
                continue  # об этом — тест о запусках Python в workflow
            read += 1
            found += [f"{path.name}:{run.line} ({run.origin}), строка кода {n}" for n in lines]
    assert read, "Ни одного блока Python в workflow не прочитано: разбор ослеп."
    assert found == [], (
        f"Код, который запускает workflow, пишет колонку с текстом ошибки мимо модели: "
        f"{'; '.join(found)}. Пиши через модель (`run.error_message = …`) — тогда "
        "текст почистит валидатор колонки."
    )


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("run.error_message = str(e)", []),
        ("storage.Run(status='failed', error_message=str(e))", []),
        ("out = {'error_message': run.error_message, 'run_quality': run.run_quality}", []),
        ("log.info('run_failed', error_message=1)", []),
        ('def f():\n    """Update error_message: set error_message = text of the run."""', []),
        ("HELP = 'insert the id; error_message is shown on the runs page'", []),
        ("session.execute(update(Run).values(error_message=str(e)))", [1]),
        ("session.execute(update(Run).values(run_quality=quality))", [1]),
        (
            "session.execute(\n    update(Run),\n    {'catalog_verification_reason': str(e)},\n)",
            [3],
        ),
        ("session.query(Run).update({Run.error_message: str(e)})", [1]),
        ("session.bulk_update_mappings(Run, [{'id': 1, 'error_message': str(e)}])", [1]),
        ("insert(Run).on_conflict_do_update(set_={'error_message': str(e)})", [1]),
        ("session.execute(text('UPDATE runs SET error_message = :m'), {'m': str(e)})", [1]),
        ("text(f'UPDATE {table} SET status = :s, error_message = :m')", [1]),
        ("SQL = '''insert into runs (status,\n  error_message)\nvalues (:s, :m)'''", [1]),
    ],
)
def test_the_check_sees_a_write_past_the_model(source, lines):
    assert writes_past_the_model(source) == lines
