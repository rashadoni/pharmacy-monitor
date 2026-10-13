"""Юнит и таймер утреннего письма о разнице цен (infra/systemd).

Команда берётся из самого файла юнита и исполняется через CLI целиком — как в
test_cadence_guard.py и test_rematch_unit.py: опечатка во флаге или в имени
команды роняет тест, а не утреннее письмо. Стоит ли юнит на сервере, эти тесты
не знают: `deploy.yml` юниты не ставит, это делает root руками (RUNBOOK,
«Утреннее письмо с разницей цен»).
"""

from __future__ import annotations

import re
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import comparison_export, notifier, storage
from src.main import cli
from tests.test_cadence_guard import (
    SCRAPE_TIMER,
    SCRAPE_TIMER_DROPINS,
    _timer_fire_utc,
    _timer_schedule,
)
from tests.test_daily_price_letter import _book, _mixed_catalog, _reader, _trusted_run

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "infra/systemd"
UNIT = SYSTEMD / "pharmacy-monitor-digest-daily.service"
TIMER = SYSTEMD / "pharmacy-monitor-digest-daily.timer"
# Письмо — утро после ночного сбора. Сбор сайта с сопоставлением укладывается в
# час с запасом (самый долгий — около 35 минут и сопоставление): раньше чем
# через два часа после его старта письмо застало бы сбор на середине. Позже чем
# через двенадцать — это уже письмо перед следующей ночью, со вчерашними данными.
MIN_AFTER_SCRAPE_START = timedelta(hours=2)
MAX_AFTER_SCRAPE_START = timedelta(hours=12)


def _settings(unit_file: Path) -> dict[str, list[str]]:
    """Ключ → значения из файла юнита, в порядке записи."""
    found: dict[str, list[str]] = {}
    for line in unit_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value.strip())
    return found


def _exec_args() -> list[str]:
    (command,) = _settings(UNIT)["ExecStart"]
    argv = shlex.split(command)
    assert argv[0] == "/opt/pharmacy-monitor/.venv/bin/pharmacy-monitor"
    return argv[1:]


def test_unit_sends_the_daily_letter_with_an_azerbaijani_attachment():
    assert _exec_args() == ["notify", "digest", "daily", "--locale", "az"]


def test_unit_command_runs_as_written(monkeypatch, db_session):
    """Команда юнита целиком, без правок: письмо уходит, вложение — на азербайджанском."""
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    monkeypatch.delenv("COUNTRY_IDENTITY_POLICY", raising=False)
    monkeypatch.delenv("OFFER_AVAILABILITY_POLICY", raising=False)
    _reader(db_session)
    _mixed_catalog(db_session, _trusted_run(db_session))

    with patch("src.notifier.send_email", return_value=True) as mock_email:
        result = CliRunner().invoke(cli, _exec_args())

    assert result.exit_code == 0, result.output
    assert "OK: daily digest sent to 1 recipients" in result.output
    book, file_name = _book(mock_email.call_args)
    assert file_name.startswith("qiymet-ferqleri-") and file_name.endswith(".xlsx")
    assert book.sheetnames[0] == comparison_export.strings("az")["sheet_diff"]


def test_unit_reads_the_production_env_and_cannot_hang_forever():
    unit = _settings(UNIT)
    assert unit["Type"] == ["oneshot"]
    assert unit["User"] == ["pm"]
    assert unit["EnvironmentFile"] == ["/etc/pharmacy-monitor/env"]
    assert unit["WorkingDirectory"] == ["/opt/pharmacy-monitor"]
    # У oneshot без TimeoutStartSec предела нет: зависшая отправка держала бы
    # юнит, и следующее утро таймер его бы не запустил. Предел — с запасом на
    # отправку каждому получателю.
    (timeout,) = unit["TimeoutStartSec"]
    assert 2 * notifier.SMTP_TIMEOUT_SEC <= int(timeout) <= 3600
    # Ни повтора, ни соседних команд: повтор после частичной отправки дал бы
    # получателям второе письмо.
    assert not {"Restart", "ExecStartPre", "ExecStartPost"} & set(unit)


def _letter_hour_utc() -> int:
    (schedule,) = _settings(TIMER)["OnCalendar"]
    clock = re.fullmatch(r"\*-\*-\* (\d{2}):00:00 UTC", schedule)
    assert clock, f"тест умеет только ежедневное расписание на ровный час в UTC: {schedule}"
    return int(clock[1])


def test_timer_fires_at_nine_in_baku_every_day():
    timer = _settings(TIMER)
    assert timer["OnCalendar"] == ["*-*-* 05:00:00 UTC"]
    assert timer["Persistent"] == ["true"]
    assert timer["Unit"] == [UNIT.name]
    assert comparison_export.baku_time(datetime(2026, 1, 15, 5, 0)).hour == 9
    assert int(timer["RandomizedDelaySec"][0]) <= 600


@pytest.mark.parametrize("day", [date(2026, 1, 12), date(2026, 7, 13)], ids=["winter", "summer"])
def test_letter_goes_out_after_the_nightly_scrapes(day):
    """Таймеры сбора заданы по времени хоста, письмо — в UTC: зазор сверяется
    в обе половины года. Переносишь таймер письма или таймеры сбора — письмо
    должно остаться утром после сбора."""
    letter_at = datetime(day.year, day.month, day.day, _letter_hour_utc(), 0)
    for timer_file in (SCRAPE_TIMER, *SCRAPE_TIMER_DROPINS):
        hour, delay = _timer_schedule(timer_file)
        scrape_at = _timer_fire_utc(day, hour) + timedelta(seconds=delay)
        if scrape_at > letter_at:  # последний сбор перед письмом был сутками раньше
            scrape_at -= timedelta(days=1)
        waited = letter_at - scrape_at
        assert MIN_AFTER_SCRAPE_START <= waited <= MAX_AFTER_SCRAPE_START, (timer_file.name, waited)
