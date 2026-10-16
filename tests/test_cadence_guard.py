"""Гвард ритма полного сбора: окно ритма и формы запуска `run`.

Гвард уже однажды был написан и не работал ни одного дня: PR #12 проверял
предикат с `mode="category"`, а systemd-юнит на проде зовёт `run --site %i` без
`--mode`. Тесты были зелёные, aloe собирался каждую ночь. Поэтому здесь команда
берётся из самого файла юнита и идёт через CLI целиком — если юнит и гвард
снова разойдутся, упадёт тест, а не недельный ритм.
"""

from __future__ import annotations

import re
import shlex
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import storage
from src._time import utcnow
from src.cadence import (
    CADENCE_GRACE_HOURS,
    SITE_SCRAPE_CADENCE_HOURS,
    cadence_window_start,
    site_cadence_hours,
    site_max_age_hours,
)

REPO = Path(__file__).resolve().parents[1]
SCRAPE_UNIT = REPO / "infra/systemd/pharmacy-monitor-scrape@.service"
SCRAPE_UNIT_DROPIN = (
    REPO / "infra/systemd/overrides/pharmacy-monitor-scrape@.service.d/zz-realtime-alerts.conf"
)
SCRAPE_TIMER = REPO / "infra/systemd/pharmacy-monitor-scrape@.timer"
SCRAPE_TIMER_DROPINS = sorted(
    (REPO / "infra/systemd/overrides").glob("pharmacy-monitor-scrape@*.timer.d/*.conf")
)
PROVISION_SCRIPT = REPO / "scripts/provision_vps.sh"
AUTONOMOUS_PHARMONLINE_WORKFLOW = (
    REPO / ".github/workflows/autonomous-pharmonline-decodo-public-api.yml"
)
BERLIN = ZoneInfo("Europe/Berlin")  # часовой пояс прод-хоста: в нём заданы OnCalendar


def _unit_run_args(unit_file: Path, site: str) -> list[str]:
    """Аргументы `pharmacy-monitor`, с которыми юнит зовёт сбор сайта."""
    commands: list[str] = []
    for line in unit_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            value = line.removeprefix("ExecStart=").strip()
            # Пустое значение в drop-in сбрасывает список, как в systemd.
            commands = [*commands, value] if value else []
    assert len(commands) == 1, f"{unit_file.name}: ожидался один ExecStart, найдено {commands}"
    argv = shlex.split(commands[0].replace("%i", site))
    assert argv[0].endswith("/pharmacy-monitor")
    return argv[1:]


def _timer_alarms(timer_file: Path) -> tuple[list[tuple[int, int]], int]:
    """([(час, минута) каждого будильника по времени хоста], RandomizedDelaySec)."""
    alarms: list[tuple[int, int]] = []
    delay: int | None = None
    for line in timer_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("OnCalendar="):
            value = line.removeprefix("OnCalendar=").strip()
            if not value:  # пустое значение сбрасывает расписание, как в systemd
                alarms = []
                continue
            clock = re.fullmatch(r"\*-\*-\* (\d{2}):(\d{2}):00", value)
            assert clock, f"{timer_file.name}: тест умеет только ежедневное расписание ЧЧ:ММ:00"
            alarms.append((int(clock[1]), int(clock[2])))
        elif line.startswith("RandomizedDelaySec="):
            delay = int(line.removeprefix("RandomizedDelaySec="))
    assert alarms and delay is not None, timer_file
    return alarms, delay


def _full_run(
    session,
    site: str,
    *,
    started_at: datetime,
    minutes: int = 30,
    status: str = "ok",
) -> storage.Run:
    """Завершённая полная попытка по сайту."""
    run = storage.Run(
        tenant_id=1,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=minutes),
        catalog_scope="full",
        full_catalog_sites=site,
    )
    session.add(run)
    session.commit()
    return run


def _timer_fire_utc(day: date, hour: int, minute: int = 0) -> datetime:
    """Момент `OnCalendar=*-*-* HH:MM:00` по времени прод-хоста, в naive UTC."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def weekly_aloe(monkeypatch):
    """aloe на недельном ритме — как все три сайта жили 2026-10-06…13.

    Карта ритма сегодня суточная, но окно обязано работать с любым шагом: сайт
    могут вернуть на неделю, и тогда гвард должен снова пропускать лишние ночи.
    """
    monkeypatch.setitem(SITE_SCRAPE_CADENCE_HOURS, "aloe", 168)
    monkeypatch.setitem(SITE_SCRAPE_CADENCE_HOURS, "aptekonline", 168)


def _berlin_weekday(moment_utc: datetime) -> int:
    return moment_utc.replace(tzinfo=timezone.utc).astimezone(BERLIN).weekday()


# ─── Окно ритма ──────────────────────────────────────────────────────────────


def test_daily_window_opens_every_evening():
    """Сутки сбора открываются в 22:00 UTC — так живут все три сайта."""
    for site in ("pharmonline", "aptekonline", "aloe"):
        assert site_cadence_hours(site) == 24
        assert cadence_window_start(site, datetime(2026, 10, 7, 12, 0)) == datetime(
            2026, 10, 6, 22, 0
        )


def test_window_boundary_belongs_to_the_new_window():
    boundary = datetime(2026, 10, 11, 22, 0)

    assert cadence_window_start("aloe", boundary) == boundary
    assert cadence_window_start("aloe", boundary - timedelta(seconds=1)) == datetime(
        2026, 10, 10, 22, 0
    )


def test_weekly_window_opens_in_the_night_to_monday(weekly_aloe):
    """Неделя сбора открывается в воскресенье 22:00 UTC."""
    wednesday = datetime(2026, 10, 7, 12, 0)

    start = cadence_window_start("aloe", wednesday)

    assert start == datetime(2026, 10, 4, 22, 0)
    assert start.weekday() == 6
    assert cadence_window_start("aloe", datetime(2026, 10, 11, 21, 59, 59)) == start


def test_site_without_declared_cadence_gets_daily_windows():
    """Сайт вне карты ритма — суточный: окно открывается каждый вечер."""
    assert site_cadence_hours("unknown-site") == 24

    start = cadence_window_start("unknown-site", datetime(2026, 10, 7, 12, 0))

    assert start == datetime(2026, 10, 6, 22, 0)


def test_scrape_timers_in_repo_are_the_ones_the_boundary_was_chosen_for():
    """Два ночных будильника — сбор и повтор; drop-in'ов у таймера сбора нет.

    Недельный drop-in aptekonline снят 2026-10-13. Вернётся в репозиторий — сайт
    с суточной картой ритма будет «просрочен» шесть дней из семи.
    """
    assert _timer_alarms(SCRAPE_TIMER) == ([(1, 0), (3, 15)], 300)
    assert SCRAPE_TIMER_DROPINS == []


def test_window_boundary_fits_the_nightly_alarms():
    """Граница окна стоит раньше ночных будильников и рядом с ними.

    Часы и случайная задержка берутся из самого файла таймера: перенесёшь
    будильник, не пересчитав `CADENCE_ANCHOR_UTC`, — упадёт этот тест. Перебираем
    каждую ночь трёх лет, то есть оба перевода часов (они приходятся на
    воскресенье — перебор одних понедельников их не видел).
    """
    alarms, delay_sec = _timer_alarms(SCRAPE_TIMER)
    jitter = timedelta(seconds=delay_sec)
    day = date(2026, 1, 1)
    while day < date(2029, 1, 1):
        windows = set()
        for hour, minute in alarms:
            # Будильник не стоит в часе перевода часов: такого местного времени
            # в одну из ночей года нет вовсе, в другую оно наступает дважды.
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN)
            assert local.utcoffset() == local.replace(fold=1).utcoffset(), (day, hour, minute)
            assert local.astimezone(timezone.utc).astimezone(BERLIN).replace(
                tzinfo=None
            ) == local.replace(tzinfo=None), (day, hour, minute)
            fire = _timer_fire_utc(day, hour, minute)
            opened = cadence_window_start("aloe", fire)
            since_boundary = fire - opened
            # Ночной запуск открывает новое окно, а не дособирает вчерашнее.
            assert timedelta(hours=1) <= since_boundary, (day, hour, minute)
            # Сбор вручную сразу после границы сдвигает следующий плановый на
            # этот зазор; с задержкой таймера и часом на разницу в длительности
            # прогонов он обязан уложиться в запас ритма.
            assert since_boundary + jitter <= timedelta(hours=CADENCE_GRACE_HOURS - 1), (
                day,
                hour,
                minute,
            )
            windows.add(opened)
        # Повтор принадлежит той же ночи, что и первый будильник: иначе он был
        # бы не повтором упавшего сбора, а вторым сбором.
        assert len(windows) == 1, day
        day += timedelta(days=1)


def test_pharmonline_workflow_in_github_actions_has_no_schedule():
    """Сбор pharmonline из GitHub Actions — только ручной запуск.

    Понедельничный cron снят 2026-10-13: дубль идёт мимо гварда ритма
    (`--mode public_api`) и исполняет `main`, а его упавшая попытка после
    удачного ночного сбора отнимает у каталога pharmonline доверие до следующей
    ночи. Вернётся расписание — вернётся и этот риск, теперь еженедельно.
    """
    import yaml

    workflow = yaml.safe_load(AUTONOMOUS_PHARMONLINE_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML читает ключ `on` как булево True.
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}


# ─── Какие сайты пора собирать ───────────────────────────────────────────────


def test_next_night_at_the_same_hour_is_due(db_session):
    """Следующей ночью в то же время суток — собираем.

    Правило «с конца прошлого сбора прошло не меньше ритма» здесь отвечало бы
    «рано»: будильник срабатывает в то же время суток, сбор длится полчаса,
    значит следующей ночью возраст — 23,4 часа. При недельном ритме то же
    правило уводило сбор на восьмой день.
    """
    last = _full_run(db_session, "aloe", started_at=datetime(2026, 10, 11, 23, 3, 13))
    # Назавтра таймеру выпала меньшая случайная задержка, чем в прошлый раз.
    now = datetime(2026, 10, 12, 23, 0, 20)
    assert (now - last.finished_at).total_seconds() / 3600 < site_cadence_hours("aloe")

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)

    assert due == ["aloe"]
    assert covered == {}


def test_second_alarm_of_the_night_skips_a_collected_site(db_session):
    """Повтор в 03:15 после удачного сбора в 01:00 не делает ничего."""
    alarms, _ = _timer_alarms(SCRAPE_TIMER)
    (first_hour, first_minute), (retry_hour, retry_minute) = alarms
    night = date(2026, 10, 14)
    last = _full_run(
        db_session, "aptekonline", started_at=_timer_fire_utc(night, first_hour, first_minute)
    )
    retry = _timer_fire_utc(night, retry_hour, retry_minute) + timedelta(seconds=40)

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=retry)

    assert due == []
    assert covered["aptekonline"] == pytest.approx(
        (retry - last.finished_at).total_seconds() / 3600
    )


@pytest.mark.parametrize("incomplete", [None, "failed", "running", "unfinished", "ineligible"])
def test_identity_partition_closes_only_its_completed_cadence_window(db_session, incomplete):
    alarms, _ = _timer_alarms(SCRAPE_TIMER)
    (first_hour, first_minute), (retry_hour, retry_minute) = alarms
    night = date(2026, 10, 14)
    run = _full_run(
        db_session,
        "pharmonline",
        started_at=_timer_fire_utc(night, first_hour, first_minute),
        status="degraded",
    )
    run.catalog_verified = False
    run.run_quality = {
        "financially_eligible": incomplete != "ineligible",
        "identity_partition": {
            "version": 1,
            "site": "pharmonline",
            "source_complete": True,
            "total_count": 9430,
            "trusted_count": 9427,
            "isolated_count": 3,
            "excluded_product_count": 3,
        },
    }
    if incomplete in {"failed", "running"}:
        run.status = incomplete
    if incomplete == "unfinished":
        run.finished_at = None
    db_session.commit()
    retry = _timer_fire_utc(night, retry_hour, retry_minute)

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["pharmonline"], now=retry)

    if incomplete is None:
        assert due == []
        assert covered["pharmonline"] == pytest.approx(
            (retry - run.finished_at).total_seconds() / 3600
        )
    else:
        assert due == ["pharmonline"]
        assert covered == {}
    # Isolation closes this night's retry, never the next night's normal scan.
    due, covered = main_mod._sites_due_for_full_scan(
        db_session, ["pharmonline"], now=retry + timedelta(days=1)
    )
    assert due == ["pharmonline"]
    assert covered == {}


@pytest.mark.parametrize("status", ["failed", "degraded"])
def test_second_alarm_of_the_night_retries_a_site_whose_run_did_not_succeed(db_session, status):
    """Ради этого второй будильник и стоит: неудачный сбор повторяется до утра."""
    alarms, _ = _timer_alarms(SCRAPE_TIMER)
    (first_hour, first_minute), (retry_hour, retry_minute) = alarms
    night = date(2026, 10, 14)
    _full_run(
        db_session,
        "aptekonline",
        started_at=_timer_fire_utc(night, first_hour, first_minute),
        status=status,
    )
    retry = _timer_fire_utc(night, retry_hour, retry_minute)

    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=retry)

    assert due == ["aptekonline"]


def test_nightly_alarms_collect_exactly_once_a_night(db_session):
    """Шесть недель ночных будильников, как на проде: один сбор за ночь.

    Каждую ночь — оба будильника из файла таймера, по времени хоста, со
    случайной задержкой. Внутри отрезка — переход на зимнее время 25 октября:
    будильники сдвигаются на час по UTC, ритм не должен ни удвоиться, ни
    пропустить ночь, а данные — ни разу стать «просроченными» между сборами.
    """
    alarms, _ = _timer_alarms(SCRAPE_TIMER)
    _full_run(db_session, "aloe", started_at=datetime(2026, 10, 6, 23, 3, 13))
    collected: list[storage.Run] = []

    for offset in range(42):
        day = date(2026, 10, 8) + timedelta(days=offset)
        for number, (hour, minute) in enumerate(alarms):
            jitter = timedelta(seconds=(offset * 97 + number * 131) % 300)
            now = _timer_fire_utc(day, hour, minute) + jitter
            due, _ = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)
            if due:
                collected.append(
                    _full_run(db_session, "aloe", started_at=now, minutes=30 + offset % 6)
                )

    nights = [
        run.started_at.replace(tzinfo=timezone.utc).astimezone(BERLIN).date() for run in collected
    ]
    assert nights == [date(2026, 10, 8) + timedelta(days=offset) for offset in range(42)]
    # Собирает всегда первый будильник ночи — повтору достаётся только сбой.
    assert {
        run.started_at.replace(tzinfo=timezone.utc).astimezone(BERLIN).hour for run in collected
    } == {alarms[0][0]}
    for previous, current in zip(collected, collected[1:]):
        gap_hours = (current.finished_at - previous.finished_at).total_seconds() / 3600
        assert gap_hours <= site_max_age_hours("aloe")


@pytest.mark.parametrize("days_later", [1, 2, 3, 4, 5, 6])
def test_weekly_site_skips_nightly_wakeups_inside_the_week(db_session, weekly_aloe, days_later):
    """Недельный сайт: со второго по седьмой будильник той же недели — уже собран."""
    last = _full_run(db_session, "aloe", started_at=datetime(2026, 10, 11, 23, 3, 13))
    now = datetime(2026, 10, 11, 23, 0, 20) + timedelta(days=days_later)

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)

    assert due == []
    assert covered["aloe"] == pytest.approx((now - last.finished_at).total_seconds() / 3600)


def test_weekly_site_is_collected_exactly_once_a_week(db_session, weekly_aloe):
    """Недельный сайт под суточным будильником — шесть недель, шесть сборов.

    Отправная точка — реальный последний сбор с прода (прогон 987). Внутри
    отрезка — переход на зимнее время: ритм не должен ни удвоиться, ни
    пропустить неделю. Седьмой день в то же время суток — собираем: по правилу
    «прошло ≥168 ч» сбор уезжал на восьмой день, а порог «данные устарели»
    (174 ч) срабатывал раньше.
    """
    _full_run(db_session, "aloe", started_at=datetime(2026, 10, 6, 23, 3, 13))
    collected: list[storage.Run] = []

    for offset in range(42):
        day = date(2026, 10, 8) + timedelta(days=offset)
        jitter = timedelta(seconds=(offset * 97) % 300)
        now = _timer_fire_utc(day, hour=1) + jitter
        due, _ = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)
        if due:
            collected.append(_full_run(db_session, "aloe", started_at=now, minutes=30 + offset % 6))

    assert [run.started_at.date() for run in collected] == [
        date(2026, 10, 11),  # 23:0x UTC = понедельник 01:0x по хосту (летнее время)
        date(2026, 10, 18),
        date(2026, 10, 26),  # 00:0x UTC = понедельник 01:0x (уже зимнее)
        date(2026, 11, 2),
        date(2026, 11, 9),
        date(2026, 11, 16),
    ]
    assert {_berlin_weekday(run.started_at) for run in collected} == {0}
    for previous, current in zip(collected, collected[1:]):
        gap_hours = (current.finished_at - previous.finished_at).total_seconds() / 3600
        assert gap_hours <= site_max_age_hours("aloe")


def test_weekly_site_is_not_skipped_after_midweek_recovery(db_session, weekly_aloe):
    """Недельный сайт после сбора посреди недели.

    Понедельничный сбор упал, в среду его повторили вручную. Правило «168ч с
    прошлого сбора» пропустило бы следующий понедельник (прошло 4,5 дня), и
    сбор вернулся бы только через неделю — 11 дней без данных. Окно ритма
    считает понедельник новой неделей.
    """
    _full_run(db_session, "aptekonline", started_at=datetime(2026, 10, 12, 0, 4), status="failed")
    _full_run(db_session, "aptekonline", started_at=datetime(2026, 10, 14, 9, 0))

    sunday_night = _timer_fire_utc(date(2026, 10, 18), hour=1)
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=sunday_night)
    assert due == []

    next_monday = _timer_fire_utc(date(2026, 10, 19), hour=1) + timedelta(seconds=240)
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=next_monday)

    assert due == ["aptekonline"]


def test_manual_collection_right_after_window_opens_keeps_the_next_night_fresh(db_session):
    """Худший случай для порога «данные устарели».

    Сбор вручную через секунды после открытия окна закрывает ночь: оба её
    будильника сайт пропускают, и следующий плановый сбор приходит через сутки
    с лишним. Берём зимнее время (будильник на час позже по UTC), полную
    случайную задержку, сайт — последний в очереди из трёх (общий замок, вся
    очередь около 70 минут) и прогон длиннее обычного.
    """
    alarms, delay_sec = _timer_alarms(SCRAPE_TIMER)
    first_hour, first_minute = alarms[0]
    night = date(2026, 11, 9)
    opened = cadence_window_start("aptekonline", _timer_fire_utc(night, first_hour, first_minute))
    early = _full_run(db_session, "aptekonline", started_at=opened + timedelta(seconds=5))

    for hour, minute in alarms:
        now = _timer_fire_utc(night, hour, minute)
        due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=now)
        assert due == [], (hour, minute)

    next_night = _timer_fire_utc(night + timedelta(days=1), first_hour, first_minute) + timedelta(
        seconds=delay_sec
    )
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=next_night)
    assert due == ["aptekonline"]
    refreshed_by = next_night + timedelta(minutes=70 + 45)
    gap_hours = (refreshed_by - early.started_at).total_seconds() / 3600
    assert gap_hours <= site_max_age_hours("aptekonline")


def test_manual_collection_in_the_daytime_does_not_cancel_the_night(db_session):
    """Кнопка «Запустить scrape» днём по Баку ночной сбор не отменяет.

    Окно открывается в 22:00 UTC (02:00 Баку): всё, что собрано раньше,
    принадлежит вчерашнему окну.
    """
    alarms, _ = _timer_alarms(SCRAPE_TIMER)
    night = date(2026, 10, 15)
    first = _timer_fire_utc(night, *alarms[0])
    _full_run(db_session, "aloe", started_at=first - timedelta(hours=9))  # 14:00 UTC, 18:00 Баку

    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=first)

    assert due == ["aloe"]


def test_run_started_before_the_window_does_not_cover_it(db_session):
    """Неделю закрывает сбор, который в ней НАЧАЛСЯ.

    Сбор, начатый до границы и законченный после неё, несёт данные прошлой
    недели — понедельничный таймер собирает заново.
    """
    boundary = datetime(2026, 10, 11, 22, 0)
    straddling = _full_run(
        db_session, "aloe", started_at=boundary - timedelta(minutes=10), minutes=40
    )
    assert straddling.finished_at > boundary

    monday_timer = _timer_fire_utc(date(2026, 10, 12), hour=1)
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=monday_timer)

    assert due == ["aloe"]


def test_failed_attempt_does_not_block_retry(db_session):
    """Упавший сбор не должен запирать сайт до конца недели."""
    now = datetime(2026, 10, 13, 23, 1)
    _full_run(db_session, "aloe", started_at=now - timedelta(hours=24), status="failed")

    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)

    assert due == ["aloe"]


def test_site_without_any_full_run_is_due(db_session):
    """Сайт, который ещё ни разу не собирали целиком, собираем сразу."""
    due, covered = main_mod._sites_due_for_full_scan(
        db_session, ["pharmonline"], now=datetime(2026, 10, 13, 23, 1)
    )

    assert due == ["pharmonline"]
    assert covered == {}


def test_due_and_covered_sites_are_separated(db_session):
    """Один прогон на три сайта собирает только те, у которых началось новое окно."""
    now = datetime(2026, 10, 13, 23, 31)
    _full_run(db_session, "aloe", started_at=now - timedelta(minutes=30))
    _full_run(db_session, "aptekonline", started_at=now - timedelta(hours=24))

    due, covered = main_mod._sites_due_for_full_scan(
        db_session, ["aloe", "aptekonline", "pharmonline"], now=now
    )

    assert sorted(due) == ["aptekonline", "pharmonline"]
    assert list(covered) == ["aloe"]


# ─── Какие формы запуска подчиняются ритму ───────────────────────────────────


def test_scheduled_full_scan_predicate():
    """Ритму подчиняется полный сбор без явной просьбы человека."""
    assert main_mod._is_scheduled_full_scan(
        requested_mode="auto", is_full_catalog=True, dry_run=False
    )
    assert main_mod._is_scheduled_full_scan(
        requested_mode="category", is_full_catalog=True, dry_run=False
    )
    # Частичные прогоны: выбранная категория, --limit, watchlist- и hourly-тики.
    assert not main_mod._is_scheduled_full_scan(
        requested_mode="auto", is_full_catalog=False, dry_run=False
    )
    assert not main_mod._is_scheduled_full_scan(
        requested_mode="auto", is_full_catalog=True, dry_run=True
    )
    # Кнопка «Запустить scrape» в дашборде — всегда собираем, иначе запрос
    # навсегда зависнет в running (регрессия 2026-06-11).
    assert not main_mod._is_scheduled_full_scan(
        requested_mode="category", is_full_catalog=True, dry_run=False, request_id=42
    )
    # Явный --mode public_api — guarded workflow со своим недельным cron и
    # проверкой, что после запуска появился новый прогон.
    assert not main_mod._is_scheduled_full_scan(
        requested_mode="public_api", is_full_catalog=True, dry_run=False
    )


class _ScrapeReached(Exception):
    """Сбор дошёл до скрейпера — дальше пайплайн тесту не нужен."""


@pytest.fixture
def scrape_calls(db_session, monkeypatch, tmp_path):
    """`run` на тестовой БД; скрейперы подменены и записывают, что их позвали."""
    calls: list[tuple[str, list[str]]] = []
    # Одни часы на подготовку данных и на сам `run`: иначе тест, попавший на
    # секунду открытия окна, увидел бы «собрано на этой неделе» как прошлую.
    frozen = utcnow()
    monkeypatch.setattr(main_mod, "utcnow", lambda: frozen)

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        calls.append(("catalog", sorted(sites_with_slugs)))
        raise _ScrapeReached

    async def fake_scrape_watchlist_all(urls_by_site):
        calls.append(("watchlist", sorted(urls_by_site)))
        raise _ScrapeReached

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {})
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "scrape_watchlist_all", fake_scrape_watchlist_all)
    # Автономный режим pharmonline пишет в os.environ напрямую. Ключи надо
    # зарегистрировать через setenv: delenv(..., raising=False) отсутствующий
    # ключ не запоминает, и запись утекла бы в следующие тесты.
    for name in (
        "PHARMONLINE_PUBLIC_API",
        "PHARMONLINE_PUBLIC_API_TRANSPORT",
        "PHARMONLINE_PUBLIC_API_REQUIRE_CATALOG_BASELINE",
        "PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER",
        "PHARMONLINE_LEGACY_ID_BRIDGE",
        "PHARMONLINE_USE_DDP",
        "PHARMONLINE_DECODO_BACKCONNECT_STICKY",
        "AI_FALLBACK_ENABLED",
        "SCRAPE_REPORT_EMAIL",
    ):
        monkeypatch.setenv(name, "")
    # Маркер автономного режима по умолчанию ищется в /opt — на хосте, где он
    # есть, тест не должен зависеть от него.
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(tmp_path / "no-marker"))
    return calls


def _collected_this_week(db_session, site: str) -> None:
    """Сайт уже собран в текущем окне ритма (часы — те же, что у `run`)."""
    opened = cadence_window_start(site, main_mod.utcnow())
    _full_run(db_session, site, started_at=opened, minutes=0)


def _collected_last_week(db_session, site: str) -> None:
    _full_run(db_session, site, started_at=main_mod.utcnow() - timedelta(days=8))


def _run_count(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(storage.Run))


def test_repo_unit_and_dropin_call_the_same_command():
    """Базовый юнит и его drop-in зовут одно и то же — проверяем одну форму."""
    assert _unit_run_args(SCRAPE_UNIT, "aloe") == ["run", "--site", "aloe"]
    assert _unit_run_args(SCRAPE_UNIT_DROPIN, "aloe") == _unit_run_args(SCRAPE_UNIT, "aloe")


@pytest.mark.parametrize("site", ["aloe", "aptekonline", "pharmonline"])
def test_systemd_unit_command_is_skipped_inside_the_week(db_session, scrape_calls, site):
    """Команда из юнита, сайт на этой неделе уже собран — сбора нет.

    Ровно это не работало на проде: юнит зовёт `run --site %i` без `--mode`,
    режим `auto` превращался в полный сбор уже после гварда.
    """
    _collected_this_week(db_session, site)
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, site))

    assert result.exit_code == 0, result.output
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before
    # Человек, запустивший ту же команду руками, должен увидеть, что сбора не было.
    assert "полный сбор пропущен" in result.output
    assert "--force" in result.output


def test_systemd_unit_command_collects_in_a_new_week(db_session, scrape_calls):
    """Та же команда, прошлый сбор — на прошлой неделе: собираем полный каталог."""
    _collected_last_week(db_session, "aloe")

    result = CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, "aloe"))

    assert isinstance(result.exception, SystemExit) and result.exit_code != 0
    assert scrape_calls == [("catalog", ["aloe"])]
    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert (run.catalog_scope, run.full_catalog_sites) == ("full", "aloe")


def _enable_pharmonline_marker(monkeypatch, tmp_path) -> None:
    """Как на проде: маркер на месте, транспорт выбран в EnvironmentFile."""
    marker = tmp_path / "pharmonline-public-api-autonomous-v1"
    marker.write_text(main_mod._PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER_CONTENT, encoding="utf-8")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(marker))
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "direct")


def test_pharmonline_timer_in_autonomous_mode_is_skipped_inside_the_week(
    db_session, scrape_calls, monkeypatch, tmp_path
):
    """Легаси-таймер pharmonline: маркер уводит `auto` в public_api — это тоже полный сбор."""
    _enable_pharmonline_marker(monkeypatch, tmp_path)
    _collected_this_week(db_session, "pharmonline")
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, "pharmonline"))

    assert result.exit_code == 0, result.output
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before


def test_pharmonline_timer_in_autonomous_mode_collects_in_a_new_week(
    db_session, scrape_calls, monkeypatch, tmp_path
):
    _enable_pharmonline_marker(monkeypatch, tmp_path)
    _collected_last_week(db_session, "pharmonline")

    CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, "pharmonline"))

    assert scrape_calls == [("catalog", ["pharmonline"])]
    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert (run.catalog_scope, run.full_catalog_sites) == ("full", "pharmonline")


def test_explicit_public_api_workflow_is_never_skipped(db_session, scrape_calls, monkeypatch):
    """Недельный GitHub-cron pharmonline зовёт `--mode public_api` и ждёт новый прогон."""
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    _collected_this_week(db_session, "pharmonline")

    CliRunner().invoke(main_mod.cli, ["run", "--site", "pharmonline", "--mode", "public_api"])

    assert scrape_calls == [("catalog", ["pharmonline"])]


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--force"],
        ["--limit", "5"],
        ["--category-id", "7"],
        ["--dry-run"],
    ],
)
def test_explicit_human_forms_collect_inside_the_week(db_session, scrape_calls, extra_args):
    """Досрочный сбор, выбранная категория, `--limit`, dry-run — ритм не спорит."""
    _collected_this_week(db_session, "aloe")

    CliRunner().invoke(main_mod.cli, ["run", "--site", "aloe", *extra_args])

    assert scrape_calls == [("catalog", ["aloe"])]


def test_dashboard_button_collects_inside_the_week(db_session, scrape_calls):
    """Кнопка «Запустить scrape»: форма серверного watcher'а с `--request-id`."""
    _collected_this_week(db_session, "aloe")
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()

    CliRunner().invoke(
        main_mod.cli,
        [
            "run",
            "--mode",
            "category",
            "--no-alerts",
            "--request-id",
            str(request.id),
            "--site",
            "aloe",
        ],
    )

    assert scrape_calls == [("catalog", ["aloe"])]


def test_watchlist_tick_form_is_not_a_full_scan(db_session, scrape_calls):
    """Форма watchlist-тика: `run` с mode=watchlist и hourly — ритм полного сбора не про неё.

    Что `watchlist-tick` зовёт `run` именно так, закреплено в test_intraday.py.
    """
    _collected_this_week(db_session, "aloe")

    CliRunner().invoke(main_mod.cli, ["run", "--site", "aloe", "--mode", "watchlist", "--hourly"])

    assert scrape_calls == [("watchlist", [])]


# ─── Непустой watchlist ──────────────────────────────────────────────────────
#
# Режим по умолчанию раньше зависел от таблицы watchlist: одна подтверждённая
# ссылка на ЛЮБОМ сайте — и команда юнита для aloe и aptekonline становилась
# watchlist-прогоном. Полный каталог перестал бы собираться совсем: на сайте со
# ссылкой — молча (частичный прогон со статусом ok), на остальных — с упавшим
# прогоном каждую ночь. На проде это не стреляло только потому, что watchlist
# был пуст. Тесты выше идут с пустым watchlist и этого не видят.


def _pin_confirmed_url(db_session, site: str) -> None:
    """Закреплённый товар с подтверждённой ссылкой на сайте."""
    main_mod.watchlist.add_tracked_product(
        db_session,
        canonical_name=f"Закреплённый товар {site}",
        **{f"{site}_url": f"https://{site}.az/product/pinned"},
    )
    assert main_mod.collect_watchlist_urls(db_session)[site]


def _provision_run_args() -> list[str]:
    """Аргументы `pharmacy-monitor`, с которыми сбор зовёт юнит из provision_vps.sh."""
    commands = [
        shlex.split(line.removeprefix("ExecStart="))
        for line in PROVISION_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=") and line.rstrip().endswith(" pharmacy-monitor run")
    ]
    assert len(commands) == 1, f"{PROVISION_SCRIPT.name}: ожидался один юнит сбора, {commands}"
    argv = commands[0]
    return argv[argv.index("pharmacy-monitor") + 1 :]


def _latest_run(db_session) -> storage.Run:
    db_session.expire_all()
    return db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))


@pytest.mark.parametrize("site", ["aloe", "aptekonline", "pharmonline"])
@pytest.mark.parametrize("pinned_site", ["aloe", "aptekonline", "pharmonline"])
def test_systemd_unit_command_collects_full_catalog_with_pinned_watchlist(
    db_session, scrape_calls, site, pinned_site
):
    """Команда из юнита при непустом watchlist — всё равно полный сбор каталога."""
    _pin_confirmed_url(db_session, pinned_site)
    _collected_last_week(db_session, site)

    CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, site))

    assert scrape_calls == [("catalog", [site])]
    run = _latest_run(db_session)
    assert (run.catalog_scope, run.full_catalog_sites) == ("full", site)


def test_systemd_unit_command_with_pinned_watchlist_is_skipped_inside_the_week(
    db_session, scrape_calls
):
    """Внутри недели плановый запуск не превращается в watchlist-прогон.

    Закреплённые ссылки обновляет свой таймер (`watchlist-tick`), а не ночной
    сбор каталога: тот в неделю, когда сайт уже собран, не делает ничего.
    """
    _pin_confirmed_url(db_session, "aloe")
    _collected_this_week(db_session, "aloe")
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, "aloe"))

    assert result.exit_code == 0, result.output
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before


def test_pharmonline_timer_in_autonomous_mode_stays_on_public_api(
    db_session, scrape_calls, monkeypatch, tmp_path
):
    """Маркер переводит `auto` в public_api раньше, чем `auto` становится category.

    Поменять их местами — и легаси-таймер pharmonline молча уйдёт с проверенного
    публичного API на категорийный сбор: оба пути зовут один `scrape_all`, и
    отличает их только маршрут.
    """
    from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE

    routes: dict[str, list[str]] = {}

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        routes.update(sites_with_slugs)
        raise _ScrapeReached

    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    _enable_pharmonline_marker(monkeypatch, tmp_path)
    _pin_confirmed_url(db_session, "pharmonline")
    _collected_last_week(db_session, "pharmonline")

    CliRunner().invoke(main_mod.cli, _unit_run_args(SCRAPE_UNIT, "pharmonline"))

    assert routes == {"pharmonline": [PUBLIC_CATALOG_ROUTE]}
    assert _latest_run(db_session).catalog_scope == "full"


def test_provision_script_unit_calls_run_without_site_or_mode():
    """Форма легаси-юнита: правило «`--site` задан → полный сбор» её бы не закрыло."""
    assert _provision_run_args() == ["run"]


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(None, id="provision_vps-unit"),
        pytest.param(["run", "--mode", "auto"], id="explicit-auto"),
    ],
)
def test_default_mode_without_site_is_full_catalog_with_pinned_watchlist(
    db_session, scrape_calls, args
):
    """Остальные, кто зовёт режим по умолчанию: полный сбор всех сайтов."""
    _pin_confirmed_url(db_session, "aloe")

    CliRunner().invoke(main_mod.cli, args or _provision_run_args())

    assert scrape_calls == [("catalog", ["aloe", "aptekonline", "pharmonline"])]
    assert _latest_run(db_session).catalog_scope == "full"


@pytest.mark.parametrize(
    ("extra_args", "scope"),
    [
        (["--force"], "full"),
        (["--dry-run"], "full"),
        (["--limit", "5"], "partial"),
        (["--category-id", "7"], "partial"),
    ],
)
def test_explicit_human_forms_collect_catalog_with_pinned_watchlist(
    db_session, scrape_calls, extra_args, scope
):
    """Ручные формы без `--mode` при непустом watchlist — тоже сбор каталога."""
    _pin_confirmed_url(db_session, "aloe")
    _collected_this_week(db_session, "aloe")

    CliRunner().invoke(main_mod.cli, ["run", "--site", "aloe", *extra_args])

    assert scrape_calls == [("catalog", ["aloe"])]
    assert _latest_run(db_session).catalog_scope == scope


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--mode", "watchlist", "--hourly"], id="tick-form"),
        pytest.param(["--hourly"], id="hourly-alone"),
        pytest.param(["--mode", "watchlist"], id="explicit-mode"),
    ],
)
def test_explicit_watchlist_forms_still_scrape_pinned_urls(db_session, scrape_calls, args):
    """Watchlist-прогон остаётся — но только по явной просьбе."""
    _pin_confirmed_url(db_session, "aloe")
    _pin_confirmed_url(db_session, "aptekonline")  # чужой сайт в прогон не попадает
    _collected_this_week(db_session, "aloe")  # ритм полного сбора тику не указ

    CliRunner().invoke(main_mod.cli, ["run", "--site", "aloe", *args])

    assert scrape_calls == [("watchlist", ["aloe"])]
    run = _latest_run(db_session)
    assert (run.catalog_scope, run.full_catalog_sites) == ("partial", None)


def test_watchlist_tick_command_still_reaches_the_watchlist_scraper(db_session, scrape_calls):
    """Таймер закреплённых ссылок целиком: `watchlist-tick` → `run` → скрейпер ссылок."""
    _pin_confirmed_url(db_session, "aloe")
    _collected_this_week(db_session, "aloe")

    CliRunner().invoke(main_mod.cli, ["watchlist-tick"])

    assert scrape_calls == [("watchlist", ["aloe"])]
    assert _latest_run(db_session).catalog_scope == "partial"
