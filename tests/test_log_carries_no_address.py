"""Адрес получателя не попадает в журнал.

Еженедельный сбор pharmonline идёт из GitHub Actions и рассылает письма, а
журнал шага у публичного репозитория открыт. Получатели — администратор и
сотрудник клиента: адрес в журнале — персональные данные клиента в открытом
доступе.

Слоёв три, и каждый проверяется отдельно:

1. вызов журнала не передаёт адрес, а обработчик сбоя отправки — текст ошибки
   (smtplib кладёт адрес и в него). Проверяется дважды: чтением кода всего
   `src/` и прямым вызовом каждого почтового пути;
2. ошибка SMTP выходит из `notifier.send_email` уже без адреса в тексте — куда
   бы её ни записали дальше (`log.exception("run_failed")` печатает трассировку);
3. вывод журнала CLI вырезает адрес из готовой строки.

`structlog.testing.capture_logs()` не видит событий внутри `CliRunner`, поэтому
почтовые пути вызываются напрямую.
"""

from __future__ import annotations

import ast
import json
import logging
import random
import re
import smtplib
import time
import traceback
from pathlib import Path

import pytest
import structlog
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import (
    alerts,
    api,
    digest,
    error_reporting,
    logging_setup,
    main,
    notifications,
    notifier,
    storage,
    tenants,
)
from src._time import utcnow

SRC = Path(__file__).resolve().parent.parent / "src"
ADDRESS = "viewer@client.example"
SMTP_REPLY = f"5.1.1 <{ADDRESS}>: Recipient address rejected"


# ─── Слой 1а: чтение кода ────────────────────────────────────────────────────

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
# Имена и атрибуты, под которыми в проекте ходит адрес или идентификатор
# человека. Проверка видит имя, а не значение: адрес в переменной с другим
# именем, в `**fields` или в repr объекта она не поймает — на это третий слой.
_PERSON_ATTRS = {"email", "emails", "telegram_chat_id"}
_PERSON_NAMES = {
    "email",
    "emails",
    "recipients",
    "recipient",
    "to",
    "email_to",
    "only_email",
    "to_addrs",
    "chat_id",
}
_COUNTING = {"len", "bool"}
_SENDERS = {"send_email", "send_telegram_message"}
_EXCEPTION_SUMMARIES = {"type", "delivery_error_fields"}
# Обработчик всего сбора: в его `try` лежит весь пайплайн, письмо — одна из
# стадий, и трассировка сбоя сбора нужна целиком. От адреса её страхуют слои 2 и
# 3 — это проверяет `test_run_failure_output_carries_no_address`.
_WHOLE_RUN_FAILURE_EVENTS = {"run_failed"}
# Команды, которые печатают адрес по назначению: оператор запускает их сам и
# читает вывод в своём терминале. `click.echo` и `print` идут мимо маски журнала,
# поэтому из workflow эти команды не запускают — см. тест ниже.
_PRINTS_AN_ADDRESS_BY_DESIGN = {
    "tenant_add_user": "tenant add-user",
    "tenant_issue_token": "tenant issue-token",
    "recipient_add": "recipient add",
    "recipient_list": "recipient list",
    "recipient_remove": "recipient remove",
    "recipient_toggle": "recipient toggle",
    "recipient_update": "recipient update",
}

_HOW_TO_FIX_AN_ADDRESS = (
    "В журнал и в вывод команды уходит адрес или идентификатор человека. "
    "Пиши `user_id=user.id` или число (`len(...)`). Если переменная названа "
    "`to`/`email`, но адресом не является, — переименуй её."
)
_HOW_TO_FIX_EXCEPTION_TEXT = (
    "Обработчик сбоя отправки пишет в журнал текст ошибки, а smtplib кладёт в "
    "него адрес получателя. Пиши `**notifier.delivery_error_fields(exc)`."
)


def _called_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "log"
    )


def _is_print_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "print"
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "echo"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "click"
    )


def _calls_with_owner(node: ast.AST, owner: str | None = None):
    """Все вызовы и имя функции, внутри которой каждый стоит."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call):
            yield child, owner
        inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
        yield from _calls_with_owner(child, inner)


def _walk_outside(node: ast.AST, wrappers: set[str]):
    """Обход выражения без захода внутрь вызовов из `wrappers`."""
    stack = [node]
    while stack:
        current = stack.pop()
        if _called_name(current) in wrappers:
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _values(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _names_a_person(call: ast.Call) -> bool:
    return any(
        (isinstance(node, ast.Attribute) and node.attr in _PERSON_ATTRS)
        or (isinstance(node, ast.Name) and node.id in _PERSON_NAMES)
        for value in _values(call)
        for node in _walk_outside(value, _COUNTING)
    )


def address_leaks(source: str, *, by_design: frozenset[str] | None = None) -> list[str]:
    """Вызовы журнала, `print` и `click.echo`, которым передан адрес.

    Печать внутри команд из `by_design` не считается; журнал считается всегда.
    """
    allowed = frozenset(_PRINTS_AN_ADDRESS_BY_DESIGN) if by_design is None else by_design
    found = []
    for call, owner in _calls_with_owner(ast.parse(source)):
        printed = _is_print_call(call) and owner not in allowed
        if (_is_log_call(call) or printed) and _names_a_person(call):
            found.append(f"{call.lineno}: {ast.unparse(call)}")
    return sorted(set(found))


def exception_text_leaks(source: str) -> list[str]:
    """Обработчики сбоя отправки, которые пишут в журнал саму ошибку."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            _called_name(inner) in _SENDERS for stmt in node.body for inner in ast.walk(stmt)
        ):
            continue
        for handler in node.handlers:
            for call in filter(_is_log_call, ast.walk(handler)):
                event = call.args[0].value if call.args else None
                if event in _WHOLE_RUN_FAILURE_EVENTS:
                    continue
                # `log.exception` печатает трассировку с текстом ошибки сам.
                names_it = call.func.attr == "exception" or any(
                    isinstance(inner, ast.Name) and inner.id == handler.name
                    for value in _values(call)
                    for inner in _walk_outside(value, _EXCEPTION_SUMMARIES)
                )
                if names_it:
                    found.append(f"{call.lineno}: {ast.unparse(call)}")
    return sorted(set(found))


def _across_src(check) -> list[str]:
    return [
        f"{path.relative_to(SRC.parent)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in check(path.read_text(encoding="utf-8"))
    ]


def test_no_log_or_print_call_in_src_is_given_an_address():
    assert _across_src(address_leaks) == [], _HOW_TO_FIX_AN_ADDRESS


def test_no_send_failure_handler_in_src_logs_the_exception_text():
    assert _across_src(exception_text_leaks) == [], _HOW_TO_FIX_EXCEPTION_TEXT


def test_the_list_of_commands_that_print_an_address_is_exact():
    """Новая команда, печатающая адрес, — решение, а не случайность."""
    printing = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        printing |= {
            owner
            for call, owner in _calls_with_owner(tree)
            if _is_print_call(call) and _names_a_person(call)
        }
    assert printing == set(_PRINTS_AN_ADDRESS_BY_DESIGN)


def test_no_workflow_runs_a_command_that_prints_an_address():
    """Журнал шага Actions публичен, а `click.echo` маска не трогает."""
    commands = sorted(set(_PRINTS_AN_ADDRESS_BY_DESIGN.values()))
    pattern = re.compile(
        r"(?:pharmacy-monitor|src\.main)\s+(?:"
        + "|".join(r"\s+".join(map(re.escape, command.split())) for command in commands)
        + r")\b"
    )
    workflows = sorted((SRC.parent / ".github" / "workflows").glob("*.yml"))
    assert workflows
    found = [
        f"{path.name}:{number}"
        for path in workflows
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert found == []
    assert pattern.search("  .venv/bin/pharmacy-monitor recipient list")
    assert pattern.search("python -m src.main tenant   issue-token admin")


@pytest.mark.parametrize(
    "call",
    [
        'log.warning("email_batch_failed", user=user.email)',
        'log.info("smtp_send", host=host, recipients=recipients)',
        'log.info("telegram_bound", user=email, chat_id=chat_id)',
        'log.info("recipient_created", id=new_user.id, email=new_user.email)',
        'log.info("x", who=f"to {user.email}")',
        'log.info("x", to=", ".join(recipients))',
        'print(f"digest sent to {user.email}")',
        'click.echo(f"OK: {r.email}")',
    ],
)
def test_the_code_check_sees_an_address_in_a_log_call(call):
    assert len(address_leaks(call)) == 1


@pytest.mark.parametrize(
    "call",
    [
        'log.info("smtp_send", recipients=len(recipients))',
        'log.info("alert_dispatched", email=len(results["email"]), telegram=0)',
        'log.info("digest_sent", kind=kind, recipients=sent)',
        'log.warning("email_batch_failed", user_id=user.id, **fields)',
        'log.info("x", has_recipients=bool(recipients))',
        'log.info("pharmacy_seen", address=address, addresses=len(rows))',
        'click.echo(f"OK: {kind} digest sent to {sent} recipients")',
    ],
)
def test_the_code_check_lets_counts_and_ids_through(call):
    assert address_leaks(call) == []


def test_the_code_check_excuses_printing_only_inside_the_listed_commands():
    source = """
def recipient_list():
    click.echo(f"  {r.email}")
    log.info("recipients_listed", first=r.email)

def run_cmd():
    click.echo(f"  {r.email}")
"""
    assert [line.split(":")[0] for line in address_leaks(source)] == ["4", "7"]


_HANDLER = """
try:
    notifier.send_email(subject=subject, html_body=html, to=[user.email])
except Exception as exc:
    {body}
"""


@pytest.mark.parametrize(
    "body",
    [
        'log.warning("email_batch_failed", user_id=user.id, error=str(exc))',
        'log.warning("email_batch_failed", error=f"{exc}")',
        'log.exception("email_batch_failed", exc_info=exc)',
        'log.exception("email_batch_failed", user_id=user.id)',
        'log.warning("email_batch_failed", error=repr(exc)[:200])',
    ],
)
def test_the_code_check_sees_exception_text_in_a_send_failure_handler(body):
    assert len(exception_text_leaks(_HANDLER.format(body=body))) == 1


@pytest.mark.parametrize(
    "body",
    [
        'log.warning("email_batch_failed", user_id=user.id, error_type=type(exc).__name__)',
        'log.warning("email_batch_failed", **notifier.delivery_error_fields(exc))',
        'log.warning("email_batch_failed", user_id=user.id)',
    ],
)
def test_the_code_check_lets_the_error_class_through(body):
    assert exception_text_leaks(_HANDLER.format(body=body)) == []


def test_the_code_check_excuses_only_the_whole_run_handler():
    assert exception_text_leaks(_HANDLER.format(body='log.exception("run_failed", run_id=1)')) == []
    assert exception_text_leaks(_HANDLER.format(body='log.exception("report_failed")')) != []


def test_the_code_check_ignores_handlers_that_send_nothing():
    source = """
try:
    rows = session.scalars(query).all()
except Exception as exc:
    log.warning("lookup_failed", error=str(exc))
"""
    assert exception_text_leaks(source) == []


# ─── Слой 1б: каждый почтовый путь, вызванный напрямую ───────────────────────


def _refused(*args, **kwargs):
    """Отказ почтового сервера с адресом в тексте — как его пишет smtplib."""
    raise RuntimeError(f"{{'{ADDRESS}': (550, b'{SMTP_REPLY}')}}")


@pytest.fixture
def session(monkeypatch, db_session):
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    monkeypatch.setattr(notifier, "send_email", _refused)
    monkeypatch.setattr(notifier, "send_telegram_message", _refused)
    return db_session


@pytest.fixture
def user(session):
    tenant = tenants.get_or_create_default(session)
    row = storage.TenantUser(
        tenant_id=tenant.id,
        email=ADDRESS,
        role="admin",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="info",
        telegram_severity_min="info",
        telegram_chat_id="700100",
        daily_digest=False,
        weekly_digest=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _event(session, user, *, rule_type="site_silent") -> storage.AlertEvent:
    event = storage.AlertEvent(
        rule_type=rule_type,
        dedup_key=f"no-address-{rule_type}",
        severity="critical",
        title="aloe молчит",
        detail="Сайт не отвечал сутки",
        tenant_id=user.tenant_id,
        created_at=utcnow(),
    )
    session.add(event)
    session.commit()
    return event


def _assert_clean(logs: list[dict], *, events: set[str], user_id: int | None) -> None:
    assert "@" not in repr(logs), logs
    failures = [entry for entry in logs if entry["event"] in events]
    assert {entry["event"] for entry in failures} == events, logs
    for entry in failures:
        # Диагностика остаётся: кому не ушло и чем ответил сервер.
        if user_id is not None:
            assert entry["user_id"] == user_id
        assert entry["error_type"] == "RuntimeError"
        assert "error" not in entry and "user" not in entry


def test_single_event_dispatch_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        notifications.dispatch_event(session, event)
    _assert_clean(
        logs, events={"email_dispatch_failed", "telegram_dispatch_failed"}, user_id=user.id
    )


def test_run_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        counts = notifications.dispatch_events_batch(session, [event])
    assert counts == {"email": 0, "telegram": 0, "failed": 2, "undelivered": 1}
    _assert_clean(logs, events={"email_batch_failed", "telegram_batch_failed"}, user_id=user.id)


def test_admin_only_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        counts = notifications.mail_unstored_events_to_admins(session, [event], note="тик")
    assert counts == {"email": 0, "telegram": 0, "failed": 2}
    _assert_clean(logs, events={"email_batch_failed", "telegram_batch_failed"}, user_id=user.id)


def test_per_user_digest_failure_log_carries_no_address(session, user):
    _event(session, user)
    with capture_logs() as logs:
        sent = notifications.send_weekly_digest(session, tenant_id=user.tenant_id)
    assert sent == {"recipients": 1, "sent": 0, "failed": 1}
    _assert_clean(logs, events={"digest_email_failed"}, user_id=user.id)


def test_per_user_digest_dry_run_names_the_recipient_by_id(session, user):
    _event(session, user)
    with capture_logs() as logs:
        notifications.send_weekly_digest(session, tenant_id=user.tenant_id, dry_run=True)
    assert "@" not in repr(logs), logs
    dry_run = [entry for entry in logs if entry["event"] == "digest_dry_run"]
    assert [entry["user_id"] for entry in dry_run] == [user.id]


def test_telegram_binding_log_carries_no_address(session, user):
    with capture_logs() as logs:
        assert notifications.bind_telegram(session, "700200", ADDRESS) is True
    assert "@" not in repr(logs), logs
    assert logs == [{"event": "telegram_bound", "log_level": "info", "user_id": user.id}]


def test_legacy_daily_digest_failure_log_carries_no_address(session, user):
    _event(session, user)
    with capture_logs() as logs, pytest.raises(digest.DigestNotSent):
        digest.send_daily_digest(session)
    _assert_clean(logs, events={"digest_email_failed"}, user_id=None)


def test_legacy_alert_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        alerts._send_email_alert(event)
    _assert_clean(logs, events={"alert_email_failed"}, user_id=None)


def test_error_report_letter_failure_log_carries_no_address(session, monkeypatch, tmp_path):
    monkeypatch.setattr(error_reporting, "ERRORS_LOG", tmp_path / "errors.jsonl")
    monkeypatch.setattr(error_reporting, "_RECENT", {})
    with capture_logs() as logs:
        error_reporting.report_error(ValueError("сбор упал"), component="test")
    _assert_clean(logs, events={"error_email_failed"}, user_id=None)


def test_login_link_failure_log_carries_no_address(session, user):
    with capture_logs() as logs:
        assert api._send_login_link(session, ADDRESS) is True
    _assert_clean(logs, events={"login_link_email_failed"}, user_id=user.id)


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


# ─── Слой 2: ошибка SMTP выходит из send_email без адреса ────────────────────


def _smtp_that_fails_with(error: Exception, at: str = "send_message"):
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            if at == "starttls":
                raise error

        def login(self, *args):
            if at == "login":
                raise error

        def send_message(self, message):
            if at == "send_message":
                raise error

    return FakeSMTP


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "resend")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "Pharmacy Monitor")


def test_smtp_send_logs_how_many_recipients_not_who(smtp_env, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(RuntimeError(), at="never"))
    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS, "second@client.example"])
    assert "@" not in repr(logs), logs
    assert [entry["event"] for entry in logs] == ["smtp_send", "smtp_sent_ok"]
    assert logs[0]["recipients"] == 2


@pytest.mark.parametrize(
    ("error", "at", "text", "fields", "reply"),
    [
        (
            smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())}),
            "send_message",
            "SMTPRecipientsRefused (SMTP 550 5.1.1)",
            {"error_type": "SMTPRecipientsRefused", "smtp_code": 550, "smtp_status": "5.1.1"},
            "5.1.1 <<address>>: Recipient address rejected",
        ),
        (
            smtplib.SMTPDataError(554, f"5.7.1 message to {ADDRESS} refused".encode()),
            "send_message",
            "SMTPDataError (SMTP 554 5.7.1)",
            {"error_type": "SMTPDataError", "smtp_code": 554, "smtp_status": "5.7.1"},
            "5.7.1 message to <address> refused",
        ),
        (
            smtplib.SMTPSenderRefused(550, b"domain is not verified", "digest@sender.example"),
            "send_message",
            "SMTPSenderRefused (SMTP 550)",
            {"error_type": "SMTPSenderRefused", "smtp_code": 550},
            "domain is not verified",
        ),
        (
            smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid"),
            "login",
            "SMTPAuthenticationError (SMTP 535 5.7.8)",
            {"error_type": "SMTPAuthenticationError", "smtp_code": 535, "smtp_status": "5.7.8"},
            "5.7.8 Authentication credentials invalid",
        ),
        (
            # Расширенный код — только в начале ответа и только из трёх чисел.
            smtplib.SMTPDataError(451, b"4.7.0.1 is not a status; policy 5.7.1 applies"),
            "send_message",
            "SMTPDataError (SMTP 451)",
            {"error_type": "SMTPDataError", "smtp_code": 451},
            "4.7.0.1 is not a status; policy 5.7.1 applies",
        ),
        (
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
            "starttls",
            "SMTPServerDisconnected",
            {"error_type": "SMTPServerDisconnected"},
            "Connection unexpectedly closed",
        ),
    ],
)
def test_smtp_refusal_leaves_send_email_without_an_address(
    smtp_env, monkeypatch, error, at, text, fields, reply
):
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(error, at=at))

    with capture_logs() as logs, pytest.raises(notifier.EmailDeliveryError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    failure = raised.value
    # Всё, что из ошибки может попасть в журнал: текст, repr, трассировка.
    printed = str(failure) + repr(failure) + "".join(traceback.format_exception(failure))
    assert "@" not in printed, printed
    assert failure.__cause__ is None and failure.__context__ is None
    assert "@" not in repr(logs), logs

    # Диагностика: класс исходной ошибки и коды — в журнал, ответ сервера — оператору.
    assert str(failure) == text
    assert notifier.delivery_error_fields(failure) == fields
    assert failure.server_reply == reply


def test_delivery_error_fields_never_carry_the_error_text():
    raw = smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    assert notifier.delivery_error_fields(raw) == {"error_type": "SMTPRecipientsRefused"}
    assert notifier.delivery_error_fields(smtplib.SMTPDataError(554, SMTP_REPLY.encode())) == {
        "error_type": "SMTPDataError",
        "smtp_code": 554,
    }


def test_delivery_error_fields_keep_what_tells_network_failures_apart():
    assert notifier.delivery_error_fields(TimeoutError("timed out")) == {
        "error_type": "TimeoutError"
    }
    assert notifier.delivery_error_fields(OSError(101, "Network is unreachable")) == {
        "error_type": "OSError",
        "errno": 101,
    }
    assert notifier.delivery_error_fields(ConnectionRefusedError(111, "Connection refused")) == {
        "error_type": "ConnectionRefusedError",
        "errno": 111,
    }
    assert notifier.delivery_error_fields(KeyError("SMTP_USER")) == {"error_type": "KeyError"}


def test_run_failure_output_carries_no_address(cli_logging, db_session, monkeypatch):
    """Сбой сбора печатают двое: журнал (`run_failed` с трассировкой) и сам click."""
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)

    async def refused(*args, **kwargs):
        _refused()

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    monkeypatch.setattr(main, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main.watchlist, "categories_for_site", lambda session, site, only_category_id=None: ["cat"]
    )
    monkeypatch.setattr(main, "baselines_for_sites", lambda *args: {"aloe": None})
    monkeypatch.setattr(main, "scrape_all", refused)

    result = CliRunner().invoke(
        main.cli, ["run", "--site", "aloe", "--mode", "category", "--no-alerts", "--force"]
    )

    assert result.exit_code != 0
    assert "run_failed" in result.output and "Traceback" in result.output
    assert "Error: {'<address>': (550" in result.output
    assert "@" not in result.output, result.output


# ─── Слой 3: вывод журнала CLI ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        (f"user={ADDRESS}", "user=<address>"),
        (f"recipients=['{ADDRESS}', 'b@c.example']", "recipients=['<address>', '<address>']"),
        (
            f"{{'{ADDRESS}': (550, b'{SMTP_REPLY}')}}",
            "{'<address>': (550, b'5.1.1 <<address>>: Recipient address rejected')}",
        ),
        ("first.last+tag@mail.client-name.example", "<address>"),
        ("почта@пример.рф", "<address>"),
        # Так адрес выглядит в JSON-выводе: не-ASCII записано \uXXXX, перевод
        # строки — \n. Буква «n» от него в адрес не входит.
        (json.dumps({"user": "почта@пример.рф"}), '{"user": "<address>"}'),
        (json.dumps(f"отказ\n{ADDRESS}"), json.dumps("отказ\n<address>")),
    ],
)
def test_mask_replaces_every_address(text, masked):
    assert logging_setup.mask_addresses(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "run_finished products=9424 run_id=980 status=ok",
        "scrape_using_proxy proxy=http://***@gate.example.com:7000",
        "ssh pm@13.140.186.143",
        '{"@type": "Product", "@context": "https://schema.org"}',
        "admin@local",
        "",
    ],
)
def test_mask_leaves_what_is_not_an_address(text):
    assert logging_setup.mask_addresses(text) == text


def test_mask_stays_linear_on_a_long_line_without_spaces():
    # Маска стоит на пути каждой строки журнала сбора. Без ограничения длины
    # имени такая строка разбирается квадратично — десятки секунд вместо сотых.
    line = "payload=" + "a" * 20_000 + f" user={ADDRESS}"
    started = time.perf_counter()
    masked = logging_setup.mask_addresses(line)
    assert time.perf_counter() - started < 5
    assert masked.endswith("user=<address>")


def test_mask_keeps_a_json_line_valid():
    """В JSON-режиме маска правит готовую строку: одинокая `\\` её бы сломала."""
    pieces = ["\\", "u", "n", '"', "'", "a", "0", "f", "@", ".", "-", " ", "я", "\n", "x@y.zz"]
    pieces += ["\\u0041", "client.example"]
    rng = random.Random(7)
    for _ in range(20_000):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        masked = logging_setup.mask_addresses(json.dumps({"value": raw}))
        assert isinstance(json.loads(masked)["value"], str), raw


def test_masking_passes_through_what_a_renderer_did_not_turn_into_text():
    event = {"event": "x", "user": ADDRESS}
    assert logging_setup.masking(lambda *_: event)(None, "info", event) is event


def _configure_cli(monkeypatch, as_json: bool) -> None:
    if as_json:
        monkeypatch.setenv("PHARMACY_LOG_JSON", "1")
    else:
        monkeypatch.delenv("PHARMACY_LOG_JSON", raising=False)
    main._setup_logging("INFO")


def _configure_shared(monkeypatch, as_json: bool) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json" if as_json else "console")
    monkeypatch.delenv("LOG_FILE", raising=False)
    logging_setup.configure_logging(service="test")


@pytest.mark.parametrize("configure", [_configure_cli, _configure_shared])
@pytest.mark.parametrize("as_json", [False, True])
def test_log_output_never_prints_an_address(cli_logging, monkeypatch, capsys, configure, as_json):
    configure(monkeypatch, as_json)
    log = structlog.get_logger()

    # То, что слои 1 и 2 должны были не пропустить: поле с адресом, список,
    # объект с адресом в repr и трассировка с текстом ошибки smtplib.
    log.info("smtp_send", host="smtp.example.test", recipients=[ADDRESS])
    log.warning("email_batch_failed", user=ADDRESS, row={"email": ADDRESS})
    log.warning("digest_email_failed", user="почта@пример.рф", note=f"отказ\n{ADDRESS}")
    try:
        raise smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    except smtplib.SMTPException:
        log.exception("run_failed", run_id=980)

    output = capsys.readouterr().out
    lines = output.strip().splitlines()
    assert "@" not in output, output
    assert output.count(logging_setup.ADDRESS_MASK) >= 7
    assert "smtp_send" in lines[0] and "email_batch_failed" in output and "run_failed" in output
    assert "SMTPRecipientsRefused" in output
    if as_json:
        assert [json.loads(line)["event"] for line in lines] == [
            "smtp_send",
            "email_batch_failed",
            "digest_email_failed",
            "run_failed",
        ]
