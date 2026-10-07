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


def _timer_schedule(timer_file: Path) -> tuple[int, int]:
    """(час срабатывания по времени хоста, RandomizedDelaySec) из файла таймера."""
    hours: list[int] = []
    delay: int | None = None
    for line in timer_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("OnCalendar="):
            value = line.removeprefix("OnCalendar=").strip()
            if not value:  # пустое значение в drop-in сбрасывает расписание
                hours = []
                continue
            clock = re.search(r"(\d{2}):00:00$", value)
            assert clock, f"{timer_file.name}: тест умеет только расписание на ровный час"
            hours.append(int(clock[1]))
        elif line.startswith("RandomizedDelaySec="):
            delay = int(line.removeprefix("RandomizedDelaySec="))
    assert len(hours) == 1 and delay is not None, timer_file
    return hours[0], delay


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


def _timer_fire_utc(day: date, hour: int) -> datetime:
    """Момент `OnCalendar=*-*-* HH:00:00` по времени прод-хоста, в naive UTC."""
    local = datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _berlin_weekday(moment_utc: datetime) -> int:
    return moment_utc.replace(tzinfo=timezone.utc).astimezone(BERLIN).weekday()


# ─── Окно ритма ──────────────────────────────────────────────────────────────


def test_weekly_window_opens_in_the_night_to_monday():
    """Неделя сбора открывается в воскресенье 22:00 UTC."""
    wednesday = datetime(2026, 10, 7, 12, 0)

    start = cadence_window_start("aloe", wednesday)

    assert start == datetime(2026, 10, 4, 22, 0)
    assert start.weekday() == 6


def test_window_boundary_belongs_to_the_new_window():
    boundary = datetime(2026, 10, 11, 22, 0)

    assert cadence_window_start("aloe", boundary) == boundary
    assert cadence_window_start("aloe", boundary - timedelta(seconds=1)) == datetime(
        2026, 10, 4, 22, 0
    )


def test_site_without_declared_cadence_gets_daily_windows():
    """Сайт вне карты ритма — суточный: окно открывается каждый вечер."""
    assert site_cadence_hours("unknown-site") == 24

    start = cadence_window_start("unknown-site", datetime(2026, 10, 7, 12, 0))

    assert start == datetime(2026, 10, 6, 22, 0)


def test_scrape_timers_in_repo_are_the_ones_the_boundary_was_chosen_for():
    assert _timer_schedule(SCRAPE_TIMER) == (1, 300)
    assert {path.parent.name: _timer_schedule(path) for path in SCRAPE_TIMER_DROPINS} == {
        "pharmacy-monitor-scrape@aptekonline.timer.d": (2, 300)
    }


@pytest.mark.parametrize(
    "timer_file",
    [SCRAPE_TIMER, *SCRAPE_TIMER_DROPINS],
    ids=lambda path: path.parent.name if path.suffix == ".conf" else path.name,
)
def test_window_boundary_fits_the_monday_night_timers(timer_file):
    """Граница окна стоит раньше ночных таймеров понедельника и рядом с ними.

    Часы и случайная задержка берутся из самих файлов таймеров: перенесёшь
    таймер, не пересчитав `CADENCE_ANCHOR_UTC`, — упадёт этот тест. Перебираем
    каждый понедельник трёх лет, то есть оба перевода часов.
    """
    hour, delay_sec = _timer_schedule(timer_file)
    jitter = timedelta(seconds=delay_sec)
    monday = date(2026, 1, 5)
    while monday < date(2029, 1, 1):
        fire = _timer_fire_utc(monday, hour)
        since_boundary = fire - cadence_window_start("aloe", fire)
        # Понедельничный запуск открывает новую неделю, а не дособирает старую.
        assert timedelta(hours=1) <= since_boundary < timedelta(hours=24), monday
        # Сбор вручную сразу после границы сдвигает следующий плановый на этот
        # зазор; с задержкой таймера и часом на разницу в длительности прогонов
        # он обязан уложиться в запас ритма.
        assert since_boundary + jitter <= timedelta(hours=CADENCE_GRACE_HOURS - 1), monday
        monday += timedelta(days=7)


# ─── Какие сайты пора собирать ───────────────────────────────────────────────


def test_same_weekday_and_hour_a_week_later_is_due(db_session):
    """7-й день в то же время суток — собираем.

    Прежнее правило «с конца прошлого сбора прошло ≥168ч» здесь отвечало «рано»:
    таймер срабатывает в то же время суток, сбор длится полчаса, значит на 7-й
    день возраст 167,4ч. Сбор уезжал на 8-й день, а порог «данные устарели»
    (174ч) успевал сработать раньше.
    """
    last = _full_run(db_session, "aloe", started_at=datetime(2026, 10, 11, 23, 3, 13))
    # Неделю спустя таймеру выпала меньшая случайная задержка, чем в прошлый раз.
    now = datetime(2026, 10, 18, 23, 0, 20)
    assert (now - last.finished_at).total_seconds() / 3600 < site_cadence_hours("aloe")

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)

    assert due == ["aloe"]
    assert covered == {}


@pytest.mark.parametrize("days_later", [1, 2, 3, 4, 5, 6])
def test_nightly_wakeups_inside_the_week_are_skipped(db_session, days_later):
    """Со второго по седьмой будильник той же недели — сайт уже собран."""
    last = _full_run(db_session, "aloe", started_at=datetime(2026, 10, 11, 23, 3, 13))
    now = datetime(2026, 10, 11, 23, 0, 20) + timedelta(days=days_later)

    due, covered = main_mod._sites_due_for_full_scan(db_session, ["aloe"], now=now)

    assert due == []
    assert covered["aloe"] == pytest.approx((now - last.finished_at).total_seconds() / 3600)


def test_nightly_timer_collects_exactly_once_a_week(db_session):
    """Шесть недель ночных срабатываний таймера, как на проде.

    Таймер aloe: каждую ночь в 01:00 по времени хоста + до 5 минут случайной
    задержки. Отправная точка — реальный последний сбор с прода (прогон 987).
    Внутри отрезка — переход на зимнее время 25 октября: таймер сдвигается на
    час по UTC, ритм не должен ни удвоиться, ни пропустить неделю.
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
    # Данные ни разу не успевают стать «просроченными» между двумя сборами.
    for previous, current in zip(collected, collected[1:]):
        gap_hours = (current.finished_at - previous.finished_at).total_seconds() / 3600
        assert gap_hours <= site_max_age_hours("aloe")


def test_weekly_timer_is_not_skipped_after_midweek_recovery(db_session):
    """Недельный таймер (aptekonline) после сбора посреди недели.

    Понедельничный сбор упал, в среду его повторили вручную. Правило «168ч с
    прошлого сбора» пропустило бы следующий понедельник (прошло 4,5 дня), и
    таймер вернулся бы только через неделю — 11 дней без данных. Окно ритма
    считает понедельник новой неделей.
    """
    _full_run(db_session, "aptekonline", started_at=datetime(2026, 10, 12, 0, 4), status="failed")
    _full_run(db_session, "aptekonline", started_at=datetime(2026, 10, 14, 9, 0))

    next_monday = _timer_fire_utc(date(2026, 10, 19), hour=2) + timedelta(seconds=240)
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=next_monday)

    assert due == ["aptekonline"]


def test_manual_collection_right_after_window_opens_keeps_next_week_fresh(db_session):
    """Худший случай для порога «данные устарели».

    Сбор вручную через секунды после открытия окна закрывает неделю, и
    следующий плановый приходит позже обычного на зазор между границей и
    таймером. Берём самый поздний таймер прода (aptekonline, 02:00 зимой),
    полную случайную задержку и прогон вдвое длиннее обычного.
    """
    this_monday = _timer_fire_utc(date(2026, 11, 9), hour=2)
    opened = cadence_window_start("aptekonline", this_monday)
    early = _full_run(db_session, "aptekonline", started_at=opened + timedelta(seconds=5))

    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=this_monday)
    assert due == []

    next_monday = _timer_fire_utc(date(2026, 11, 16), hour=2) + timedelta(seconds=300)
    due, _ = main_mod._sites_due_for_full_scan(db_session, ["aptekonline"], now=next_monday)
    assert due == ["aptekonline"]
    refreshed_by = next_monday + timedelta(minutes=65)
    gap_hours = (refreshed_by - early.started_at).total_seconds() / 3600
    assert gap_hours <= site_max_age_hours("aptekonline")


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
    """Один прогон на три сайта собирает только те, у которых началась новая неделя."""
    now = datetime(2026, 10, 13, 23, 1)
    _full_run(db_session, "aloe", started_at=now - timedelta(hours=24))
    _full_run(db_session, "aptekonline", started_at=now - timedelta(days=8))

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
