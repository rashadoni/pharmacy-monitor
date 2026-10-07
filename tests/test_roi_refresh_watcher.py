"""Серверный watcher исполняет очередь пересчёта рекомендаций.

Заявку на пересчёт (`roi_refresh_requests`) никто, кроме watcher'а, не читает.
Если его тик перестанет звать команду — или выкладка перестанет привозить сам
скрипт, — заявки молча копятся, а рекомендации после смены порогов пропадают до
следующего полного сбора, как и было до очереди. Поэтому скрипт здесь
запускается по-настоящему, с подставными бинарями.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from src import main as main_mod
from src import storage

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "infra" / "server" / "watch-scrape-queue.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _tick(tmp_path: Path, *, job_active: bool, refresh_exit: int = 0, pending_scrape: str = "null"):
    """Один тик watcher'а в песочнице. Возвращает (вызовы CLI, stdout)."""
    venv_bin = tmp_path / "venv-bin"
    path_bin = tmp_path / "path-bin"
    venv_bin.mkdir()
    path_bin.mkdir()
    calls = tmp_path / "cli-calls.log"
    # CLI: записать аргументы; `roi refresh` выходит с заданным кодом.
    _stub(
        venv_bin / "pharmacy-monitor",
        f'echo "$*" >> "{calls}"\n[[ "$1 $2" == "roi refresh" ]] && exit {refresh_exit}\nexit 0\n',
    )
    (venv_bin / "python").symlink_to(sys.executable)
    _stub(path_bin / "pgrep", f"exit {0 if job_active else 1}\n")
    _stub(path_bin / "curl", f"echo '{{\"pending\": {pending_scrape}}}'\n")
    result = subprocess.run(
        ["/bin/bash", str(WATCHER)],
        env={
            "PATH": f"{path_bin}:{os.environ['PATH']}",
            "PHARMACY_API_KEY": "test-key",
            "PROJECT_DIR": str(tmp_path),
            "VENV_BIN": str(venv_bin),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return (calls.read_text().splitlines() if calls.exists() else []), result.stdout


def test_idle_tick_drains_the_refresh_queue(tmp_path):
    calls, _ = _tick(tmp_path, job_active=False)

    assert "roi refresh --pending" in calls


def test_tick_does_not_refresh_while_a_scrape_job_is_active(tmp_path):
    calls, output = _tick(tmp_path, job_active=True)

    assert calls == []
    assert "already active" in output


def test_failed_refresh_is_logged_and_does_not_stop_the_scrape_queue(tmp_path):
    calls, output = _tick(
        tmp_path,
        job_active=False,
        refresh_exit=1,
        pending_scrape='{"id": 7, "mode": "all", "category_id": null, "sites": ["aloe"]}',
    )

    assert "roi refresh --pending FAILED" in output
    assert calls.index("roi refresh --pending") < calls.index(
        "run --mode category --no-alerts --request-id 7 --site aloe"
    )


def test_cli_accepts_the_command_exactly_as_the_watcher_calls_it(monkeypatch, db_session, tmp_path):
    """Подставной бинарь принял бы любые аргументы — настоящий CLI нет."""
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: Session)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    calls, _ = _tick(tmp_path, job_active=False)
    (command,) = [call for call in calls if call.startswith("roi ")]

    result = CliRunner().invoke(main_mod.cli, command.split())

    assert result.exit_code == 0, result.output


def test_deploy_ships_the_watcher_after_the_code_it_calls():
    """`infra/server/` выкладка раньше не привозила вовсе — скрипт клали руками.

    Порядок тоже важен: скрипт, приехавший раньше `src/`, целую минуту звал бы
    команду, которой в старом коде ещё нет.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    steps = {step.get("name"): step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"]}
    rsync = next(run for name, run in steps.items() if name and name.startswith("Rsync code"))

    assert "infra/server/ pm@" in rsync
    assert rsync.index(" src/ pm@") < rsync.index("infra/server/ pm@")
    # Недоступный для записи каталог должен остановить выкладку до rsync кода.
    assert "/opt/pharmacy-monitor/infra/server" in steps["Preflight deployment paths are writable"]


@pytest.mark.parametrize("unit", ["pharmacy-monitor-scrape-watcher.service"])
def test_watcher_unit_runs_the_script_from_the_runtime_checkout(unit):
    """Поэтому правка скрипта доезжает rsync'ом и не требует действий под root."""
    text = (ROOT / "infra" / "systemd" / unit).read_text()

    assert "ExecStart=/bin/bash /opt/pharmacy-monitor/infra/server/watch-scrape-queue.sh" in text
