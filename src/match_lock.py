"""Замок сопоставления: один ключ и один способ его взять.

Всё, что меняет состав кластеров (`products.canonical_id`, строки `matches`),
работает под одним advisory-замком PostgreSQL. Этап сопоставления (конец сбора,
`rematch`) держит его сессионным — полторы — три с половиной минуты, через
несколько транзакций; ручные операции и эндпоинты берут транзакционный, до
commit или закрытия сессии. Эндпоинты ждут его недолго и отказывают
(`acquire_match_mutation_xact_lock_within`): дашборд обрывает запрос через
30 секунд, а этап идёт минуты.

Определение одно намеренно: два помощника с одинаковым ключом в разных модулях
расходятся молча — замок с другим ключом берётся без ошибки и ничего не
сериализует. `src.matcher` отдаёт эти же функции под прежними именами.

На SQLite замка нет: функции ничего не делают.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

MATCH_MUTATION_ADVISORY_LOCK_KEY = "pharmacy_monitor_matcher"
# SQLSTATE lock_not_available: ожидание замка оборвано по lock_timeout.
_LOCK_NOT_AVAILABLE = "55P03"


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def acquire_match_mutation_xact_lock(session: Session) -> None:
    """Serialize one transaction with every canonical topology mutation.

    Брать до первого чтения того, что собираешься менять: сессии проекта не
    сбрасывают объекты после commit, и прочитанное до ожидания замка после
    ожидания остаётся прежним — а этап за это время менял именно эти строки.
    """
    if _is_postgres(session):
        _take_xact_lock(session)


def _take_xact_lock(session: Session) -> None:
    session.scalar(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
    )


def acquire_match_mutation_xact_lock_within(session: Session, seconds: float) -> bool:
    """Тот же транзакционный замок, но ждать не дольше ``seconds``.

    False — замок занят дольше этого срока: идёт этап сопоставления. Транзакция
    сессии при этом откатана (ожидание оборвала база, продолжать её нельзя), и
    соединение вернулось в пул: отказавший запрос ничего не держит.

    Срок действует только на ожидание этого замка. Дальше в той же транзакции
    ожидание блокировок снова такое, как задано соединению: запись в строку,
    занятую сбором, ждёт сколько нужно, а не падает через ``seconds``.
    """
    if not _is_postgres(session):
        return True
    # SET не принимает параметров; число собрано здесь же, не из запроса.
    session.execute(text(f"SET LOCAL lock_timeout = {max(1, round(seconds * 1000))}"))
    try:
        _take_xact_lock(session)
    except OperationalError as error:
        busy = getattr(error.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE
        session.rollback()
        if not busy:
            raise
        return False
    session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return True


def acquire_match_mutation_lock(session: Session, *, wait: bool = True) -> bool:
    """Session-level lock for multi-transaction operations and rollback."""
    if not _is_postgres(session):
        return True
    fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
    value = session.scalar(
        text(f"SELECT {fn}(hashtext(:key))"),
        {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
    )
    return True if wait else bool(value)


def release_match_mutation_lock(session: Session) -> None:
    if _is_postgres(session):
        session.scalar(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
        )
