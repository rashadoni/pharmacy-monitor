"""Что система считает почтовым адресом получателя.

Правило одно на все входы — `recipient add`, `recipient update --new-email`,
`tenant add-user`, страница «Пользователи», запрос ссылки на вход — и на
отправку: запись, которая его не проходит, в заголовок `To:` не попадает
(`notifier`).

Зачем оно строже, чем «в строке есть @». Письмо по списку получателей одно на
всех, и запись, которая не адрес, срывает его всему списку: пакет `email` падает
на сборке заголовка, а нелатинские буквы не берёт сам почтовый сервер. Об этом
в журнале остаётся одна строка с классом ошибки.

Правило уже, чем RFC 5322, и это намеренно:

- только латиница. Нелатинская буква в адресе у нас — почти всегда раскладка
  клавиатуры (`ə`, `ı`, кириллическая «а» вместо латинской): письмо уйдёт в
  никуда или чужому человеку. А настоящий адрес с такими буквами доходит, только
  если их понимают и наш почтовый сервер, и сервер получателя: сервер, который
  SMTPUTF8 не объявляет, smtplib останавливает ещё до отправки — всему списку;
- имя ящика — буквы, цифры и знаки `. _ % + -`. Ровно такие адреса маска
  журнала (`logging_setup.mask_addresses`) вырезает целиком; от `o'brien@…` она
  оставила бы начало имени, имя в кавычках не увидела бы вовсе.
  `tests/test_email_address.py` сверяет правило с самой маской;
- домен — имя с точкой, зона из латинских букв: `admin@local` и адрес сервера в
  квадратных скобках письмо не получат. Часть домена в записи `xn--…` — те же
  нелатинские буквы, только закодированные: Chrome сам переводит в неё домен,
  набранный не в той раскладке, раньше, чем страница его прочтёт.

Чего правило не проверяет: что ящик существует. Опечатку в имени оно пропустит.
"""

from __future__ import annotations

import re

# Ширина колонок `recipients.email` и `tenant_users.email`.
MAX_LENGTH = 200
_MAX_MAILBOX_LENGTH = 64

_MAILBOX_RE = re.compile(r"[a-z0-9_%+\-]+(?:\.[a-z0-9_%+\-]+)*")
_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"(?:{_LABEL}\.)+[a-z]{{2,63}}")

# Код причины → что сказать человеку. Код уходит в журнал и в ответ API (по нему
# страница «Пользователи» берёт перевод), текст — оператору CLI. Ни в том, ни в
# другом нет ни самой записи, ни знака «@»: текст с ним
# `logging_setup.without_addresses` скрывает целиком.
PROBLEMS = {
    "empty": "адрес пуст",
    "whitespace": (
        "в записи пробел, перенос строки или невидимый знак — нужен один адрес, "
        "без имени и пояснений"
    ),
    "not_ascii": (
        "в адресе нелатинские буквы — проверьте раскладку клавиатуры и запишите адрес латиницей"
    ),
    "several": "в записи несколько адресов или имя рядом с адресом — добавляйте по одному адресу",
    "no_at": "в адресе нет знака «собака» между именем ящика и доменом",
    "several_at": "в адресе больше одного знака «собака»",
    "too_long": f"адрес длиннее {MAX_LENGTH} знаков",
    "mailbox": (
        "имя ящика (до «собаки») — латинские буквы, цифры и знаки . _ % + -, без точки "
        f"в начале, в конце и двух точек подряд, не длиннее {_MAX_MAILBOX_LENGTH} знаков"
    ),
    "domain": (
        "домен (после «собаки») — имя с точкой из латинских букв, цифр и дефисов, "
        "например example.com"
    ),
}


class InvalidEmailAddress(ValueError):
    """Запись не адрес. Текст — причина словами, без самой записи.

    `problem` — код причины из `PROBLEMS`.
    """

    def __init__(self, problem: str):
        self.problem = problem
        super().__init__(f"Адрес не принят: {PROBLEMS[problem]}.")


def address_problem(value: object) -> str | None:
    """Почему запись не адрес — код из `PROBLEMS`; `None`, если адрес.

    Регистр и пробелы по краям не считаются: их снимает `normalize_address`.
    """
    if not isinstance(value, str):
        return "empty"
    address = value.strip().lower()
    if not address:
        return "empty"
    # Длина — первой: дальше запись читается по знаку, а запрос ссылки на вход
    # принимает строку любой длины от кого угодно.
    if len(address) > MAX_LENGTH:
        return "too_long"
    if any(char.isspace() or not char.isprintable() for char in address):
        return "whitespace"
    if not address.isascii():
        return "not_ascii"
    if any(char in address for char in ',;<>"()'):
        return "several"
    if "@" not in address:
        return "no_at"
    if address.count("@") > 1:
        return "several_at"
    mailbox, domain = address.split("@")
    if len(mailbox) > _MAX_MAILBOX_LENGTH or not _MAILBOX_RE.fullmatch(mailbox):
        return "mailbox"
    if any(label.startswith("xn--") for label in domain.split(".")):
        return "not_ascii"
    if not _DOMAIN_RE.fullmatch(domain):
        return "domain"
    return None


def is_address(value: object) -> bool:
    return address_problem(value) is None


def normalize_address(value: str) -> str:
    """Адрес, каким он хранится: без пробелов по краям, строчными буквами.

    Запись, которая не адрес, — `InvalidEmailAddress`.
    """
    problem = address_problem(value)
    if problem is not None:
        raise InvalidEmailAddress(problem)
    return value.strip().lower()
