"""Текст ошибки упавшего запроса не несёт его данных.

Ошибку запроса печатают далеко от места, где она случилась: Sentry забирает её
текст в событие, uvicorn и CLI пишут трассировку в журнал (журнал шага GitHub
Actions публичный), `run` кладёт её в `runs.error_message`. До правки
2026-10-11 в этом тексте стояли данные запроса — дважды:

    [parameters: {'magic_token_1': '…'}]            дописывает SQLAlchemy
    DETAIL:  Failing row contains (2, 1, адрес, …)   пишет сам PostgreSQL

Здесь запросы падают по-настоящему — на PostgreSQL, от чужого замка и от
ограничений таблицы — и проверяется всё, что из ошибки можно напечатать. У
каждого случая есть контроль: упавший запрос данные действительно нёс (они
лежат в `exc.params` или в `exc.orig.diag`, откуда их никто не печатает), и
проба под uvicorn видит их, когда движок создан без защиты.

Чего эти тесты НЕ проверяют: значения в переменных кадров и в теле запроса
(события Sentry собирают их отдельно, это настройки `sentry_sdk.init`), токен
в адресе `/auth/verify?token=…`, текст ошибок, которые пишет не сервер базы, а
драйвер или Python. Первую строку ошибки сервера защита оставляет как есть
везде, кроме ошибок данных (класс 22): значение, переданное туда, где сервер
ждёт имя или выражение, в ней останется.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from src import storage, tenants

REPO = Path(__file__).resolve().parents[1]

ADDRESS = "canary.owner@client-pharmacy.example"
# Вид bcrypt-хеша, сам хеш выдуман: настоящий считается четверть секунды.
STORED_HASH = "$2b$12$" + "StoredCanaryHash./0123456789abcdefghijklmnopqrstuvwxy"
NEW_HASH = "$2b$12$" + "NewCanaryHash./0123456789abcdefghijklmnopqrstuvwxyzAB"
_BCRYPT_SHAPED = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}")
_HIDDEN_BY_SQLALCHEMY = "[SQL parameters hidden due to hide_parameters=True]"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI эти тесты обязаны исполняться, а DATABASE_URL — не PostgreSQL: "
            "DETAIL с данными строки пишет только он, на SQLite проверять нечего."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


def _printed(exc: BaseException) -> str:
    """Всё, что из ошибки печатают: текст, repr и трассировка вместе с причиной."""
    parts = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts += [str(orig), repr(orig)]
    return "\n".join(parts)


# ─── PostgreSQL: настоящие сбои на движке приложения ────────────────────────


@pytest.fixture(scope="module")
def schema() -> Iterator[tuple[str, str]]:
    """Своя схема с таблицами проекта; строит и убирает её посторонний движок."""
    url = _database_url()
    name = f"statement_data_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    search_path = f"-csearch_path={name}"
    builder = create_engine(url, connect_args={"options": search_path})
    storage.Base.metadata.create_all(builder)
    builder.dispose()
    try:
        yield url, search_path
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


def _application_url(schema: tuple[str, str]) -> str:
    """Адрес базы, каким его получило бы приложение: своя схема и короткий lock_timeout."""
    url, search_path = schema
    return (
        make_url(url)
        .update_query_dict({"options": f"{search_path} -clock_timeout=300"})
        .render_as_string(hide_password=False)
    )


@pytest.fixture
def app_engine(schema):
    """Движок, созданный так же, как его создаёт приложение, — `storage.make_engine`."""
    engine = storage.make_engine(_application_url(schema))
    yield engine
    engine.dispose()


@pytest.fixture
def outsider(schema):
    """Другой процесс у той же базы: держит замки, которых ждёт приложение."""
    url, search_path = schema
    engine = create_engine(url, connect_args={"options": search_path})
    yield engine
    engine.dispose()


@pytest.fixture
def user(schema, outsider) -> Iterator[SimpleNamespace]:
    """Пользователь с адресом, хешем пароля и действующим токеном входа."""
    Session = sessionmaker(outsider, expire_on_commit=False, autoflush=False)
    with Session() as session:
        tenant = tenants.get_or_create_default(session)
        row = storage.TenantUser(
            tenant_id=tenant.id, email=ADDRESS, role="admin", password_hash=STORED_HASH
        )
        session.add(row)
        session.commit()
        # Токен выдаёт приложение: что оно кладёт в базу и чем ищет — сам токен
        # или его хеш, — тест не предполагает и не пускает наружу ни то ни другое.
        token = tenants.issue_magic_token(session, ADDRESS)
        found = SimpleNamespace(
            id=row.id,
            tenant_id=tenant.id,
            token=token,
            token_forms=(token, hashlib.sha256(token.encode()).hexdigest()),
        )
    yield found
    with outsider.begin() as connection:
        connection.execute(text("DELETE FROM tenant_users"))


def _locked(outsider, mode: str):
    """Чужая транзакция с замком на таблице пользователей — пока открыт контекст."""
    connection = outsider.connect()
    connection.begin()
    connection.execute(text(f"LOCK TABLE tenant_users IN {mode} MODE"))
    return connection


def _session(engine):
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)()


def test_lock_wait_on_lookup_by_token_does_not_print_the_token(app_engine, outsider, user):
    with _locked(outsider, "ACCESS EXCLUSIVE"), _session(app_engine) as session:
        with pytest.raises(OperationalError) as caught:
            tenants.verify_magic_token(session, user.token)

    exc = caught.value
    # Контроль: запрос нёс токен (или его хеш) параметром.
    assert any(form in repr(exc.params) for form in user.token_forms)
    printed = _printed(exc)
    for form in user.token_forms:
        assert form not in printed
    # Что остаётся для разбора сбоя: класс ошибки, запрос и слова сервера — здесь
    # целиком и без пометки, потому что скрывать в них нечего.
    assert "LockNotAvailable" in printed
    assert str(exc.orig) == "canceling statement due to lock timeout"
    assert "tenant_users" in printed
    assert _HIDDEN_BY_SQLALCHEMY in printed


def test_lock_wait_on_password_write_does_not_print_the_hash(app_engine, outsider, user):
    # SHARE: читать можно, писать нельзя — как при долгом чужом чтении с замком.
    with _locked(outsider, "SHARE"), _session(app_engine) as session:
        row = session.get(storage.TenantUser, user.id)
        row.password_hash = NEW_HASH
        with pytest.raises(OperationalError) as caught:
            session.commit()

    exc = caught.value
    assert NEW_HASH in repr(exc.params)
    printed = _printed(exc)
    assert not _BCRYPT_SHAPED.search(printed)
    assert "UPDATE tenant_users SET password_hash" in printed


def test_violated_constraint_does_not_print_the_row(app_engine, user):
    """NOT NULL: PostgreSQL приводит в DETAIL всю строку — адрес, хеш пароля, токен."""
    with _session(app_engine) as session:
        with pytest.raises(IntegrityError) as caught:
            session.execute(
                update(storage.TenantUser).where(storage.TenantUser.id == user.id).values(role=None)
            )

    exc = caught.value
    # Контроль: сервер строку прислал — она в разобранной ошибке драйвера.
    detail = exc.orig.diag.message_detail
    assert ADDRESS in detail and STORED_HASH in detail
    assert any(form in detail for form in user.token_forms)
    printed = _printed(exc)
    for secret in (ADDRESS, STORED_HASH, *user.token_forms):
        assert secret not in printed
    assert "DETAIL" not in printed
    assert 'null value in column "role" of relation "tenant_users"' in printed
    assert "NotNullViolation" in printed
    assert storage._SERVER_DETAILS_HIDDEN in printed


def test_duplicate_key_does_not_print_the_address(app_engine, user):
    with _session(app_engine) as session:
        with pytest.raises(IntegrityError) as caught:
            session.execute(
                insert(storage.TenantUser).values(
                    tenant_id=user.tenant_id, email=ADDRESS, role="viewer"
                )
            )

    exc = caught.value
    assert ADDRESS in repr(exc.params)
    assert ADDRESS in exc.orig.diag.message_detail
    printed = _printed(exc)
    assert ADDRESS not in printed
    assert 'unique constraint "uq_tenant_email"' in printed


def test_value_of_the_wrong_type_is_not_printed(app_engine, user):
    """Ошибка типа: значение стоит прямо в первой строке ошибки сервера."""
    not_a_number = "canary-not-a-number-5f1c9a77"
    with _session(app_engine) as session:
        with pytest.raises(DBAPIError) as caught:
            session.execute(
                update(storage.TenantUser)
                .where(storage.TenantUser.id == user.id)
                .values(tenant_id=not_a_number)
            )

    exc = caught.value
    assert not_a_number in exc.orig.diag.message_primary
    printed = _printed(exc)
    assert not_a_number not in printed
    # Первой строки нет вовсе; что случилось, говорят класс ошибки и её код.
    assert "InvalidTextRepresentation" in printed
    assert f"data exception, SQLSTATE 22P02 {storage._SERVER_DETAILS_HIDDEN}" in printed


def test_value_too_long_is_named_by_class_and_code(app_engine, user):
    too_long = "x" * 300
    with _session(app_engine) as session:
        with pytest.raises(DBAPIError) as caught:
            session.execute(
                update(storage.TenantUser)
                .where(storage.TenantUser.id == user.id)
                .values(role=too_long)
            )

    printed = _printed(caught.value)
    assert too_long not in printed
    assert "StringDataRightTruncation" in printed
    assert "data exception, SQLSTATE 22001" in printed


def test_raise_with_an_empty_message_is_named_by_its_code(app_engine):
    with app_engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text("DO $$ BEGIN RAISE EXCEPTION ''; END $$"))

    assert str(caught.value.orig) == f"SQLSTATE P0001 {storage._SERVER_DETAILS_HIDDEN}"


def test_error_at_commit_does_not_print_the_key(app_engine, outsider):
    """Отложенное ограничение падает на COMMIT: у ошибки нет ни запроса, ни параметров."""
    missing = 987654321
    with outsider.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE deferred_probe (id integer PRIMARY KEY, parent integer "
                "REFERENCES deferred_probe (id) DEFERRABLE INITIALLY DEFERRED)"
            )
        )
    try:
        with app_engine.connect() as connection:
            connection.execute(
                text("INSERT INTO deferred_probe (id, parent) VALUES (1, :parent)"),
                {"parent": missing},
            )
            with pytest.raises(IntegrityError) as caught:
                connection.commit()
    finally:
        with outsider.begin() as connection:
            connection.execute(text("DROP TABLE deferred_probe"))

    exc = caught.value
    # Контроль: это ошибка без запроса, и значение сервер прислал.
    assert exc.statement is None
    assert str(missing) in exc.orig.diag.message_detail
    printed = _printed(exc)
    assert str(missing) not in printed
    assert "ForeignKeyViolation" in printed
    assert storage._SERVER_DETAILS_HIDDEN in printed


def test_parameters_logged_by_the_server_are_not_printed(schema, user):
    """Сервер с `log_parameter_max_length_on_error` приписывает параметры к самой ошибке.

    Так на сервере смотрят параметры упавшего запроса (docs/RUNBOOK.md,
    «Параметры упавшего запроса»): они ложатся в журнал PostgreSQL — и строкой
    CONTEXT уходят клиенту, мимо `hide_parameters`.
    """
    url, search_path = schema
    engine = storage.make_engine(
        make_url(url)
        .update_query_dict({"options": f"{search_path} -clog_parameter_max_length_on_error=200"})
        .render_as_string(hide_password=False)
    )
    try:
        try:
            engine.connect().close()
        except OperationalError as refused:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"настройку сервера задаёт только суперпользователь: {refused.orig}")
        with _session(engine) as session:
            with pytest.raises(IntegrityError) as caught:
                session.execute(
                    insert(storage.TenantUser).values(
                        tenant_id=user.tenant_id,
                        email=ADDRESS,
                        role="viewer",
                        password_hash=NEW_HASH,
                    )
                )
    finally:
        engine.dispose()

    exc = caught.value
    # Контроль: сервер параметры прислал.
    context = exc.orig.diag.context
    assert ADDRESS in context and NEW_HASH in context
    printed = _printed(exc)
    assert ADDRESS not in printed
    assert not _BCRYPT_SHAPED.search(printed)
    assert "CONTEXT" not in printed


# ─── SQLite: обе ветки `make_engine`, без PostgreSQL ────────────────────────


@pytest.mark.parametrize("location", ["relative", "absolute"])
def test_sqlite_engine_hides_parameters(tmp_path, monkeypatch, location):
    monkeypatch.chdir(tmp_path)
    url = (
        "sqlite:///data/db.sqlite" if location == "relative" else f"sqlite:///{tmp_path}/db.sqlite"
    )
    engine = storage.make_engine(url)
    try:
        storage.Base.metadata.create_all(engine)
        with _session(engine) as session:
            tenant = tenants.get_or_create_default(session)
            values = {"tenant_id": tenant.id, "email": ADDRESS, "password_hash": STORED_HASH}
            session.execute(insert(storage.TenantUser).values(**values, role="admin"))
            with pytest.raises(IntegrityError) as caught:
                session.execute(insert(storage.TenantUser).values(**values, role="viewer"))
    finally:
        engine.dispose()

    assert ADDRESS in repr(caught.value.params)
    printed = _printed(caught.value)
    assert ADDRESS not in printed and STORED_HASH not in printed
    assert _HIDDEN_BY_SQLALCHEMY in printed


# ─── Правило о тексте ошибки сервера — без базы ─────────────────────────────


_SAME = object()


def _driver_error(
    text_: str, *, primary: str | None, sqlstate: object, diag_sqlstate: object = _SAME, **fields
):
    """Ошибка, какой её отдаёт psycopg: текст, код и разобранные поля сервера.

    Код у psycopg лежит в двух местах — у самой ошибки и в `diag`; по умолчанию
    оба одинаковы.
    """
    error = Exception(text_)
    error.sqlstate = sqlstate
    error.diag = SimpleNamespace(
        sqlstate=sqlstate if diag_sqlstate is _SAME else diag_sqlstate,
        message_primary=primary,
        message_detail=fields.get("detail"),
        message_hint=fields.get("hint"),
        context=fields.get("context"),
    )
    return error


def _handled(original: Exception, **context) -> tuple[str, str]:
    """Прогнать ошибку через обработчик движка; вернуть её текст и текст обёртки.

    Контекст — со всеми полями настоящего `ExceptionContext`: обработчик, который
    начнёт смотреть на любое из них, получит значение, а не AttributeError.
    """
    wrapped = Exception(f"(builtins.Exception) {original}")
    fields = {
        "original_exception": original,
        "sqlalchemy_exception": wrapped,
        "statement": "UPDATE tenant_users SET role=%(role)s",
        "parameters": {"role": "s3cret"},
        "execution_context": object(),
        "connection": object(),
        "cursor": object(),
        "engine": object(),
        "dialect": object(),
        "chained_exception": wrapped,
        "is_disconnect": False,
        "is_pre_ping": False,
        "invalidate_pool_on_disconnect": True,
        **context,
    }
    storage._keep_statement_data_out_of_driver_error(SimpleNamespace(**fields))
    return str(original), str(wrapped)


@pytest.mark.parametrize(
    ("sqlstate", "primary"),
    [
        ("22P02", 'invalid input syntax for type integer: "s3cret"'),
        ("22003", 'value "s3cret" is out of range for type integer'),
        # Значение без кавычек и значение в кавычках другого языка сервера.
        ("22008", "date/time field value out of range: s3cret"),
        ("22P02", "ungültige Eingabesyntax für Typ integer: »s3cret«"),
        ("22023", 'time zone "s3cret" not recognized'),
        # Значения в строке нет, но и она не остаётся: правило не разбирает слова.
        ("22001", "value too long for type character varying(100)"),
    ],
)
def test_data_exception_keeps_no_words_of_the_server(sqlstate, primary):
    original, wrapped = _handled(_driver_error(primary, primary=primary, sqlstate=sqlstate))

    assert original == f"data exception, SQLSTATE {sqlstate} {storage._SERVER_DETAILS_HIDDEN}"
    assert wrapped == f"(builtins.Exception) {original}"


@pytest.mark.parametrize(
    ("on_error", "in_diag"), [("22P02", None), (None, "22P02")], ids=["error", "diag"]
)
def test_data_exception_is_known_by_either_place_of_its_code(on_error, in_diag):
    primary = 'invalid input syntax for type integer: "s3cret"'
    error = _driver_error(primary, primary=primary, sqlstate=on_error, diag_sqlstate=in_diag)

    assert _handled(error)[0] == (
        f"data exception, SQLSTATE 22P02 {storage._SERVER_DETAILS_HIDDEN}"
    )


@pytest.mark.parametrize("sqlstate", [None, "", "22P0", " 22P02", b"22P02", 22002])
def test_server_error_with_an_unreadable_code_is_withheld(sqlstate):
    """Первая строка есть, а класс ошибки не узнать: вдруг это ошибка данных."""
    primary = 'invalid input syntax for type integer: "s3cret"'

    original, wrapped = _handled(_driver_error(primary, primary=primary, sqlstate=sqlstate))

    assert original == storage._DRIVER_TEXT_WITHHELD
    assert "s3cret" not in wrapped


@pytest.mark.parametrize(
    ("sqlstate", "primary"),
    [
        # Вне класса 22 в кавычках первой строки — имена: ограничения, колонки.
        ("23505", 'duplicate key value violates unique constraint "uq_tenant_email"'),
        ("42703", 'column "role" does not exist'),
        ("55P03", "canceling statement due to lock timeout"),
    ],
)
def test_first_line_outside_data_exceptions_is_kept(sqlstate, primary):
    assert _handled(_driver_error(primary, primary=primary, sqlstate=sqlstate))[0] == primary


@pytest.mark.parametrize("field", ["detail", "hint", "context"])
@pytest.mark.parametrize(
    ("sqlstate", "primary"),
    [
        ("23514", 'new row for relation "tenant_users" violates check constraint "c"'),
        ("57014", "canceling statement due to statement timeout"),
        ("40P01", "deadlock detected"),
        ("57P01", "terminating connection due to administrator command"),
    ],
)
@pytest.mark.parametrize(
    "context",
    [
        {},
        # COMMIT: ни запроса, ни контекста исполнения.
        {"statement": None, "parameters": None, "execution_context": None, "cursor": None},
        # Обрыв соединения: сервер успел прислать текст.
        {"is_disconnect": True},
        # Сбой до соединения и на проверке соединения из пула.
        {"connection": None, "is_pre_ping": True, "invalidate_pool_on_disconnect": False},
    ],
    ids=["statement", "commit", "disconnect", "pre-ping"],
)
def test_everything_after_the_first_line_is_dropped(field, sqlstate, primary, context):
    error = _driver_error(
        f"{primary}\n{field.upper()}:  s3cret",
        primary=primary,
        sqlstate=sqlstate,
        **{field: "s3cret"},
    )

    original, wrapped = _handled(error, **context)

    assert original == f"{primary} {storage._SERVER_DETAILS_HIDDEN}"
    assert wrapped == f"(builtins.Exception) {original}"


def test_position_excerpt_is_dropped_without_a_note():
    primary = "canceling statement due to lock timeout"
    error = _driver_error(
        f"{primary}\nLINE 2: FROM tenant_users\n             ^", primary=primary, sqlstate="55P03"
    )

    assert _handled(error) == (primary, f"(builtins.Exception) {primary}")


def test_error_that_is_not_from_the_server_is_left_alone():
    """Обрыв соединения, значение, которое драйвер не смог передать: ни кода, ни первой строки."""
    lost = _driver_error("server closed the connection unexpectedly", primary=None, sqlstate=None)
    assert _handled(lost)[0] == "server closed the connection unexpectedly"

    plain = ValueError("no diag at all")
    assert _handled(plain)[0] == "no diag at all"


def test_empty_first_line_is_replaced_by_the_code():
    """`RAISE EXCEPTION ''`: первая строка есть, но пустая — это ошибка сервера, а не сбой разбора."""
    context = "PL/pgSQL function inline_code_block line 1 at RAISE"
    error = _driver_error(f"\nCONTEXT:  {context}", primary="", sqlstate="P0001", context=context)

    assert _handled(error)[0] == f"SQLSTATE P0001 {storage._SERVER_DETAILS_HIDDEN}"


def test_server_error_without_a_first_line_is_withheld():
    """Код ошибки есть, первой строки нет — драйвер отдаёт ошибку не так, как ждёт обработчик."""
    text_ = 'duplicate key\nDETAIL:  Key (email)=("s3cret") already exists.'
    error = _driver_error(text_, primary=None, sqlstate="23505")

    original, wrapped = _handled(error)

    assert original == storage._DRIVER_TEXT_WITHHELD
    assert "s3cret" not in wrapped


def test_error_that_could_not_be_checked_is_withheld():
    """Обработчик, упавший на разборе ошибки, не пропускает её текст как есть."""

    class Unreadable(Exception):
        @property
        def diag(self):
            raise RuntimeError("the driver changed under us")

    error = Unreadable('duplicate key\nDETAIL:  Key (email)=("s3cret") already exists.')

    original, wrapped = _handled(error)

    assert original == storage._DRIVER_TEXT_WITHHELD
    assert wrapped.endswith(f".Unreadable) {storage._DRIVER_TEXT_WITHHELD}")
    assert "s3cret" not in original + wrapped


# ─── Страж: движок создаёт только фабрика ───────────────────────────────────

_ENGINE_FACTORIES = {
    "create_engine",
    "create_async_engine",
    "create_mock_engine",
    "engine_from_config",
    "async_engine_from_config",
}
_GUARDED_FACTORY = ("src/storage.py", "create_engine_hiding_statement_data")


def _engine_factory_uses(root: Path) -> list[str]:
    """Где в коде названа фабрика движка SQLAlchemy — кроме строк импорта."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)

        def visit(node: ast.AST, function: str | None) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias) and node.asname and node.name in _ENGINE_FACTORIES:
                # `from sqlalchemy import create_engine as make` — дальше по имени не найти.
                found.append(f"{relative}:{node.lineno} import {node.name} as {node.asname}")
            if name in _ENGINE_FACTORIES and (relative, function) != _GUARDED_FACTORY:
                found.append(f"{relative}:{node.lineno} {name}")
            for child in ast.iter_child_nodes(node):
                visit(child, function)

        visit(tree, None)
    return found


def test_only_the_guarded_factory_creates_engines():
    """Новый `create_engine` в `src/` или `migrations/` вернул бы данные запроса в текст ошибки.

    Это растяжка на имени, а не доказательство: страж видит имя фабрики в коде
    этих двух каталогов. Он не видит `scripts/` (на сервер не выкладывается;
    движки там — SQLite в памяти и разовый перенос базы 2026 года) и не видит
    соединение, собранное мимо имени: `getattr`, импорт под другим именем из
    третьего модуля, `psycopg.connect`, адрес базы строкой в `pandas.read_sql`.
    """
    uses = _engine_factory_uses(REPO / "src") + _engine_factory_uses(REPO / "migrations")

    assert uses == [], (
        "движок SQLAlchemy создаётся мимо storage.create_engine_hiding_statement_data: "
        + "; ".join(uses)
    )


def test_guard_sees_an_engine_created_elsewhere(tmp_path, monkeypatch):
    """Контроль стража: чужой `create_engine` он находит во всех записях."""
    module = tmp_path / "src" / "elsewhere.py"
    module.parent.mkdir()
    module.write_text(
        "import sqlalchemy\n"
        "from sqlalchemy import create_engine as make, engine_from_config\n"
        "def a():\n    return sqlalchemy.create_engine('sqlite://')\n"
        "def b():\n    return make('sqlite://')\n"
        "factory = engine_from_config\n"
        "def create_engine_hiding_statement_data():\n    return sqlalchemy.create_engine('sqlite://')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)

    uses = _engine_factory_uses(tmp_path / "src")

    assert uses == [
        "src/elsewhere.py:2 import create_engine as make",
        "src/elsewhere.py:4 create_engine",
        "src/elsewhere.py:7 engine_from_config",
        # Одноимённая функция в другом модуле — не та фабрика.
        "src/elsewhere.py:9 create_engine",
    ]


# Python внутри шага workflow печатает в публичный журнал и ходит в боевую базу.
# Три шага строят движок сами; их запросы читают прогоны, размер базы и каталог,
# людей в них нет (прочитано 2026-10-11). Новому такому шагу — фабрика.
#
# deploy.yml — четвёртый, и фабрику взять не может: ревизию базы он читает до
# того, как релиз попал на сервер, а `src` в живом каталоге — прошлого релиза и
# фабрики может не знать. Запрос один, без параметров: `SELECT version_num FROM
# alembic_version`. Миграции тот же workflow исполняет уже через `migrations/env.py`
# своего релиза, то есть через фабрику.
_WORKFLOWS_WITH_THEIR_OWN_ENGINE = {
    ".github/workflows/deploy.yml",
    ".github/workflows/preflight-pharmonline-public-api-full.yml",
    ".github/workflows/recover-pharmonline-decodo-public-api.yml",
    ".github/workflows/recover-pharmonline-public-api.yml",
}


def test_no_new_workflow_builds_its_own_engine():
    call = re.compile(r"\b(?:%s)\s*\(" % "|".join(sorted(_ENGINE_FACTORIES)))
    found = {
        path.relative_to(REPO).as_posix()
        for path in (REPO / ".github" / "workflows").glob("*.y*ml")
        if call.search(path.read_text(encoding="utf-8"))
    }

    # Контроль: поиск находит то, что в списке есть сегодня (список не длиннее дела).
    assert found, "ни одного workflow с движком не найдено — поиск сломан или список устарел"
    # Исключение выдано файлу за то, что его запросы не о людях.
    for relative in sorted(found & _WORKFLOWS_WITH_THEIR_OWN_ENGINE):
        people = re.findall(r"tenant_users|recipients", (REPO / relative).read_text("utf-8"))
        assert not people, f"{relative} строит движок сам и читает таблицу с людьми"
    assert found <= _WORKFLOWS_WITH_THEIR_OWN_ENGINE, (
        "workflow строит движок мимо storage.create_engine_hiding_statement_data: "
        + ", ".join(sorted(found - _WORKFLOWS_WITH_THEIR_OWN_ENGINE))
    )


# ─── Приложение под uvicorn: конверт Sentry и журнал ────────────────────────


@pytest.fixture(scope="module")
def probes(tmp_path_factory) -> dict[str, SimpleNamespace]:
    """Проба в трёх режимах: движок с защитой, без неё (контроль) и пара «движок + хуки Sentry»."""
    url = _database_url()
    results = {}
    for mode in ("guarded", "unguarded", "as_shipped"):
        report = tmp_path_factory.mktemp(f"probe_{mode}") / "report.json"
        done = subprocess.run(
            [sys.executable, str(REPO / "tests" / "failed_statement_probe.py"), mode, str(report)],
            # Запуск по пути кладёт в sys.path каталог tests/, а не корень: без
            # PYTHONPATH проба взяла бы `src` из установленного пакета, если он есть.
            env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(REPO)},
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=180,
        )
        journal = done.stdout + done.stderr
        assert done.returncode == 0, f"проба ({mode}) упала:\n{journal[-4000:]}"
        found = json.loads(report.read_text(encoding="utf-8"))
        results[mode] = SimpleNamespace(
            cases=found["cases"], sent=found["sent"], hooks=found["hooks"], journal=journal
        )
    return results


def _error_texts(case: dict) -> list[str]:
    """Тексты исключений из событий об ошибке: поле `exception.values[].value`."""
    texts = []
    for item in case["items"]:
        if item["type"] != "event":
            continue
        for value in (json.loads(item["raw"]).get("exception") or {}).get("values") or []:
            texts.append(f"{value.get('type')}: {value.get('value')}")
    return texts


def test_probe_sees_statement_data_when_the_engine_is_unguarded(probes):
    """Контроль: без защиты движка данные запроса есть и в событии, и в журнале."""
    cases, journal = probes["unguarded"].cases, probes["unguarded"].journal

    token = "\n".join(_error_texts(cases["token"]))
    assert "[parameters: " in token
    assert any(form in token for form in cases["token"]["secrets"].values())
    assert any(form in journal for form in cases["token"]["secrets"].values())

    address = "\n".join(_error_texts(cases["address"]))
    assert cases["address"]["secrets"]["address"] in address
    assert cases["address"]["secrets"]["address"] in journal

    row = "\n".join(_error_texts(cases["row"]))
    assert "Failing row contains" in row
    assert cases["row"]["secrets"]["address"] in row
    assert _BCRYPT_SHAPED.search(row)
    assert cases["row"]["secrets"]["address"] in journal
    assert _BCRYPT_SHAPED.search(journal)


@pytest.mark.parametrize(
    ("name", "error", "cause"),
    [
        ("token", "OperationalError", "LockNotAvailable"),
        ("address", "OperationalError", "LockNotAvailable"),
        ("row", "IntegrityError", 'check constraint "probe_invited_has_no_password"'),
    ],
)
def test_failure_reaches_sentry_as_an_error_of_the_statement(probes, name, error, cause):
    """Сбой настоящий и до Sentry дошёл: ответ 500, событие с ошибкой запроса к пользователям.

    Без этого «в конверте чисто» могло бы значить, что ничего не отправлено.
    """
    case = probes["guarded"].cases[name]

    assert case["status"] == 500
    reported = [text_ for text_ in _error_texts(case) if text_.startswith(f"{error}: ")]
    assert reported, _error_texts(case)
    for text_ in reported:
        assert cause in text_
        assert "tenant_users" in text_
        assert _HIDDEN_BY_SQLALCHEMY in text_


def test_sentry_envelopes_carry_no_statement_data(probes):
    # Всё отправленное за прогон, а не по случаям: трасса запроса уходит позже события.
    sent = "\n".join(item["raw"] for item in probes["guarded"].sent)

    # Контроль: в собранном есть и события об ошибках, и трассы запросов.
    kinds = [item["type"] for item in probes["guarded"].sent]
    assert kinds.count("event") >= 3 and kinds.count("transaction") >= 3, kinds
    for name, case in probes["guarded"].cases.items():
        for label, secret in case["secrets"].items():
            assert secret not in sent, f"{name}: {label} ушёл в Sentry"
    assert not _BCRYPT_SHAPED.search(sent), "хеш пароля ушёл в Sentry"
    assert "[parameters: " not in sent
    assert "Failing row contains" not in sent


def test_engine_and_sentry_hooks_together_send_the_error_without_its_data(probes):
    """Как на сервере: защищённый движок и хуки `before_send` из `src/observability.py`.

    Остальные режимы проверяют слои порознь; здесь — что вместе они не мешают
    друг другу: событие об ошибке дошло, данных запроса в нём нет.
    """
    shipped = probes["as_shipped"]
    sent = "\n".join(item["raw"] for item in shipped.sent)

    # Контроль: хуки здесь стояли, а в двух других режимах — нет.
    assert shipped.hooks
    assert not probes["guarded"].hooks and not probes["unguarded"].hooks

    assert [case["status"] for case in shipped.cases.values()] == [500, 500, 500]
    assert [item["type"] for item in shipped.sent].count("event") >= 3
    assert sent.count(_HIDDEN_BY_SQLALCHEMY) >= 3
    for name, case in shipped.cases.items():
        for label, secret in case["secrets"].items():
            assert secret not in sent, f"{name}: {label} ушёл в Sentry"
    assert not _BCRYPT_SHAPED.search(sent), "хеш пароля ушёл в Sentry"


def test_journal_carries_no_statement_data(probes):
    guarded = probes["guarded"]

    # Три сбоя — три трассировки uvicorn: журнал об ошибках действительно писал.
    assert guarded.journal.count("Exception in ASGI application") >= 3
    assert guarded.journal.count(_HIDDEN_BY_SQLALCHEMY) >= 3
    for name, case in guarded.cases.items():
        for label, secret in case["secrets"].items():
            assert secret not in guarded.journal, f"{name}: {label} попал в журнал"
    assert not _BCRYPT_SHAPED.search(guarded.journal), "хеш пароля попал в журнал"
    assert "[parameters: " not in guarded.journal
    assert "Failing row contains" not in guarded.journal


# ─── CLI: трассировка упавшей команды ───────────────────────────────────────


def test_cli_traceback_carries_no_address(schema, outsider, user, tmp_path):
    """Команда упала на запросе по адресу — в её выводе адреса нет.

    Вывод команды — журнал: на сервере его забирает journald, в workflow —
    публичный журнал шага.
    """
    with _locked(outsider, "ACCESS EXCLUSIVE"):
        done = subprocess.run(
            [sys.executable, "-m", "src.main", "tenant", "issue-token", ADDRESS],
            env={
                **os.environ,
                "DATABASE_URL": _application_url(schema),
                "PYTHONPATH": str(REPO),
            },
            cwd=tmp_path,  # команда пишет logs/app.jsonl в текущий каталог
            capture_output=True,
            text=True,
            timeout=120,
        )
    output = done.stdout + done.stderr

    # Контроль: упал именно запрос к пользователям, и трассировка напечатана.
    assert done.returncode != 0
    assert "LockNotAvailable" in output
    assert "tenant_users" in output
    assert _HIDDEN_BY_SQLALCHEMY in output
    assert ADDRESS not in output
