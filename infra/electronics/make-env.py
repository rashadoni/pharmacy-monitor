"""Файл окружения экземпляра электроники — из аптечного, по белому списку.

Запускает install.sh от root: `python make-env.py <аптечный файл> <новый файл>
<база> <адрес сайта>`. Значения секретов никуда не печатаются; единственное,
что уходит в вывод, — новый пароль входа (последней строкой, для владельца) и
имя роли PostgreSQL (первой, для `createdb`).

Переносится только то, что экземпляру нужно и что у экземпляров общее: почта
для отправки и учётка прокси. Всё остальное — в том числе `EMAIL_TO` (список
получателей аптечного экземпляра), аптечные сайты, Sentry, Telegram, ключи
сторонних сервисов — не переносится: письмо тревоги второго экземпляра не
должно уйти аптечным получателям.
"""

from __future__ import annotations

import os
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

import bcrypt
from sqlalchemy.engine import make_url

CARRIED = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "DECODO_USERNAME",
    "DECODO_PASSWORD",
    "DECODO_HOST",
    "DECODO_PORTS",
    "DECODO_PAGE_ATTEMPTS",
)


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


def main() -> int:
    source_path, target_path, database, site_url = sys.argv[1:5]
    source = read_env(source_path)

    raw_url = source.get("DATABASE_URL", "")
    if not raw_url:
        print("в аптечном файле окружения нет DATABASE_URL", file=sys.stderr)
        return 1
    url = make_url(raw_url)
    if not url.database or url.database == database:
        print("адрес аптечной базы не содержит имени базы или уже называет новую", file=sys.stderr)
        return 1
    # Та же учётка и тот же сервер, другая база; параметры адреса сохраняются.
    new_url = url.set(database=database).render_as_string(hide_password=False)

    lines = [
        "# Экземпляр электроники. Создан install.sh; аптечный файл — образец, не источник правды.",
        "PM_PROFILE=electronics",
        f"DATABASE_URL={new_url}",
    ]
    redis = source.get("REDIS_URL", "")
    if redis:
        parts = urlsplit(redis)
        current = parts.path.strip("/")
        if parts.scheme in {"redis", "rediss"} and (current.isdigit() or current == ""):
            # Своя база Redis: счётчики запросов и замки не делятся с аптеками.
            mine = (int(current) if current else 0) + 1
            lines.append(f"REDIS_URL={urlunsplit(parts._replace(path=f'/{mine}'))}")
        # Адрес другого вида (сокет, параметры) не переписывается: без REDIS_URL
        # счётчики живут в памяти процесса, это безопаснее общей базы.
    for key in CARRIED:
        if source.get(key):
            lines.append(f"{key}={source[key]}")

    password = secrets.token_urlsafe(15)
    lines += [
        f"JWT_SECRET={secrets.token_hex(32)}",
        f"PHARMACY_API_KEY={secrets.token_hex(24)}",
        "ADMIN_PASSWORD_HASH=" + bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        f"PHARMACY_PUBLIC_URL={site_url}",
        # Через прокси ходит только Baku Electronics; Irshad открывается напрямую.
        "DECODO_SITES=bakuelectronics",
        "SCRAPE_REPORT_EMAIL=0",
    ]

    descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print(url.username or "")
    print(password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
