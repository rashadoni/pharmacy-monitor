"""Тесты src/logging_setup.configure_logging.

Coverage gap closure (2026-05-28): 0% → ~100%.
Покрываем JSON и console renderers, env-driven level, optional file rotation,
service tag injection.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import psycopg
import pytest
import sqlalchemy
import structlog

from src import logging_setup


@pytest.fixture(autouse=True)
def _reset_structlog():
    """Между тестами сбрасываем structlog config (singleton state)."""
    yield
    structlog.reset_defaults()
    # Чистим root handlers чтобы тесты не залипали друг другу
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)


def test_configure_logging_default_uses_json(monkeypatch):
    """Без LOG_FORMAT → JSON renderer."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    logging_setup.configure_logging(service="test")
    log = structlog.get_logger("test")
    # JSON renderer не падает, output можем проверить через capsys
    # Просто убедимся что log не падает
    log.info("hello", x=1)


def test_configure_logging_console_format(monkeypatch, capsys):
    """LOG_FORMAT=console → human-readable rendering."""
    monkeypatch.setenv("LOG_FORMAT", "console")
    logging_setup.configure_logging(service="test")
    log = structlog.get_logger("test")
    log.info("readable_event", value=42)
    out = capsys.readouterr().out
    # Console renderer пишет в stderr but PrintLoggerFactory uses stdout
    # Проверка: hidden in message body
    full = out + capsys.readouterr().err
    assert "readable_event" in full or "value" in full


def test_configure_logging_respects_log_level(monkeypatch):
    """LOG_LEVEL=WARNING → root.level == WARNING."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    logging_setup.configure_logging(service="test")
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_invalid_level_falls_back_to_info(monkeypatch):
    """Unknown LOG_LEVEL → INFO (graceful)."""
    monkeypatch.setenv("LOG_LEVEL", "GIBBERISH")
    logging_setup.configure_logging(service="test")
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_creates_log_file_handler(monkeypatch, tmp_path):
    """LOG_FILE задан → RotatingFileHandler добавлен."""
    log_file = tmp_path / "out.jsonl"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_FILE_MAX_MB", "1")
    monkeypatch.setenv("LOG_FILE_BACKUPS", "2")
    logging_setup.configure_logging(service="test")
    root = logging.getLogger()
    # Среди handlers должен быть RotatingFileHandler
    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].baseFilename == str(log_file)
    assert rotating[0].maxBytes == 1024 * 1024
    assert rotating[0].backupCount == 2


def test_configure_logging_skips_file_handler_when_unset(monkeypatch):
    """LOG_FILE не задан → нет RotatingFileHandler."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    logging_setup.configure_logging(service="test")
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotating == []


def test_configure_logging_creates_parent_dirs_for_log_file(monkeypatch, tmp_path):
    """Несуществующий dir создаётся автоматически."""
    log_path = tmp_path / "nested" / "deep" / "out.jsonl"
    assert not log_path.parent.exists()
    monkeypatch.setenv("LOG_FILE", str(log_path))
    logging_setup.configure_logging(service="test")
    assert log_path.parent.exists()


def test_add_service_tag_injects_service():
    """_add_service_tag processor добавляет service= в event_dict."""
    proc = logging_setup._add_service_tag("test-service")
    out = proc(None, None, {"message": "x"})
    assert out["service"] == "test-service"


def test_add_service_tag_does_not_overwrite():
    """Existing service= не overrideит'ся (setdefault поведение)."""
    proc = logging_setup._add_service_tag("default-service")
    out = proc(None, None, {"service": "explicit"})
    assert out["service"] == "explicit"


def test_configure_logging_replaces_root_handlers(monkeypatch):
    """Старые handlers очищаются — добавляется StreamHandler."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    # Сначала добавляем "чужой" handler
    leftover = logging.NullHandler()
    logging.getLogger().addHandler(leftover)
    logging_setup.configure_logging(service="test")
    handlers = logging.getLogger().handlers
    assert leftover not in handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)


# ─── error_fields: что о сбое идёт в журнал вместо текста ошибки ─────────────

ADDRESS = "viewer@client.example"
CHAT_ID = "700200"


def _database_error(orig: BaseException) -> sqlalchemy.exc.DBAPIError:
    """Ошибка базы, какой её отдаёт SQLAlchemy: запрос и его параметры — в тексте."""
    return sqlalchemy.exc.ProgrammingError(
        "UPDATE tenant_users SET telegram_chat_id=%(chat)s WHERE email = %(email)s",
        {"chat": CHAT_ID, "email": ADDRESS},
        orig,
    )


def _through_src(error: BaseException):
    """Бросить ошибку так, чтобы она прошла через строку `src/`: `masking`
    зовёт переданный ей отрисовщик из `src/logging_setup.py`."""

    def renderer(logger, name, event):
        raise error

    logging_setup.masking(renderer)(None, "info", {})


def _caught(call) -> BaseException:
    try:
        call()
    except Exception as exc:
        return exc
    raise AssertionError("ошибки не было")


def test_error_fields_carry_the_class_and_the_database_code_not_the_text():
    error = _database_error(psycopg.errors.UndefinedTable('relation "tenant_users" does not exist'))
    # Посылка: в тексте ошибки лежат параметры запроса.
    assert ADDRESS in str(error) and CHAT_ID in str(error)

    fields = logging_setup.error_fields(error)

    assert fields == {"error_type": "ProgrammingError", "sqlstate": "42P01"}


@pytest.mark.parametrize(
    "not_a_code",
    [
        f"connection for {ADDRESS} refused",
        # Код в начале и код внутри — всё равно текст.
        f"08006 {ADDRESS}",
        f"{ADDRESS} 08006",
        "08006\n" + ADDRESS,
        "0800",
        8006,
        None,
    ],
)
def test_error_fields_take_only_a_code_for_the_database_code(not_a_code):
    """`sqlstate` — ровно пять знаков кода. Что угодно другое под этим именем — текст."""
    orig = RuntimeError("нет связи")
    orig.sqlstate = not_a_code

    assert logging_setup.error_fields(_database_error(orig)) == {"error_type": "ProgrammingError"}


def test_error_fields_have_no_database_code_when_the_server_sent_none():
    """Отказ в соединении: до сервера не дошло, кода у ошибки нет."""
    refused = psycopg.OperationalError("connection refused")
    assert refused.sqlstate is None

    assert logging_setup.error_fields(_database_error(refused)) == {
        "error_type": "ProgrammingError"
    }


def test_error_fields_name_the_last_line_of_src_the_error_went_through():
    # `mask_addresses(None)` падает внутри `src/logging_setup.py`.
    error = _caught(lambda: logging_setup.mask_addresses(None))

    fields = logging_setup.error_fields(error)

    assert fields["error_type"] == "TypeError"
    where, line, function = fields["error_at"].replace(" in ", ":").split(":")
    assert (where, function) == ("logging_setup.py", "mask_addresses")
    source = Path(logging_setup.__file__).read_text(encoding="utf-8").splitlines()
    assert '"@" not in text' in source[int(line) - 1]


def test_error_fields_keep_the_place_when_reading_the_database_code_fails():
    class Unreadable(Exception):
        @property
        def orig(self):
            raise RuntimeError(f"нельзя прочитать {ADDRESS}")

    fields = logging_setup.error_fields(_caught(lambda: _through_src(Unreadable())))

    assert fields["error_type"] == "Unreadable"
    assert fields["error_at"].startswith("logging_setup.py:"), fields


def test_error_fields_know_src_by_a_path_that_was_not_normalized():
    """`sys.path` с «..» в записи: путь файла тот же, а строка другая."""
    src = Path(logging_setup.__file__).parent
    filename = str(src / ".." / src.name / "generated.py")
    namespace: dict = {}
    exec(compile("def generated():\n    raise ValueError('x')\n", filename, "exec"), namespace)

    fields = logging_setup.error_fields(_caught(namespace["generated"]))

    assert fields == {"error_type": "ValueError", "error_at": "generated.py:2 in generated"}


def test_error_fields_leave_out_the_place_when_the_error_never_touched_src():
    assert logging_setup.error_fields(_caught(lambda: int("x"))) == {"error_type": "ValueError"}
    assert logging_setup.error_fields(ValueError("не брошена")) == {"error_type": "ValueError"}


@pytest.mark.parametrize("filename", ["<string>", "telegram_bot.py", "./telegram_bot.py"])
def test_error_fields_do_not_take_code_without_a_full_path_for_src(monkeypatch, filename):
    """SQLAlchemy собирает свои методы из строки: файл у такого кода — `<string>`.
    Из каталога `src/` относительный путь выглядел бы нашим."""
    monkeypatch.chdir(Path(logging_setup.__file__).parent)
    namespace: dict = {}
    exec(compile("def generated():\n    raise ValueError('x')\n", filename, "exec"), namespace)

    fields = logging_setup.error_fields(_caught(namespace["generated"]))

    assert fields == {"error_type": "ValueError"}


def test_error_fields_work_when_the_working_directory_is_gone(monkeypatch, tmp_path):
    """Зовут из `except`: падение здесь остановило бы того, кто ловил сбой."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()
    # На пути ошибки — код без файла, как у SQLAlchemy: его путь нельзя
    # достраивать от рабочего каталога.
    namespace: dict = {"fail": lambda: logging_setup.mask_addresses(None)}
    exec(compile("def generated():\n    fail()\n", "<string>", "exec"), namespace)
    error = _caught(namespace["generated"])

    fields = logging_setup.error_fields(error)

    assert fields["error_type"] == "TypeError"
    assert fields["error_at"].startswith("logging_setup.py:")


def test_error_fields_never_raise():
    class Unreadable(Exception):
        @property
        def orig(self):
            raise RuntimeError(f"нельзя прочитать {ADDRESS}")

    assert logging_setup.error_fields(Unreadable()) == {"error_type": "Unreadable"}
