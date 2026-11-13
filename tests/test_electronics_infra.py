"""Юниты и сценарии установки экземпляра электроники согласованы между собой.

На сервере их ставит root, и ошибка в них видна только там. Тест читает файлы
`infra/electronics/` и сверяет то, что обязано совпадать: порты, пути, имена
сайтов, команды CLI.
"""

from __future__ import annotations

import re
import subprocess
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
    assert sync.count(f"127.0.0.1:{api_port}") >= 2
    # Порты аптечного экземпляра и Grafana заняты.
    assert {api_port, web_port}.isdisjoint({"8080", "3000", "3001", "9090"})
    block = _text("Caddyfile.block")
    assert "__DOMAIN__ {" in block
    assert "reverse_proxy 127.0.0.1:__API_PORT__" in block
    assert "reverse_proxy 127.0.0.1:__WEB_PORT__" in block


def test_environment_is_built_for_the_electronics_profile():
    install = _text("install.sh")
    sync = _text("sync.sh")
    assert 'echo "PM_PROFILE=electronics"' in install
    # Синхронизация отказывает, если файл окружения не этого профиля.
    assert 'grep -qx "PM_PROFILE=$PROFILE" "$ENV_FILE"' in sync
    assert "NEXT_PUBLIC_PM_PROFILE=$PROFILE" in sync
    # Аптечные секреты входа и аптечные сайты в окружение экземпляра не переходят.
    dropped = re.search(r"grep -Ev '\^\(([^']+)\)='", install).group(1).split("|")
    for key in ("DATABASE_URL", "JWT_SECRET", "PHARMACY_API_KEY", "ADMIN_PASSWORD_HASH"):
        assert key in dropped, key
    for prefix in ("PHARMONLINE_", "APTEKONLINE_", "ALOE_", "SENTRY_", "TELEGRAM_"):
        assert any(item.startswith(prefix) for item in dropped), prefix
    # Через прокси ходит только сайт, который без него не открывается.
    assert 'echo "DECODO_SITES=bakuelectronics"' in install
    # Пароль входа показывается владельцу один раз, одной строкой; больше
    # нигде его значение в вывод не идёт.
    shown = [
        line
        for line in install.splitlines()
        if "echo" in line and re.search(r"\$ADMIN_PASSWORD(?![_A-Z])", line)
    ]
    assert len(shown) == 1 and "Вход:" in shown[0], shown
