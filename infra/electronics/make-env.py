"""Файл окружения экземпляра электроники — из аптечного, по белому списку.

Запускает install.sh от root системным интерпретатором в изолированном режиме
(`/usr/bin/python3 -I`): только стандартная библиотека, ничего из каталогов
учётки pm. Вызов: `make-env.py <аптечный файл> <новый файл> <база> <адрес сайта>`.

Значения секретов никуда не печатаются. В вывод уходят две строки: имя роли
PostgreSQL (для `createdb`) и новый пароль входа (его install.sh показывает
владельцу один раз).

Переносится только то, что экземпляру нужно и что у экземпляров общее: почта
для отправки и учётка прокси. Всё остальное — в том числе `EMAIL_TO` (список
получателей аптечного экземпляра), аптечные сайты, Sentry, Telegram, ключи
сторонних сервисов — не переносится: письмо тревоги второго экземпляра не
должно уйти аптечным получателям.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

CARRIED = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "DECODO_USERNAME",
    "DECODO_PASSWORD",
    "DECODO_HOST",
    "DECODO_PORTS",
    "DECODO_PAGE_ATTEMPTS",
)
APP_NAME = "Price Monitor"


def read_env(path: str) -> dict[str, str]:
    """Строки `KEY=значение` файла окружения systemd; кавычки вокруг значения снимаются."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    return values


def password_hash(password: str) -> str:
    """bcrypt-хеш пароля — тот же вид, что проверяет вход (`$2b$…`)."""
    try:
        import bcrypt
    except ImportError:
        # Системный интерпретатор сервера без пакета bcrypt: тот же алгоритм
        # через libxcrypt.
        import crypt

        hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))
        if not hashed or not hashed.startswith("$2"):
            raise RuntimeError("системная библиотека не умеет bcrypt") from None
        return hashed
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def main() -> int:
    source_path, target_path, database, site_url = sys.argv[1:5]
    source = read_env(source_path)

    raw_url = source.get("DATABASE_URL", "")
    if not raw_url:
        print("в аптечном файле окружения нет DATABASE_URL", file=sys.stderr)
        return 1
    parts = urlsplit(raw_url)
    current = parts.path.lstrip("/")
    if not current or "/" in current or current == database:
        print(
            "из адреса аптечной базы не удалось взять имя базы (или оно уже новое). Так бывает, "
            "когда в пароле стоит «/», «?» или «#» без кодирования: файл не создан",
            file=sys.stderr,
        )
        return 1
    if not parts.username:
        print("в адресе аптечной базы нет имени роли PostgreSQL", file=sys.stderr)
        return 1
    # Та же учётка и тот же сервер, другая база. Меняется только имя базы:
    # остальное остаётся буква в букву, как в аптечном файле. Пересборка адреса
    # закодировала бы знаки пароля через «%», а на «%» в адресе спотыкается
    # файл настроек миграций — и печатает адрес целиком.
    new_url = urlunsplit(parts._replace(path=f"/{database}"))
    if new_url.count("%") != raw_url.count("%"):
        print("адрес базы после замены имени изменился не только в имени — отказ", file=sys.stderr)
        return 1

    lines = [
        "# Экземпляр электроники. Создан install.sh; аптечный файл — образец, не источник правды.",
        "PM_PROFILE=electronics",
        f"DATABASE_URL={new_url}",
    ]
    redis = source.get("REDIS_URL", "")
    if redis:
        redis_parts = urlsplit(redis)
        index = redis_parts.path.strip("/")
        plain = redis_parts.scheme in {"redis", "rediss"} and not redis_parts.query
        if plain and (index.isdigit() or index == ""):
            # Своя база Redis: счётчики запросов и замки не делятся с аптеками.
            mine = (int(index) if index else 0) + 1
            lines.append(f"REDIS_URL={urlunsplit(redis_parts._replace(path=f'/{mine}'))}")
        # Адрес другого вида (сокет, параметры) не переписывается: без REDIS_URL
        # счётчики живут в памяти процесса, это безопаснее общей базы.
    for key in CARRIED:
        if source.get(key):
            lines.append(f"{key}={source[key]}")
    sender = source.get("SMTP_FROM", "")
    if sender:
        # Адрес отправителя общий, имя — своё: письма экземпляра не подписаны аптечным.
        address = re.search(r"<([^<>]+)>", sender)
        lines.append(
            f"SMTP_FROM={APP_NAME} <{address.group(1)}>" if address else f"SMTP_FROM={sender}"
        )

    password = secrets.token_urlsafe(15)
    lines += [
        f"JWT_SECRET={secrets.token_hex(32)}",
        f"PHARMACY_API_KEY={secrets.token_hex(24)}",
        f"ADMIN_PASSWORD_HASH={password_hash(password)}",
        f"PHARMACY_PUBLIC_URL={site_url}",
        # Сайт открывается только по HTTPS: кука сессии — с признаком Secure.
        "PHARMACY_COOKIE_SECURE=true",
        # Через прокси ходит только Baku Electronics; Irshad открывается напрямую.
        "DECODO_SITES=bakuelectronics",
        "SCRAPE_REPORT_EMAIL=0",
    ]

    descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print(parts.username)
    print(password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
