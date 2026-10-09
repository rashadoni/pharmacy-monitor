"""Привязка Telegram-чата к аккаунту — по одноразовому коду из дашборда.

Чат привязывает тот, кто вошёл в аккаунт, а не тот, кто знает его адрес:

1. вошедший пользователь просит код в дашборде (`issue_code`);
2. отправляет его боту `/start <код>` (`bind_chat`).

До 2026-10-09 бот принимал `/start <адрес>` и записывал чат любому активному
пользователю с таким адресом. Адрес админа опубликован, так что посторонний
получал алерты клиента и затирал настоящую привязку.

Что держит правило:

- код живёт `CODE_TTL`, срабатывает один раз, в базе лежит только его SHA-256;
- у пользователя один действующий код: новый заменяет прежний;
- бот адрес не принимает и не ищет вовсе — по ответу нечего перебирать;
- привязанный аккаунт кода не получает и чужим чатом не перезаписывается:
  сначала «Отвязать» в дашборде или `/stop` из самого чата;
- чат привязан не больше чем к одному действующему аккаунту;
- после `MAX_FAILED_ATTEMPTS` неудач чат ждёт конца окна `ATTEMPT_WINDOW`, и
  код в это время не проверяется вовсе — и верный тоже.

Замков на PostgreSQL два, и берутся они в одном порядке: чат
(`_serialize_chat`), затем строка пользователя. Код читают, гасят и заменяют
только под замком строки его пользователя — своего замка у строки кода нет.

В журнал не идут ни код, ни чат, ни адрес: получатель — `user_id`. Сбой базы
выходит отсюда как `BindingStorageError` без текста запроса — в нём номер чата.
"""

from __future__ import annotations

import functools
import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Concatenate, ParamSpec, TypeVar

import structlog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src._time import utcnow
from src.storage import AuditLog, TelegramBindAttempt, TelegramBindCode, TenantUser

log = structlog.get_logger()

CODE_TTL = timedelta(minutes=10)
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = timedelta(minutes=15)
# 16 случайных байт — 22 знака из A-Z a-z 0-9 _ -: ровно тот алфавит, который
# Telegram пропускает в ссылке `t.me/<бот>?start=<код>` (не длиннее 64 знаков).
_CODE_BYTES = 16
_CODE_SHAPE = re.compile(r"[A-Za-z0-9_-]{22}")
# Что пишется в `audit_logs.resource` о привязке и отвязке из чата.
AUDIT_RESOURCE = "telegram-chat"

_NO_SYNC = {"synchronize_session": False}

_P = ParamSpec("_P")
_R = TypeVar("_R")


class AlreadyBound(Exception):
    """Аккаунт уже привязан к чату: код не выдаётся, пока привязку не сняли."""


class BindingStorageError(RuntimeError):
    """Сбой базы в привязке — без текста исходной ошибки.

    SQLAlchemy кладёт в текст ошибки параметры запроса, а среди них номер чата;
    обработчик бота пишет текст ошибки в журнал. Здесь остаётся класс ошибки,
    место и код PostgreSQL; то же пишет в журнал событие
    `telegram_binding_storage_failed`.
    """


class BindOutcome(Enum):
    BOUND = "bound"
    # Одна причина на всё: кода нет, он истёк или уже использован, аккаунт
    # выключен или уже привязан, чат занят. Отвечающий причин не различает.
    REFUSED = "refused"
    LOCKED = "locked"


@dataclass(frozen=True)
class IssuedCode:
    code: str
    expires_at: datetime


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def chat_lock_key(chat_id: str) -> str:
    return f"telegram-bind-chat:{chat_id}"


def _without_query_text(
    fn: Callable[Concatenate[Session, _P], _R],
) -> Callable[Concatenate[Session, _P], _R]:
    """Ошибка базы выходит из функции без параметров запроса в тексте."""

    @functools.wraps(fn)
    def guarded(session: Session, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return fn(session, *args, **kwargs)
        except SQLAlchemyError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            failure = BindingStorageError(
                f"{type(exc).__name__} in {fn.__name__}"
                + (f", sqlstate {sqlstate}" if sqlstate else "")
            )
            # Тот, кто поймает `failure`, увидит только её класс: что случилось
            # с базой, записать можно лишь здесь.
            log.error(
                "telegram_binding_storage_failed",
                where=fn.__name__,
                error_type=type(exc).__name__,
                sqlstate=sqlstate,
            )
        # Вне except: у новой ошибки нет ни причины, ни контекста, и трассировка
        # не покажет исходную — с параметрами запроса.
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise failure

    return guarded


def issue_code(session: Session, user_id: int) -> IssuedCode:
    """Выдать код привязки действующему пользователю. Прежний код гаснет.

    `AlreadyBound` — у аккаунта уже есть чат; `LookupError` — пользователя нет
    или он выключен.
    """
    now = utcnow()
    user = session.scalar(
        select(TenantUser)
        .where(TenantUser.id == user_id, TenantUser.is_active.is_(True))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise LookupError("no active user")
    if user.telegram_chat_id is not None:
        raise AlreadyBound
    session.execute(
        delete(TelegramBindCode).where(TelegramBindCode.user_id == user_id),
        execution_options=_NO_SYNC,
    )
    code = secrets.token_urlsafe(_CODE_BYTES)
    expires_at = now + CODE_TTL
    session.add(
        TelegramBindCode(
            user_id=user_id, code_hash=_hash(code), expires_at=expires_at, created_at=now
        )
    )
    session.commit()
    log.info("telegram_bind_code_issued", user_id=user_id)
    return IssuedCode(code=code, expires_at=expires_at)


@_without_query_text
def bind_chat(session: Session, chat_id: str, code: str) -> BindOutcome:
    """Привязать чат к аккаунту, которому выдан `code`."""
    chat_id = str(chat_id)
    now = utcnow()
    _serialize_chat(session, chat_id)
    if _failures_in_window(session, chat_id, now) >= MAX_FAILED_ATTEMPTS:
        session.commit()
        log.warning("telegram_bind_locked")
        return BindOutcome.LOCKED

    user_id = _redeem(session, chat_id, code, now)
    if user_id is None:
        _record_failure(session, chat_id, now)
        session.commit()
        log.warning("telegram_bind_refused")
        return BindOutcome.REFUSED

    session.execute(
        delete(TelegramBindAttempt).where(TelegramBindAttempt.chat_id == chat_id),
        execution_options=_NO_SYNC,
    )
    session.commit()
    log.info("telegram_bound", user_id=user_id)
    return BindOutcome.BOUND


@_without_query_text
def unbind_chat(session: Session, chat_id: str) -> bool:
    """Снять привязку с чата по просьбе самого чата (`/stop`).

    Чат, привязанный не тем кодом (чужая ссылка, группа по ошибке), иначе не
    освободить: «Отвязать» в дашборде есть только у хозяина аккаунта.
    """
    chat_id = str(chat_id)
    _serialize_chat(session, chat_id)
    owners = session.execute(
        select(TenantUser.id, TenantUser.tenant_id).where(TenantUser.telegram_chat_id == chat_id)
    ).all()
    if owners:
        session.execute(
            update(TenantUser)
            .where(TenantUser.telegram_chat_id == chat_id)
            .values(telegram_chat_id=None)
        )
        for user_id, tenant_id in owners:
            session.add(_audit(tenant_id, user_id, "UNBIND"))
    session.commit()
    for user_id, _ in owners:
        log.info("telegram_unbound", user_id=user_id)
    return bool(owners)


@_without_query_text
def user_for_chat(session: Session, chat_id: str) -> TenantUser | None:
    """Действующий пользователь, к которому привязан чат.

    Чат, записанный за двумя действующими аккаунтами (до такого доводит только
    ручная правка базы), не отвечает ни за одного.
    """
    owners = session.scalars(
        select(TenantUser)
        .where(TenantUser.telegram_chat_id == str(chat_id), TenantUser.is_active.is_(True))
        .order_by(TenantUser.id)
        .limit(2)
    ).all()
    return owners[0] if len(owners) == 1 else None


def _audit(tenant_id: int, user_id: int, action: str) -> AuditLog:
    """Запись идёт в той же транзакции, что и привязка: без следа её не бывает.

    `actor_user_id` — аккаунт, чей это чат. Кто из участников группы набрал
    команду, запись не знает.
    """
    return AuditLog(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        action=action,
        resource=AUDIT_RESOURCE,
        response_status=200,
    )


def _serialize_chat(session: Session, chat_id: str) -> None:
    """Запросы одного чата идут по одному — до конца транзакции.

    «Чат свободен» — обычное чтение: два кода из одного чата одновременно
    привязали бы его к двум аккаунтам. На SQLite пишет один, замка нет.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": chat_lock_key(chat_id)},
        )


def _redeem(session: Session, chat_id: str, code: str, now: datetime) -> int | None:
    """Погасить код и записать чат его пользователю. `None` — отказ.

    До отказа в базу пишется только одно: истёкший код стирается.
    """
    code = (code or "").strip()
    # Что не похоже на код, в базу не идёт даже хешем: человек по привычке
    # пришлёт адрес, а хеш адреса подбирается по словарю.
    if not _CODE_SHAPE.fullmatch(code):
        return None
    code_hash = _hash(code)
    # Чей это код, читается без замка — иначе не узнать, чью строку запирать.
    user_id = session.scalar(
        select(TelegramBindCode.user_id).where(TelegramBindCode.code_hash == code_hash)
    )
    if user_id is None:
        return None
    # Дальше — под замком строки пользователя, как и выдача кода: пока он наш,
    # код этого пользователя никто не погасит и не заменит.
    tenant_id = session.scalar(
        select(TenantUser.tenant_id).where(TenantUser.id == user_id).with_for_update()
    )
    # Прочитанному до замка верить нельзя: код читается заново.
    row = session.scalar(
        select(TelegramBindCode)
        .where(TelegramBindCode.code_hash == code_hash, TelegramBindCode.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if row is None or tenant_id is None:
        return None  # пока ждали замка, код погасили или заменили
    if row.expires_at <= now:
        session.delete(row)
        return None
    occupied = session.scalar(
        select(TenantUser.id)
        .where(TenantUser.telegram_chat_id == chat_id, TenantUser.is_active.is_(True))
        .limit(1)
    )
    if occupied is not None:
        return None
    # Условие в самом UPDATE: аккаунт, к которому чат уже привязан, не
    # перезаписывается, что бы ни прочитал этот процесс строкой выше.
    claimed = session.execute(
        update(TenantUser)
        .where(
            TenantUser.id == user_id,
            TenantUser.is_active.is_(True),
            TenantUser.telegram_chat_id.is_(None),
        )
        .values(telegram_chat_id=chat_id)
    ).rowcount
    if claimed != 1:
        return None
    # Чат мог остаться записан за выключенным аккаунтом — он сообщений не
    # получает. Снять, чтобы после его включения у чата не оказалось двух
    # хозяев.
    session.execute(
        update(TenantUser)
        .where(TenantUser.telegram_chat_id == chat_id, TenantUser.id != user_id)
        .values(telegram_chat_id=None)
    )
    session.add(_audit(tenant_id, user_id, "BIND"))
    session.delete(row)
    return user_id


def _failures_in_window(session: Session, chat_id: str, now: datetime) -> int:
    row = session.execute(
        select(TelegramBindAttempt.failures, TelegramBindAttempt.window_started_at).where(
            TelegramBindAttempt.chat_id == chat_id
        )
    ).first()
    if row is None or row.window_started_at + ATTEMPT_WINDOW <= now:
        return 0
    return row.failures


def _record_failure(session: Session, chat_id: str, now: datetime) -> None:
    # Истёкшие окна — всех чатов, и этого тоже: его счёт начнётся заново.
    session.execute(
        delete(TelegramBindAttempt).where(
            TelegramBindAttempt.window_started_at <= now - ATTEMPT_WINDOW
        ),
        execution_options=_NO_SYNC,
    )
    bumped = session.execute(
        update(TelegramBindAttempt)
        .where(TelegramBindAttempt.chat_id == chat_id)
        .values(failures=TelegramBindAttempt.failures + 1),
        execution_options=_NO_SYNC,
    ).rowcount
    if not bumped:
        session.execute(
            insert(TelegramBindAttempt).values(chat_id=chat_id, failures=1, window_started_at=now)
        )
