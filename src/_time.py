"""Shared time helper.

`datetime.utcnow()` deprecated с Python 3.12+. Используем
`datetime.now(timezone.utc).replace(tzinfo=None)` чтобы сохранить
обратную совместимость с naive-DateTime столбцами в SQLAlchemy.

Использование:
    from src._time import utcnow
    Run(started_at=utcnow())
    column = Mapped[datetime] = mapped_column(DateTime, default=utcnow)
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Replacement for the deprecated datetime.utcnow().

    Возвращает naive UTC datetime (tzinfo=None) — совместим с
    существующими DateTime столбцами в schema.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
