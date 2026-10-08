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
сериализует, а проверка, добавленная одному, второму не достаётся (так и было:
сверку соединения замка получил только помощник в `matcher`). `src.matcher`
отдаёт эти же функции под прежними именами.

На SQLite замка нет: функции ничего не делают.
"""

from __future__ import annotations

import structlog
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.orm import Session

log = structlog.get_logger()

MATCH_MUTATION_ADVISORY_LOCK_KEY = "pharmacy_monitor_matcher"
# Session.info: соединение, на котором сессия держит сессионный замок (_HeldLock).
_LOCK_CONNECTION_INFO_KEY = "match_mutation_lock_connection"
# SQLSTATE lock_not_available: ожидание замка оборвано по lock_timeout.
_LOCK_NOT_AVAILABLE = "55P03"


class _HeldLock:
    __slots__ = ("connection", "raw", "previous_bind")

    def __init__(self, connection: Connection, raw, previous_bind) -> None:
        self.connection = connection
        # Соединение драйвера под ним. После обрыва или Ctrl-C посреди запроса
        # SQLAlchemy соединение вне пула не закрывает, а только отпускает, и при
        # следующем запросе молча берёт вместо него другое — из пула.
        self.raw = raw
        self.previous_bind = previous_bind

    def intact(self) -> bool:
        """Сессия всё ещё ходит через то самое соединение, на котором лежит замок."""
        try:
            return self.connection.connection.dbapi_connection is self.raw
        except InvalidRequestError:
            # Соединение потеряно, а транзакция на нём ещё не откачена: SQLAlchemy
            # отказывается его выдавать («can't reconnect until … rolled back»).
            return False


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def acquire_match_mutation_xact_lock(session: Session) -> None:
    """Serialize one transaction with every canonical topology mutation.

    Брать до первого чтения того, что собираешься менять: сессии проекта не
    сбрасывают объекты после commit, и прочитанное до ожидания замка после
    ожидания остаётся прежним — а этап за это время менял именно эти строки.
    """
    if _is_postgres(session):
        _require_the_lock_connection(session)
        _take_xact_lock(session)


def _require_the_lock_connection(session: Session) -> None:
    """Сессия, державшая сессионный замок, обязана быть на его соединении."""
    held = session.info.get(_LOCK_CONNECTION_INFO_KEY)
    if held is not None and not held.intact():
        # После обрыва или Ctrl-C и rollback сессия идёт уже через пул: без
        # сессионного замка либо в очередь за собственным брошенным соединением.
        raise RuntimeError(
            "the session is no longer on the connection that holds the match mutation lock"
        )


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
    ожидание блокировок снова такое, как задано серверу, роли или параметрам
    подключения (сессионный ``SET lock_timeout`` до конца транзакции не
    действует — в проекте его никто не ставит): запись в строку, занятую сбором,
    ждёт сколько нужно, а не падает через ``seconds``. Ноль секунд — не «ждать
    без предела», а отказ сразу.
    """
    if not _is_postgres(session):
        return True
    _require_the_lock_connection(session)
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
    """Session-level lock for multi-transaction operations and rollback.

    Сессионный замок принадлежит соединению с базой, а сессия, привязанная к
    движку, после каждого commit возвращает соединение в пул и на следующий
    запрос берёт его заново. Пул вправе отдать другое: соединение старше
    `pool_recycle` он закрывает и открывает новое. Замок тогда пропадает посреди
    операции, а снятие уходит в соединение, которое его не держит. На проде так
    закончились 12 этапов сопоставления из 39 с 3 сентября по 7 октября 2026.

    Поэтому замок берётся на отдельном соединении, которое в пул не
    возвращается, и до снятия замка сессия работает только через него. Держать
    замок на одном соединении, а писать через другое нельзя: шаги операции
    берут транзакционный замок с тем же ключом
    (`acquire_match_mutation_xact_lock`) и встали бы в очередь за собственным
    процессом.

    Перевести сессию на другое соединение можно только между транзакциями,
    поэтому открытая транзакция завершается через `session.commit()` — как её
    завершил бы первый же commit самой операции — ещё до попытки взять замок,
    то есть и тогда, когда замок занят и функция вернёт False. Транзакцию,
    оборванную ошибкой SQL, сервер при этом откатывает.

    Пока замок держится, `session.get_bind()` — соединение, а не движок: код,
    которому нужен движок (`run_lock.try_shared_scrape_read_lock`), под замком
    не вызывать. Если соединение замка потеряно (обрыв, Ctrl-C посреди
    запроса), запросы сессии падают, а после rollback SQLAlchemy переподключил
    бы её через пул, уже мимо замка — поэтому `acquire_match_mutation_xact_lock`
    тогда отказывает. Шаг, который транзакционный замок не берёт, этой проверки
    не проходит: откатывать и продолжать под замком нельзя.

    Сессию, которую вызывающий сам привязал к соединению, функция не трогает:
    замок ложится на это соединение.
    """
    if not _is_postgres(session):
        return True
    fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
    statement = text(f"SELECT {fn}(hashtext(:key))")
    params = {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY}
    if _LOCK_CONNECTION_INFO_KEY in session.info:
        raise RuntimeError("this session already holds the match mutation lock")
    bind = session.get_bind()
    if isinstance(bind, Connection):
        value = session.scalar(statement, params)
        return True if wait else bool(value)

    if session.in_transaction():
        session.commit()
    connection = bind.connect()
    raw = None
    try:
        # Вне пула: закрытие такого соединения — конец сеанса на сервере, и
        # замок уходит вместе с ним, даже если явное снятие не удалось.
        connection.detach()
        raw = connection.connection.dbapi_connection
        value = connection.scalar(statement, params)
        # Сессионный замок переживает COMMIT; транзакцию самого запроса
        # закрываем, чтобы сессия начала на этом соединении свою.
        connection.commit()
    except BaseException:
        _close_lock_connection(connection, raw)
        raise
    if not wait and not value:
        _close_lock_connection(connection, raw)
        return False
    session.info[_LOCK_CONNECTION_INFO_KEY] = _HeldLock(connection, raw, session.bind)
    session.bind = connection
    return True


def release_match_mutation_lock(session: Session) -> None:
    """Снять замок, взятый `acquire_match_mutation_lock`.

    Незавершённую транзакцию сессии функция не фиксирует и не откатывает:
    транзакция доживает на выделенном соединении, и оно закрывается вместе с
    ней. Следующая транзакция сессии снова идёт через пул.
    """
    if not _is_postgres(session):
        return
    statement = text("SELECT pg_advisory_unlock(hashtext(:key))")
    params = {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY}
    held = session.info.pop(_LOCK_CONNECTION_INFO_KEY, None)
    if held is None:
        if not session.scalar(statement, params):
            log.warning("matcher_lock_not_held_at_release")
        return
    try:
        if not held.connection.scalar(statement, params):
            log.warning("matcher_lock_not_held_at_release")
    finally:
        session.bind = held.previous_bind
        _close_after_session_transaction(session, held)


def _close_after_session_transaction(session: Session, held: _HeldLock) -> None:
    """Закрыть выделенное соединение, когда сессия закончит на нём транзакцию."""
    if not session.in_transaction():
        _close_lock_connection(held.connection, held.raw)
        return

    def close(_session: Session, transaction) -> None:
        # Точки сохранения (вложенные транзакции) соединение не освобождают.
        if transaction.parent is None and not held.connection.closed:
            _close_lock_connection(held.connection, held.raw)

    event.listen(session, "after_transaction_end", close)


def _close_lock_connection(connection: Connection, raw) -> None:
    try:
        connection.close()
    finally:
        if raw is not None:
            raw.close()
