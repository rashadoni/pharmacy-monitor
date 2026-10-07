"""Этап сопоставления один — в конце сбора и в `rematch`; плановый юнит его не сбрасывает.

2026-10-07, после выкладки правок сопоставления (PR #21), выяснилось сразу два
расхождения между «как задумано» и «как на проде»:

* `rematch` делал только часть того, что делает конец сбора (без замены
  устаревших строк и без revalidate), а `intraday-tick` и пустой `watchlist-tick`
  сопоставление не запускают вовсе — «прогнать руками после выкладки» давало
  другой результат, а сами новые пары до понедельника не появились бы;
* на прод 2026-05-26 руками положили юнит `pharmacy-monitor-rematch` с
  `rematch --reset`: каждый понедельник все автоматические пары удалялись и
  собирались заново, хотя полный сброс на проде считался запрещённым. Заменён
  ли он, эти тесты не знают — они проверяют файлы в Git и саму команду.

Команда здесь берётся из самого файла юнита — как в test_cadence_guard.py.
"""

from __future__ import annotations

import ast
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from src import main as main_mod
from src import storage
from tests.test_matcher_coverage import _product, _stale_cluster
from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline, _session_factory

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "infra/systemd"
REMATCH_UNIT = SYSTEMD / "pharmacy-monitor-rematch.service"
REMATCH_TIMER = SYSTEMD / "pharmacy-monitor-rematch.timer"
REMATCH_DROPINS = sorted((SYSTEMD / "overrides").glob("pharmacy-monitor-rematch.service.d/*.conf"))
INSTALLER = REPO / "infra/scripts/install_systemd_schedule.sh"

STAGE = [
    "refresh_derived_fields",
    "relink_stale_members",
    "match_products",
    "revalidate_split",
    "flag_suspected_mismatches",
]
_EMPTY_RESULT = {
    "refresh_derived_fields": 0,
    "relink_stale_members": [],
    "match_products": 0,
    "revalidate_split": [],
    "flag_suspected_mismatches": 0,
}


def _record_stage(monkeypatch, *, failing: dict[str, Exception] | None = None) -> list[tuple]:
    """Подменить шаги этапа записью вызовов: (имя шага, именованные аргументы)."""
    calls: list[tuple] = []
    for name in STAGE:

        def step(session, _name=name, **kwargs):
            calls.append((_name, kwargs))
            if failing and _name in failing:
                raise failing[_name]
            return _EMPTY_RESULT[_name]

        monkeypatch.setattr(main_mod.matcher, name, step)
    return calls


def _steps(calls: list[tuple]) -> list[str]:
    return [name for name, _ in calls]


def _rematch(db_session, monkeypatch, *args: str):
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    return CliRunner().invoke(main_mod.cli, ["rematch", *args])


def _two_clusters(db_session) -> tuple[int, int, list, list]:
    """Автоматическая и ручная пара: (id авто, id ручной, товары авто, товары ручной)."""
    auto = storage.Match(tenant_id=1, canonical_name="Auto", confidence=0.9, is_manual=False)
    manual = storage.Match(tenant_id=1, canonical_name="Manual", confidence=1.0, is_manual=True)
    db_session.add_all([auto, manual])
    db_session.flush()
    paired_auto = [
        _product(db_session, site, "Ferrovef N60", canonical_id=auto.id)
        for site in ("pharmonline", "aptekonline")
    ]
    paired_manual = [
        _product(db_session, site, "Kreon 10000 N20", canonical_id=manual.id)
        for site in ("pharmonline", "aptekonline")
    ]
    db_session.commit()
    return auto.id, manual.id, paired_auto, paired_manual


# ── Юнит ─────────────────────────────────────────────────────────────────────


def _unit_lines(*unit_files: Path) -> list[str]:
    return [
        line.strip()
        for unit_file in unit_files
        for line in unit_file.read_text(encoding="utf-8").splitlines()
    ]


def _exec_args(*unit_files: Path) -> list[str]:
    commands: list[str] = []
    for line in _unit_lines(*unit_files):
        if line.startswith("ExecStart="):
            value = line.removeprefix("ExecStart=").strip()
            # Пустое значение в drop-in сбрасывает список, как в systemd.
            commands = [*commands, value] if value else []
    assert len(commands) == 1, f"ожидался один ExecStart, найдено {commands}"
    argv = shlex.split(commands[0])
    assert argv[0].endswith("/pharmacy-monitor")
    return argv[1:]


def test_scheduled_rematch_unit_runs_the_incremental_stage_only():
    """Ни `--reset`, ни других флагов: юнит зовёт обычный этап сопоставления."""
    assert _exec_args(REMATCH_UNIT, *REMATCH_DROPINS) == ["rematch"]
    # Сброс не спрятан и в соседних командах юнита.
    other_commands = [
        line
        for line in _unit_lines(REMATCH_UNIT, *REMATCH_DROPINS)
        if line.startswith(("ExecStartPre=", "ExecStartPost=", "ExecStop"))
    ]
    assert other_commands == []


def test_scheduled_rematch_unit_cannot_hold_the_matcher_lock_forever():
    """У oneshot без TimeoutStartSec предела нет: зависшая команда держала бы замок."""
    timeouts = [
        line.removeprefix("TimeoutStartSec=")
        for line in _unit_lines(REMATCH_UNIT, *REMATCH_DROPINS)
        if line.startswith("TimeoutStartSec=")
    ]
    assert timeouts and 0 < int(timeouts[-1]) <= 3600


def test_rematch_timer_fires_after_the_monday_scans_and_has_a_second_slot():
    """При занятом замке команда выходит без работы, и пропуск никто не повторяет.

    Поэтому не понедельник (идут недельные сборы) и не один день в неделю.
    """
    schedule = [
        line.removeprefix("OnCalendar=")
        for line in _unit_lines(REMATCH_TIMER)
        if line.startswith("OnCalendar=")
    ]
    assert schedule == ["Tue,Fri *-*-* 04:30:00"]


def test_schedule_installer_installs_the_versioned_rematch_unit():
    """Юнит лежал на проде мимо Git — так `--reset` и прожил четыре месяца."""
    script = INSTALLER.read_text(encoding="utf-8")
    install_list, rest = script.split("systemctl daemon-reload")
    enable_list = rest.split("systemctl list-timers")[0]
    assert "pharmacy-monitor-rematch.service" in install_list
    assert "pharmacy-monitor-rematch.timer" in install_list
    assert "pharmacy-monitor-rematch.timer" in enable_list
    # Проверяется то, что systemd реально запустит (с местными drop-in), а не файл.
    assert "systemctl show --property ExecStart --value pharmacy-monitor-rematch.service" in rest
    assert "grep -q -- '--reset'" in rest


# ── Где этап вызывается ──────────────────────────────────────────────────────


def _functions_calling(path: Path, callee: str) -> set[str]:
    """Имена функций и методов (любой вложенности), в теле которых есть вызов `callee`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == callee:
                    found.add(node.name)
    return found


def test_the_stage_has_exactly_two_entry_points():
    """Сопоставление запускают `run` и `rematch`; `scrape` и `intraday-tick` — нет.

    Запись «новые пары появятся с ближайшим тиком» в документации PR #21 была
    неверной: тик зовёт `scrape`, а тот до сопоставления не доходит.
    """
    src = REPO / "src"
    assert _functions_calling(src / "main.py", "_run_matching_stage") == {
        "run_cmd",
        "rematch_cmd",
    }
    callers = {
        (path.name, name)
        for path in sorted(src.rglob("*.py"))
        for name in _functions_calling(path, "match_products")
    }
    assert callers == {("main.py", "_run_matching_stage")}


def _run(db_session, monkeypatch, *extra: str):
    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    calls = _record_stage(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli, ["run", "--site", "aloe", "--mode", "category", "--no-alerts", *extra]
        )
    assert result.exit_code == 0, result.output
    return calls


def test_full_run_executes_the_same_stage_as_rematch(db_session, monkeypatch):
    calls = _run(db_session, monkeypatch)

    assert _steps(calls) == STAGE
    assert dict(calls)["match_products"] == {}  # порог — тот, что в матчере


def test_bounded_run_reaches_the_stage_too(db_session, monkeypatch):
    """Не только полный сбор: кнопка в дашборде и `run --limit` тоже сопоставляют.

    Поэтому при откате правок сопоставления останавливают и очередь кнопки
    (`pharmacy-monitor-scrape-watcher`), а не только ночные сборы.
    """
    assert _steps(_run(db_session, monkeypatch, "--limit", "1")) == STAGE


# ── Команда rematch ──────────────────────────────────────────────────────────


def test_rematch_runs_every_step_of_the_stage_in_order(db_session, monkeypatch):
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert _steps(calls) == STAGE
    # Без --threshold порог не передаётся: действует тот, что в матчере.
    assert dict(calls)["match_products"] == {}


def test_rematch_threshold_reaches_the_matcher(db_session, monkeypatch):
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch, "--threshold", "90")

    assert result.exit_code == 0, result.output
    assert dict(calls)["match_products"] == {"fuzzy_threshold": 90}


def test_rematch_gives_the_cluster_slot_to_the_live_twin(db_session, monkeypatch):
    """Настоящие шаги, без подмены: прежний `rematch` устаревшие строки не заменял."""
    match, old, partner, twin = _stale_cluster(db_session, twin_url_same=True)

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Stale members relinked: 1." in result.output
    for product in (old, partner, twin):
        db_session.refresh(product)
    assert (old.canonical_id, twin.canonical_id, partner.canonical_id) == (None, match.id, match.id)


def test_rematch_steps_aside_while_a_scrape_run_is_active(db_session, monkeypatch):
    """Сбор и сопоставление правят одни строки товаров — одновременно не идут."""
    calls = _record_stage(monkeypatch)
    matcher_lock: list[str] = []
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, *, wait: False
    )
    # Короткого читателя каталога rematch пережидает до двух минут настоящего
    # времени; здесь блокировка «занята» сбором, ждать её в тесте незачем.
    monkeypatch.setattr(main_mod, "_SCRAPE_LOCK_READER_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(
        main_mod, "_acquire_matcher_lock", lambda *a, **kw: matcher_lock.append("taken") or True
    )

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "skipped because the run lock is busy" in result.output
    assert calls == [] and matcher_lock == []


def test_rematch_does_not_wait_for_the_scrape_lock(db_session, monkeypatch):
    """Ждать нельзя: плановый юнит провисел бы весь многочасовой сбор."""
    waits: list[bool] = []
    _record_stage(monkeypatch)
    monkeypatch.setattr(
        main_mod,
        "_hold_scrape_lock_until_command_exit",
        lambda factory, *, wait: waits.append(wait) or True,
    )

    assert _rematch(db_session, monkeypatch).exit_code == 0
    assert waits == [False]


@pytest.mark.parametrize(
    ("args", "takes_lock"),
    [
        (("--revalidate",), True),
        (("--revalidate", "--dry-run"), False),
        (("--relink-dead",), True),
        (("--relink-dead", "--dry-run"), False),
    ],
)
def test_only_writing_modes_take_the_run_lock(db_session, monkeypatch, args, takes_lock):
    """Просмотр (`--dry-run`) не должен отказывать из-за идущего сбора."""
    waits: list[bool] = []
    _record_stage(monkeypatch)
    monkeypatch.setattr(main_mod.matcher, "relink_dead_members", lambda session, dry_run: [])
    monkeypatch.setattr(
        main_mod,
        "_hold_scrape_lock_until_command_exit",
        lambda factory, *, wait: waits.append(wait) or True,
    )

    assert _rematch(db_session, monkeypatch, *args).exit_code == 0
    assert waits == ([False] if takes_lock else [])


def test_rematch_steps_aside_while_another_matching_stage_runs(db_session, monkeypatch):
    calls = _record_stage(monkeypatch)
    released: list[str] = []
    monkeypatch.setattr(main_mod, "_acquire_matcher_lock", lambda *a, **kw: False)
    monkeypatch.setattr(main_mod, "_release_matcher_lock", lambda s: released.append("released"))

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "already running; skipped" in result.output
    assert calls == [] and released == []  # чужой замок не снимаем


def test_matcher_lock_is_released_when_the_stage_fails(db_session, monkeypatch):
    _record_stage(monkeypatch, failing={"match_products": ValueError("boom")})
    released: list[str] = []
    monkeypatch.setattr(main_mod, "_acquire_matcher_lock", lambda *a, **kw: True)
    monkeypatch.setattr(main_mod, "_release_matcher_lock", lambda s: released.append("released"))

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code != 0
    assert released == ["released"]


@pytest.mark.parametrize("step", ["refresh_derived_fields", "relink_stale_members"])
def test_failed_preparation_step_does_not_cancel_matching_but_fails_the_command(
    db_session, monkeypatch, step
):
    calls = _record_stage(monkeypatch, failing={step: ValueError("boom")})

    result = _rematch(db_session, monkeypatch)

    assert _steps(calls) == STAGE  # этап дошёл до конца
    assert "FAILED, see log" in result.output
    # …но плановый юнит не должен отчитаться успехом.
    assert result.exit_code != 0
    assert "preparation step failed" in result.output


def test_failed_preparation_step_is_rolled_back_alone(db_session, monkeypatch):
    """Точки сохранения: запись упавшего шага откатывается, запись соседнего — нет."""
    product = _product(db_session, "pharmonline", "Kreon 10000 N20")
    product.name_normalized = "устарело"
    db_session.commit()

    def broken_relink(session, **kwargs):
        victim = session.get(storage.Product, product.id)
        victim.brand = "записано упавшим шагом"
        session.flush()
        raise ValueError("boom")

    monkeypatch.setattr(main_mod.matcher, "relink_stale_members", broken_relink)

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code != 0 and "relinked" in result.output
    db_session.expire_all()
    saved = db_session.get(storage.Product, product.id)
    assert saved.name_normalized == "kreon 10000"  # пересчёт полей сохранён
    assert saved.brand == "Kreon"  # запись упавшего шага откатилась


def test_failed_revalidation_fails_the_command(db_session, monkeypatch):
    """После сбоя revalidate в базе могут остаться пары, которые правила запрещают."""
    calls = _record_stage(monkeypatch, failing={"revalidate_split": ValueError("split failed")})

    result = _rematch(db_session, monkeypatch)

    assert result.exit_code != 0
    assert "identity revalidation failed" in result.output
    assert "flag_suspected_mismatches" not in _steps(calls)


# ── Полный сброс ─────────────────────────────────────────────────────────────


def test_reset_alone_refuses_and_touches_nothing(db_session, monkeypatch):
    """Так старый юнит с `rematch --reset` падает, а не стирает пары молча."""
    auto_id, manual_id, paired_auto, paired_manual = _two_clusters(db_session)
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch, "--reset")

    assert result.exit_code == 2
    assert main_mod.REMATCH_RESET_CONFIRM_FLAG in result.output
    assert calls == []
    db_session.expire_all()
    assert db_session.get(storage.Match, auto_id) is not None
    assert [p.canonical_id for p in paired_auto] == [auto_id, auto_id]
    assert [p.canonical_id for p in paired_manual] == [manual_id, manual_id]


def test_confirmed_reset_drops_automatic_pairs_keeps_manual_ones_then_runs_the_stage(
    db_session, monkeypatch
):
    auto_id, manual_id, paired_auto, paired_manual = _two_clusters(db_session)
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch, "--reset", main_mod.REMATCH_RESET_CONFIRM_FLAG)

    assert result.exit_code == 0, result.output
    assert "Reset 1 auto-matches." in result.output
    assert _steps(calls) == STAGE
    db_session.expire_all()
    assert db_session.get(storage.Match, auto_id) is None
    assert db_session.get(storage.Match, manual_id) is not None
    assert [p.canonical_id for p in paired_auto] == [None, None]
    assert [p.canonical_id for p in paired_manual] == [manual_id, manual_id]


def test_confirmation_flag_alone_is_a_mistake_not_a_plain_rematch(db_session, monkeypatch):
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch, main_mod.REMATCH_RESET_CONFIRM_FLAG)

    assert result.exit_code == 2 and calls == []


def test_standalone_full_rematch_script_refuses_without_confirmation(tmp_path):
    """`scripts/full_rematch.py` — второй путь к полному сбросу, мимо команды."""
    database = tmp_path / "must-not-be-created.sqlite"
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/full_rematch.py")],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"DATABASE_URL": f"sqlite:///{database}", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )

    assert result.returncode == 2
    assert main_mod.REMATCH_RESET_CONFIRM_FLAG in result.stderr
    assert not database.exists()


@pytest.mark.parametrize("extra", [(), ("--reset", "--i-accept-full-rebuild")])
def test_dry_run_is_refused_where_it_would_write(db_session, monkeypatch, extra):
    """Раньше `rematch --reset --dry-run` молча выполнял настоящий сброс."""
    auto_id, _, paired_auto, _ = _two_clusters(db_session)
    calls = _record_stage(monkeypatch)

    result = _rematch(db_session, monkeypatch, "--dry-run", *extra)

    assert result.exit_code == 2
    assert calls == []
    db_session.expire_all()
    assert [p.canonical_id for p in paired_auto] == [auto_id, auto_id]
