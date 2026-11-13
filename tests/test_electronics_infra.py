"""Юниты и сценарии установки экземпляра электроники согласованы между собой.

На сервере их ставит root, и ошибка в них видна только там. Тест читает файлы
`infra/electronics/` и сверяет то, что обязано совпадать: порты, пути, имена
сайтов, команды CLI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from src import site_profile
from src.main import cli

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra" / "electronics"
UNITS = sorted((INFRA / "systemd").iterdir())


def _text(name: str) -> str:
    return (INFRA / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["install.sh", "sync.sh"])
def test_scripts_parse(script):
    done = subprocess.run(["bash", "-n", str(INFRA / script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_units_belong_to_the_electronics_instance_only():
    assert UNITS, "нет юнитов"
    for unit in UNITS:
        body = unit.read_text(encoding="utf-8")
        assert unit.name.startswith("electro-monitor-"), unit.name
        # Ни один юнит не читает аптечное окружение и не пишет в аптечные каталоги.
        assert "/etc/pharmacy-monitor" not in body, unit.name
        for line in body.splitlines():
            if line.startswith(("WorkingDirectory=", "ReadWritePaths=")):
                assert "/opt/pharmacy-monitor" not in line, (unit.name, line)
        if "python -m" in body:
            assert "EnvironmentFile=/etc/electro-monitor/env" in body, unit.name
            assert "WorkingDirectory=/opt/electro-monitor" in body, unit.name


def test_every_unit_command_exists_in_the_cli():
    commands = set(cli.commands)
    seen = set()
    for unit in UNITS:
        for found in re.findall(r"python -m src\.main ([a-z-]+)", unit.read_text(encoding="utf-8")):
            seen.add(found)
            assert found in commands, (unit.name, found)
    assert {"run", "health-check"} <= seen


def test_scrape_timers_cover_exactly_the_scraped_sites():
    timers = {
        re.fullmatch(r"electro-monitor-scrape@(\w+)\.timer", unit.name).group(1)
        for unit in UNITS
        if unit.name.startswith("electro-monitor-scrape@") and unit.name.endswith(".timer")
    }
    profile = site_profile.ELECTRONICS
    # Сайт, чей каталог приходит файлом, ночным таймером не собирается.
    assert timers == set(profile.sites) - set(profile.imported_sites)
    for site in timers:
        body = (INFRA / "systemd" / f"electro-monitor-scrape@{site}.timer").read_text("utf-8")
        assert f"Unit=electro-monitor-scrape@{site}.service" in body
        # Первый запуск — после открытия суточного окна ритма (22:00 UTC).
        first = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):00 UTC", body)
        assert first and int(first.group(1)) == 22, site


def test_ports_agree_between_units_scripts_and_caddy():
    api_unit = (INFRA / "systemd" / "electro-monitor-api.service").read_text("utf-8")
    web_unit = (INFRA / "systemd" / "electro-monitor-frontend.service").read_text("utf-8")
    install = _text("install.sh")
    sync = _text("sync.sh")
    api_port = re.search(r"--port (\d+)", api_unit).group(1)
    web_port = re.search(r"Environment=PORT=(\d+)", web_unit).group(1)
    assert f"API_PORT={api_port}" in install and f"WEB_PORT={web_port}" in install
    assert f"API_PORT={api_port}" in sync
    # Порты аптечного экземпляра и Grafana заняты.
    assert {api_port, web_port}.isdisjoint({"8080", "3000", "3001", "9090"})
    block = _text("Caddyfile.block")
    assert "__DOMAIN__ {" in block
    assert "reverse_proxy 127.0.0.1:__API_PORT__" in block
    assert "reverse_proxy 127.0.0.1:__WEB_PORT__" in block


def test_sync_refuses_a_foreign_environment_and_builds_with_the_profile():
    sync = _text("sync.sh")
    # Синхронизация отказывает, если файл окружения не этого профиля или без базы.
    assert 'grep -qx "PM_PROFILE=$PROFILE" "$ENV_FILE"' in sync
    assert "grep -q '^DATABASE_URL=' \"$ENV_FILE\"" in sync
    assert "NEXT_PUBLIC_PM_PROFILE=$PROFILE" in sync
    # Файл окружения читает systemd, а не оболочка: в нём хеш пароля с «$».
    assert 'EnvironmentFile="$ENV_FILE"' in sync
    assert not re.search(r"(^|\s)(\.|source)\s+\"?\$ENV_FILE", sync, re.M)
    # Приватные файлы сборки аптечного интерфейса и его сборка сюда не переходят.
    # Исключения — БЕЗ косой черты: с ней rsync ловит только каталоги, а
    # `.next` живого аптечного каталога — ссылка на его действующую сборку.
    for excluded in ("'.env*'", "'.next'", "'node_modules'"):
        assert f"--exclude {excluded}" in sync, excluded
    assert "--exclude '.next/'" not in sync and "--exclude 'node_modules/'" not in sync
    for line in sync.splitlines():
        if "as_pm rsync" in line and "$SRC/" in line:
            assert "--safe-links" in line or line.rstrip().endswith("\\"), line
    # Миграции и метка базы — каждый раз, а не «если код изменился».
    body_before_restart = sync[: sync.index("перезапуск API")]
    assert "-m alembic upgrade head" in body_before_restart
    assert "-m src.main init-db" in body_before_restart
    # Под идущим сбором код не меняют.
    assert "systemctl show -p ActiveState" in sync
    assert "is-active --quiet 'electro-monitor-scrape" not in sync


def test_root_never_executes_or_writes_what_pm_can_replace():
    install = _text("install.sh")
    sync = _text("sync.sh")
    unit = (INFRA / "systemd" / "electro-monitor-sync.service").read_text("utf-8")
    # Каталог экземпляра принадлежит pm: сценарий, который root исполняет по
    # таймеру, лежит в каталоге root.
    assert "ExecStart=/usr/local/lib/electro-monitor/sync.sh" in unit
    assert "/opt/electro-monitor" not in unit
    assert "LIB=/usr/local/lib/electro-monitor" in install
    # В каталоги pm пишет pm: ни одного rsync и chown от root.
    for line in sync.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(r"(?<!as_pm )\brsync ", line), line
        assert "chown" not in line, line
    front = (INFRA / "systemd" / "electro-monitor-frontend.service").read_text("utf-8")
    assert "ExecStartPre=+" not in front
    # Сам сценарий установки исполняет от root только системный интерпретатор
    # в изолированном режиме: venv аптечного экземпляра принадлежит pm.
    assert 'SYSPY="/usr/bin/python3 -I"' in install
    # Тело обёртки `electro-monitor` — не команды установки: её строка с
    # интерпретатором исполняется уже от pm, внутри временного юнита.
    body = re.sub(r"<<WRAP\n.*?\nWRAP\n", "\n", install, flags=re.S)
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            continue
        # Интерпретатор из venv — в начале команды или сразу после «$(».
        assert not re.search(r'(^\s*|\$\(\s*)"?\$PY"?\s', line), line
    # Повторный запуск идёт из каталога установки: файл в самого себя не копируется.
    assert "if [[ ! $HERE -ef $LIB ]]; then" in install


def test_units_and_scripts_never_call_the_venv_console_scripts():
    # `.venv/bin/pharmacy-monitor` и `.venv/bin/alembic` берут `src` из
    # аптечного каталога; экземпляр электроники зовёт только `python -m`.
    texts = [unit.read_text("utf-8") for unit in UNITS] + [_text("install.sh"), _text("sync.sh")]
    for body in texts:
        for line in body.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert ".venv/bin/pharmacy-monitor" not in line, line
            assert ".venv/bin/alembic" not in line, line
            assert ".venv/bin/uvicorn" not in line, line


def test_install_enables_no_scrapes_and_no_health_alerts_by_itself():
    # До записи разделов и первой загрузки каталога база пуста: проверка
    # здоровья считала бы это критической поломкой, а сборы шли бы впустую.
    install = _text("install.sh")
    enabling = [line for line in install.splitlines() if "systemctl enable" in line]
    # Команды сценария стоят с отступом в четыре пробела; с отступом в два —
    # подсказка владельцу в конце вывода.
    automatic = [line for line in enabling if line.startswith("    ")]
    hints = [line for line in enabling if not line.startswith("    ")]
    assert automatic and hints, enabling
    for line in automatic:
        assert "scrape@" not in line and "health" not in line, line
    assert any("scrape@" in line for line in hints)


def test_caddy_is_changed_only_through_a_validated_copy():
    install = _text("install.sh")
    append = install.index('"$LIB/Caddyfile.block" >> "$new"')
    validate = install.index('caddy validate --config "$new"')
    put = install.index('cp -p "$new" "$CADDYFILE"')
    assert append < validate < put
    # Проверка — от имени службы, а не root.
    assert 'sudo -u "${caddy_user:-caddy}" caddy validate' in install
    # Блок дописывается только в копию.
    assert '>> "$CADDYFILE"' not in install
    # Неудачный перезапуск возвращает прежний файл, а не обрывает сценарий.
    assert "if ! systemctl restart caddy ||" in install
    # --no-caddy не должен падать на необъявленной переменной.
    assert install.index("CADDYFILE=/etc/caddy/Caddyfile") < install.index("for arg in")


# На сервере помощник исполняет системный интерпретатор в изолированном режиме,
# без пакетов проекта. Хеш пароля ему даёт пакет bcrypt, а без него — модуль
# crypt (в Python 3.13 его уже нет): где нет ни того ни другого, тест пропускается.
def _usable(interpreter: list[str], module: str) -> bool:
    try:
        probe = subprocess.run([*interpreter, "-c", f"import {module}"], capture_output=True)
    except OSError:
        return False
    return probe.returncode == 0


_SYSTEM = ["/usr/bin/python3", "-I"]
SYSTEM_PYTHON = _SYSTEM if _usable(_SYSTEM, "bcrypt") or _usable(_SYSTEM, "crypt") else None
# Интерпретатор тестов без пакетов проекта: пакета bcrypt не видит, идёт через crypt.
_BARE = [sys.executable, "-I", "-S"]
BARE_PYTHON = _BARE if not _usable(_BARE, "bcrypt") and _usable(_BARE, "crypt") else None


def _make_env(tmp_path, source_text, interpreter=(sys.executable,)):
    source = tmp_path / "pharmacy.env"
    source.write_text(source_text, encoding="utf-8")
    target = tmp_path / "electro.env"
    done = subprocess.run(
        [
            *interpreter,
            str(INFRA / "make-env.py"),
            str(source),
            str(target),
            "electro_monitor",
            "https://electro.example",
        ],
        capture_output=True,
        text=True,
    )
    values = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    return done, target, values


PHARMACY_ENV = """# старый секрет в комментарии: JWT_SECRET=old-commented-secret
DATABASE_URL="postgresql+psycopg://pm:p%40ss$word!x=y@127.0.0.1:5432/pharmacy_monitor?sslmode=require&application_name=pm/api"
REDIS_URL=redis://127.0.0.1:6379/0
JWT_SECRET=pharmacy-jwt-secret
PHARMACY_API_KEY=pharmacy-api-key
ADMIN_PASSWORD_HASH=$2b$12$pharmacyhashpharmacyhashpharmacyhashpharmacyhashpharm
EMAIL_TO=owner@pharmacy.example, client@pharmacy.example
SMTP_HOST=smtp.example
SMTP_PASSWORD=smtp secret with spaces
SMTP_FROM=Pharmacy Monitor <digest@example>
DECODO_USERNAME=decodo-user
DECODO_PASSWORD=dec$odo=pass
DECODO_SITES=aptekonline,aloe,pharmonline
SENTRY_DSN=https://sentry.example/1
TELEGRAM_BOT_TOKEN=123:telegram
PHARMONLINE_USE_DDP=1
ANTHROPIC_API_KEY=sk-ant
LOG_FILE=/opt/pharmacy-monitor/logs/app.log
"""


def test_environment_is_built_from_a_whitelist(tmp_path):
    done, target, values = _make_env(tmp_path, PHARMACY_ENV)
    assert done.returncode == 0, done.stderr
    assert values["PM_PROFILE"] == "electronics"
    # Та же учётка и сервер, другая база; параметры адреса и «/» внутри них целы.
    # Та же учётка и сервер, другая база. Меняется только имя базы: остальное —
    # буква в букву, без перекодирования («%» в адресе роняет миграции).
    assert values["DATABASE_URL"] == (
        "postgresql+psycopg://pm:p%40ss$word!x=y@127.0.0.1:5432/electro_monitor"
        "?sslmode=require&application_name=pm/api"
    )
    # Своя база Redis.
    assert values["REDIS_URL"] == "redis://127.0.0.1:6379/1"
    # Почта для отправки и учётка прокси переносятся как есть, с пробелами и «$».
    assert values["SMTP_PASSWORD"] == "smtp secret with spaces"
    # Адрес отправителя общий, имя — своё.
    assert values["SMTP_FROM"] == "Price Monitor <digest@example>"
    assert values["PHARMACY_COOKIE_SECURE"] == "true"
    assert values["DECODO_PASSWORD"] == "dec$odo=pass"
    assert values["DECODO_SITES"] == "bakuelectronics"
    assert values["PHARMACY_PUBLIC_URL"] == "https://electro.example"
    # Свои секреты входа, а не аптечные.
    assert values["JWT_SECRET"] != "pharmacy-jwt-secret" and len(values["JWT_SECRET"]) == 64
    assert values["PHARMACY_API_KEY"] != "pharmacy-api-key"
    # Аптечные получатели писем, аптечные сайты, Sentry, Telegram, ключи
    # сторонних сервисов, пути журнала и комментарии не переходят.
    for key in (
        "EMAIL_TO",
        "SENTRY_DSN",
        "TELEGRAM_BOT_TOKEN",
        "PHARMONLINE_USE_DDP",
        "ANTHROPIC_API_KEY",
        "LOG_FILE",
    ):
        assert key not in values, key
    text = target.read_text(encoding="utf-8")
    assert "old-commented-secret" not in text and "pharmacy.example" not in text
    assert (target.stat().st_mode & 0o777) == 0o640

    # В вывод уходят роль базы и новый пароль входа — и ничего из секретов файла.
    role, password = done.stdout.splitlines()
    assert role == "pm"
    import bcrypt

    assert bcrypt.checkpw(password.encode(), values["ADMIN_PASSWORD_HASH"].encode())
    for secret in ("p%40ss", "word!x", "smtp secret", "dec$odo", values["JWT_SECRET"]):
        assert secret not in done.stdout + done.stderr


@pytest.mark.skipif(SYSTEM_PYTHON is None, reason="нет системного интерпретатора")
def test_environment_helper_runs_on_the_bare_system_interpreter(tmp_path):
    # Без пакетов проекта: только стандартная библиотека, хеш пароля — bcrypt.
    done, _, values = _make_env(tmp_path, PHARMACY_ENV, interpreter=SYSTEM_PYTHON)
    assert done.returncode == 0, done.stderr
    import bcrypt

    role, password = done.stdout.splitlines()
    assert role == "pm"
    assert values["ADMIN_PASSWORD_HASH"].startswith("$2")
    assert bcrypt.checkpw(password.encode(), values["ADMIN_PASSWORD_HASH"].encode())
    assert values["DATABASE_URL"].endswith(
        "/electro_monitor?sslmode=require&application_name=pm/api"
    )


@pytest.mark.skipif(BARE_PYTHON is None, reason="нет интерпретатора с crypt и без bcrypt")
def test_password_hash_made_without_the_bcrypt_package_still_opens_the_login(tmp_path):
    done, _, values = _make_env(tmp_path, PHARMACY_ENV, interpreter=BARE_PYTHON)
    assert done.returncode == 0, done.stderr
    import bcrypt

    _, password = done.stdout.splitlines()
    assert bcrypt.checkpw(password.encode(), values["ADMIN_PASSWORD_HASH"].encode())
    assert not bcrypt.checkpw(b"another password", values["ADMIN_PASSWORD_HASH"].encode())


def test_environment_helper_refuses_instead_of_guessing(tmp_path):
    # Нет адреса базы — отказ с сообщением, файл не создаётся.
    done, target, _ = _make_env(tmp_path, "SMTP_HOST=smtp.example\n")
    assert done.returncode != 0 and not target.exists()
    assert "DATABASE_URL" in done.stderr

    # Адрес без имени роли (сокет) или без имени базы — отказ ДО создания файла:
    # иначе пароль входа был бы выдан, а установка не прошла бы никогда.
    for url in (
        "postgresql:///pharmacy_monitor",
        "postgresql://pm:x@h/",
        "postgresql://pm:x@h/electro_monitor",
    ):
        done, target, _ = _make_env(tmp_path, f"DATABASE_URL={url}\n")
        assert done.returncode != 0 and not target.exists(), url
        assert "x@h" not in done.stderr

    # Существующий файл не перезаписывается: пароль входа в нём уже выдан.
    done, target, values = _make_env(tmp_path, PHARMACY_ENV)
    assert done.returncode == 0
    again = subprocess.run(
        [
            sys.executable,
            str(INFRA / "make-env.py"),
            str(tmp_path / "pharmacy.env"),
            str(target),
            "electro_monitor",
            "https://x",
        ],
        capture_output=True,
        text=True,
    )
    assert again.returncode != 0
    assert target.read_text(encoding="utf-8").count("PM_PROFILE=electronics") == 1

    # Redis с адресом другого вида не переписывается наугад и не переносится.
    (tmp_path / "electro.env").unlink()
    done, _, values = _make_env(
        tmp_path, PHARMACY_ENV.replace("redis://127.0.0.1:6379/0", "unix:///run/redis.sock?db=0")
    )
    assert done.returncode == 0 and "REDIS_URL" not in values
    (tmp_path / "electro.env").unlink()
    done, _, values = _make_env(
        tmp_path, PHARMACY_ENV.replace("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/1")
    )
    assert values["REDIS_URL"] == "redis://127.0.0.1:6379/2"
