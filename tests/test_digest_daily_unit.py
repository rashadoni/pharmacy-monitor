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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import comparison_export, notifier, storage
from src.main import cli
from tests.test_daily_price_letter import _book, _mixed_catalog, _reader, _trusted_run

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "infra/systemd"
UNIT = SYSTEMD / "pharmacy-monitor-digest-daily.service"
TIMER = SYSTEMD / "pharmacy-monitor-digest-daily.timer"
SCRAPE_TIMER = SYSTEMD / "pharmacy-monitor-scrape@.timer"
# Часовой пояс прод-хоста: в нём заданы OnCalendar таймеров сбора.
HOST_TZ = ZoneInfo("Europe/Berlin")
WINTER, SUMMER = date(2026, 1, 12), date(2026, 7, 13)
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


def _alarms(timer_text: str) -> tuple[list[tuple[int, int, ZoneInfo]], int]:
    """Будильники таймера — (час, минута, пояс) каждой строки OnCalendar — и
    RandomizedDelaySec. Строк может быть несколько; пустая сбрасывает набранные
    выше, как в systemd. Пояс без явного указания — пояс хоста."""
    alarms: list[tuple[int, int, ZoneInfo]] = []
    delay = 0
    for line in timer_text.splitlines():
        line = line.strip()
        if line.startswith("RandomizedDelaySec="):
            delay = int(line.removeprefix("RandomizedDelaySec="))
        if not line.startswith("OnCalendar="):
            continue
        value = line.removeprefix("OnCalendar=").strip()
        if not value:
            alarms = []
            continue
        clock = re.search(
            r"(\d{1,2}):(\d{2})(?::\d{2})?(?: (UTC|[A-Z][A-Za-z_]+/[A-Za-z_]+))?$", value
        )
        assert clock, f"тест умеет только расписание «… ЧЧ:ММ[:СС] [пояс]», научи его: {value}"
        zone = ZoneInfo(clock[3]) if clock[3] else HOST_TZ
        alarms.append((int(clock[1]), int(clock[2]), zone))
    return alarms, delay


def _scrape_timer_texts() -> list[str]:
    """Таймер сбора и его drop-in'ы. Каталога drop-in'ов может не быть вовсе."""
    dropins = sorted((SYSTEMD / "overrides").glob("pharmacy-monitor-scrape@*.timer.d/*.conf"))
    return [path.read_text(encoding="utf-8") for path in (SCRAPE_TIMER, *dropins)]


def _since_last_scrape_alarm(day: date, letter_hour_utc: int, timer_texts: list[str]) -> timedelta:
    """Сколько к моменту письма прошло после ПОСЛЕДНЕГО перед ним будильника сбора
    (с его полной случайной задержкой). Смотрится время суток: недельный будильник
    считается так, будто он звонит каждую ночь."""
    letter_at = datetime(day.year, day.month, day.day, letter_hour_utc, 0)
    parsed = [_alarms(text) for text in timer_texts]
    delay = timedelta(seconds=max(delay for _alarms_of_file, delay in parsed))
    waits = []
    for alarms, _delay in parsed:
        for hour, minute, zone in alarms:
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            rang_at = local.astimezone(timezone.utc).replace(tzinfo=None) + delay
            while rang_at > letter_at:  # этот будильник перед письмом звонил сутками раньше
                rang_at -= timedelta(days=1)
            waits.append(letter_at - rang_at)
    assert waits, "у таймера сбора не нашлось ни одного OnCalendar"
    return min(waits)


@pytest.mark.parametrize("day", [WINTER, SUMMER], ids=["winter", "summer"])
def test_letter_goes_out_after_the_nightly_scrapes(day):
    """Таймеры сбора заданы по времени хоста, письмо — в UTC: зазор сверяется
    в обе половины года. Переносишь таймер письма или таймеры сбора — письмо
    должно остаться утром после сбора."""
    waited = _since_last_scrape_alarm(day, _letter_hour_utc(), _scrape_timer_texts())

    assert MIN_AFTER_SCRAPE_START <= waited <= MAX_AFTER_SCRAPE_START, waited


_TWO_ALARMS_NO_DROPINS = """
[Timer]
# ночной сбор и повтор
OnCalendar=*-*-* 01:00:00
OnCalendar=*-*-* 03:15:00
RandomizedDelaySec=300
Persistent=true
"""


def test_gap_check_counts_from_the_last_of_several_alarms():
    """Таймер сбора с двумя строками OnCalendar и без drop-in'ов: зазор считается
    от последнего будильника. 03:15 по хосту — это 02:15 UTC зимой и 01:15 летом;
    до письма в 05:00 UTC остаётся 2 ч 45 мин и 3 ч 45 мин минус случайная задержка."""
    assert _alarms(_TWO_ALARMS_NO_DROPINS) == ([(1, 0, HOST_TZ), (3, 15, HOST_TZ)], 300)

    texts = [_TWO_ALARMS_NO_DROPINS]
    assert _since_last_scrape_alarm(WINTER, 5, texts) == timedelta(hours=2, minutes=40)
    assert _since_last_scrape_alarm(SUMMER, 5, texts) == timedelta(hours=3, minutes=40)
    # Повтор в 04:30 по хосту был бы зимой слишком близко к письму.
    later = [_TWO_ALARMS_NO_DROPINS.replace("03:15:00", "04:30:00")]
    assert _since_last_scrape_alarm(WINTER, 5, later) < MIN_AFTER_SCRAPE_START


def test_gap_check_reads_timers_the_way_systemd_does():
    """Пустой OnCalendar сбрасывает расписание выше; день недели и пояс понимаются."""
    weekly_dropin = "OnCalendar=\nOnCalendar=Mon *-*-* 02:00:00\nRandomizedDelaySec=300\n"
    assert _alarms("OnCalendar=*-*-* 01:00:00\n" + weekly_dropin) == ([(2, 0, HOST_TZ)], 300)
    assert _alarms("OnCalendar=*-*-* 23:30 UTC\n") == ([(23, 30, ZoneInfo("UTC"))], 0)
    # Будильник позже письма по времени суток — это вчерашний вечер.
    assert _since_last_scrape_alarm(WINTER, 5, ["OnCalendar=*-*-* 21:00:00 UTC\n"]) == (
        timedelta(hours=8)
    )
    with pytest.raises(AssertionError, match="научи его"):
        _alarms("OnCalendar=hourly\n")
