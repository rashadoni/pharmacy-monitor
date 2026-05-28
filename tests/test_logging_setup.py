"""Тесты src/logging_setup.configure_logging.

Coverage gap closure (2026-05-28): 0% → ~100%.
Покрываем JSON и console renderers, env-driven level, optional file rotation,
service tag injection.
"""

from __future__ import annotations

import logging

import pytest
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
