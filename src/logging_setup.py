"""Structured logging setup — JSON to stdout (journald) + optional file with rotation.

Production setup uses systemd journal (StandardOutput=journal in unit files), so
JSON logs go to journald which can be shipped to Better Stack / Loki / ELK.

Optionally write to a rotating file too (LOG_FILE env). Useful for local dev or
fallback if journald isn't available.

Usage (call once at process start):
    from src.logging_setup import configure_logging
    configure_logging(service="api")
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog


def configure_logging(service: str = "app") -> None:
    """Configure structlog → either JSON (production) or pretty (dev).

    Format is controlled by LOG_FORMAT env: "json" (default) or "console".
    Optional file rotation: LOG_FILE=/path/to/file.jsonl + LOG_FILE_MAX_MB=50.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = os.environ.get("LOG_FORMAT", "json").lower()
    log_file = os.environ.get("LOG_FILE")  # e.g. /var/log/pharmacy-monitor/app.jsonl

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Always include service tag for filtering across CLI / API / scraper
        _add_service_tag(service),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Stdlib logging → forward to structlog so 3rd-party libs get same format
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Optional rotating file handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.environ.get("LOG_FILE_MAX_MB", "50")) * 1024 * 1024
        backups = int(os.environ.get("LOG_FILE_BACKUPS", "5"))
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(file_handler)


def _add_service_tag(service: str):
    """Processor that injects service=... into every log event."""

    def add(_, __, event_dict):
        event_dict.setdefault("service", service)
        return event_dict

    return add
