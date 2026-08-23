"""Cross-process coordination for mutable scrape and financial read state."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger()

SCRAPE_ADVISORY_LOCK_KEY = "pharmacy_monitor_scrape"


def _is_postgres_bind(bind) -> bool:
    return str(bind.url).startswith("postgresql")


@contextmanager
def try_exclusive_scrape_lock(session: Session) -> Iterator[bool]:
    """Hold a non-blocking exclusive scrape lock for an isolated maintenance job.

    The regular producer command keeps this same session-level advisory lock
    for its whole lifetime.  A maintenance command cannot use Click's command
    lifetime hook, so it owns a separate checked-out connection and releases
    the lock explicitly.  The lock connection commits immediately after each
    advisory-lock operation and never remains idle in a transaction.
    """
    bind = session.get_bind()
    if not _is_postgres_bind(bind):
        yield True
        return

    connection = bind.connect()
    acquired = False
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                {"key": SCRAPE_ADVISORY_LOCK_KEY},
            )
        )
        connection.commit()
        yield acquired
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": SCRAPE_ADVISORY_LOCK_KEY},
                )
                connection.commit()
            except Exception as exc:
                connection.rollback()
                log.warning("scrape_lock_release_failed", error=str(exc))
        connection.close()


@contextmanager
def try_shared_scrape_read_lock(session: Session) -> Iterator[bool]:
    """Hold a non-blocking shared lock while reading mutable product state.

    Scrape producers hold the matching exclusive session advisory lock.  A
    dedicated connection keeps this reader lock independent of the caller's
    ORM transaction and releases it immediately after the calculation.  The
    implicit lock transaction is committed at acquisition/release so the
    connection is never left ``idle in transaction``.
    """
    bind = session.get_bind()
    if not _is_postgres_bind(bind):
        yield True
        return

    connection = bind.connect()
    acquired = False
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock_shared(hashtext(:key))"),
                {"key": SCRAPE_ADVISORY_LOCK_KEY},
            )
        )
        connection.commit()
        yield acquired
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock_shared(hashtext(:key))"),
                    {"key": SCRAPE_ADVISORY_LOCK_KEY},
                )
                connection.commit()
            except Exception as exc:
                connection.rollback()
                log.warning("scrape_read_lock_release_failed", error=str(exc))
        connection.close()
