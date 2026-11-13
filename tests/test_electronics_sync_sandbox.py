"""`infra/electronics/sync.sh` исполняется в песочнице: настоящий bash и rsync.

На сервере сценарий запускает root, и его ошибку видно только там. Здесь он
исполняется целиком над временными каталогами; подменены только команды,
которым нужен сервер: `sudo`, `systemd-run`, `systemctl`, `curl`, `pnpm` и
интерпретатор экземпляра. Пути и проверка «запускать от root» переписаны в
копии сценария — в самом сценарии тестовых лазеек нет.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "infra" / "electronics" / "sync.sh"

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="нужен rsync")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Sandbox:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.src = tmp_path / "pharmacy"
        self.dst = tmp_path / "electro"
        self.env = tmp_path / "env"
        self.log = tmp_path / "calls.log"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        for directory in (self.state, self.bin, self.dst / "frontend", self.dst / "frontend-src"):
            directory.mkdir(parents=True)
        (self.src / "src").mkdir(parents=True)
        (self.src / "src" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")
        (self.src / "migrations" / "versions").mkdir(parents=True)
        (self.src / "migrations" / "versions" / "0001.py").write_text("# 1\n", encoding="utf-8")
        (self.src / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        front = self.src / "frontend"
        (front / "src").mkdir(parents=True)
        (front / "src" / "page.tsx").write_text("export {}\n", encoding="utf-8")
        (front / "package.json").write_text("{}\n", encoding="utf-8")
        # То, что из аптечного каталога интерфейса переходить не должно.
        (front / ".env.production").write_text("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME=pharmacy\n")
        (front / ".next").mkdir()
        (front / ".next" / "BUILD_ID").write_text("pharmacy-build\n")
        (front / "node_modules").mkdir()
        (front / "node_modules" / "stale.js").write_text("old\n")
        # Файл окружения, негодный для оболочки: его обязан читать не bash.
        self.env.write_text(
            "SMTP_FROM=Price Monitor <a@b>\n"
            "ADMIN_PASSWORD_HASH=$2b$12$abc\n"
            "PM_PROFILE=electronics\n"
            "DATABASE_URL=postgresql://pm:x@h/electro_monitor?a=1&b=2\n",
            encoding="utf-8",
        )

        log = self.log
        _stub(
            self.bin / "sudo",
            'while [[ $1 == -* ]]; do [[ $1 == -u ]] && shift; shift; done\nexec "$@"\n',
        )
        _stub(
            self.bin / "systemd-run",
            f'''wd=.
while [[ $1 == -* ]]; do
  if [[ $1 == -p ]]; then
    case $2 in WorkingDirectory=*) wd=${{2#WorkingDirectory=}} ;; EnvironmentFile=*) echo "envfile ${{2#EnvironmentFile=}}" >> "{log}" ;; esac
    shift 2
  else shift; fi
done
cd "$wd" && exec "$@"
''',
        )
        _stub(
            self.bin / "python-instance",
            f'''echo "python $* (cwd=$(basename "$PWD"))" >> "{log}"
if [[ $* == *"alembic upgrade head"* && -f "{self.state}/alembic-fails" ]]; then exit 1; fi
exit 0
''',
        )
        _stub(
            self.bin / "systemctl",
            f'''echo "systemctl $*" >> "{log}"
case "$1 $2" in
  "is-active --quiet") unit=${{3//\\*/STAR}}; [[ -f "{self.state}/active-$unit" ]] ;;
  "restart "*) touch "{self.state}/active-$2" ;;
esac
''',
        )
        _stub(self.bin / "curl", f'echo "curl $*" >> "{log}"\n[[ ! -f "{self.state}/api-down" ]]\n')
        _stub(
            self.bin / "pnpm",
            f'''echo "pnpm $* (profile=$NEXT_PUBLIC_PM_PROFILE cwd=$(basename "$PWD"))" >> "{log}"
if [[ $1 == build ]]; then
  [[ -f "{self.state}/build-fails" ]] && exit 1
  mkdir -p .next/standalone .next/static
  date +%s%N > .next/BUILD_ID; echo ok > .next/standalone/server.js
fi
''',
        )
        _stub(self.bin / "sleep", "exit 0\n")

        script = SYNC.read_text(encoding="utf-8")
        for original, replacement in (
            ("SRC=/opt/pharmacy-monitor", f"SRC={self.src}"),
            ("DST=/opt/electro-monitor", f"DST={self.dst}"),
            ("ENV_FILE=/etc/electro-monitor/env", f"ENV_FILE={self.env}"),
            ('PY="$SRC/.venv/bin/python"', f'PY="{self.bin}/python-instance"'),
            ("[[ $EUID -eq 0 ]] ||", "true ||"),
            # У учётки теста нет профиля входа с pnpm: в песочнице PATH — заглушки.
            ("as_pm bash -lc", "as_pm bash -c"),
        ):
            assert original in script, original
            script = script.replace(original, replacement)
        self.script = tmp_path / "sync.sh"
        self.script.write_text(script, encoding="utf-8")

    def run(self) -> subprocess.CompletedProcess:
        self.log.write_text("", encoding="utf-8")
        return subprocess.run(
            ["bash", str(self.script)],
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def did(self, fragment: str) -> bool:
        return any(fragment in call for call in self.calls())


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def test_first_run_migrates_builds_and_starts_everything(box):
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr

    assert (box.dst / "src" / "main.py").read_text() == "VERSION = 1\n"
    assert (box.dst / "migrations" / "versions" / "0001.py").exists()
    # Миграции и метка базы — кодом экземпляра, из его каталога, с его окружением.
    assert box.did("python -m alembic upgrade head (cwd=electro)")
    assert box.did("python -m src.main init-db (cwd=electro)")
    assert box.did(f"envfile {box.env}")
    assert box.did("systemctl restart electro-monitor-api.service")
    # Интерфейс собран со своим профилем, в стороне, и перенесён в рабочий каталог.
    assert box.did("pnpm build (profile=electronics cwd=frontend-src)")
    assert (box.dst / "frontend" / ".next" / "standalone" / "server.js").exists()
    assert box.did("systemctl restart electro-monitor-frontend.service")
    # Из аптечного каталога интерфейса не пришли ни приватные файлы сборки, ни
    # его сборка, ни его зависимости.
    assert not (box.dst / "frontend-src" / ".env.production").exists()
    assert not (box.dst / "frontend-src" / "node_modules").exists()
    assert (box.dst / "frontend" / ".next" / "BUILD_ID").read_text() != "pharmacy-build\n"
    # Аптечный каталог не тронут.
    assert (box.src / "frontend" / ".next" / "BUILD_ID").read_text() == "pharmacy-build\n"
    assert (box.src / "frontend" / "node_modules" / "stale.js").exists()


def test_second_run_without_changes_restarts_and_builds_nothing(box):
    assert box.run().returncode == 0
    build_id = (box.dst / "frontend" / ".next" / "BUILD_ID").read_text()

    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    # Миграции и метка — каждый раз (на готовой базе это пусто)…
    assert box.did("alembic upgrade head") and box.did("init-db")
    # …а перезапусков и сборки нет.
    assert not box.did("systemctl restart")
    assert not box.did("pnpm")
    assert (box.dst / "frontend" / ".next" / "BUILD_ID").read_text() == build_id


def test_code_change_restarts_the_api_and_frontend_change_rebuilds(box):
    assert box.run().returncode == 0

    (box.src / "src" / "main.py").write_text("VERSION = 2\n", encoding="utf-8")
    assert box.run().returncode == 0
    assert box.did("systemctl restart electro-monitor-api.service")
    assert not box.did("pnpm")
    assert (box.dst / "src" / "main.py").read_text() == "VERSION = 2\n"

    (box.src / "frontend" / "src" / "page.tsx").write_text("export const a = 1\n")
    assert box.run().returncode == 0
    assert box.did("pnpm build")
    assert box.did("systemctl restart electro-monitor-frontend.service")
    assert not box.did("systemctl restart electro-monitor-api.service")


def test_run_after_a_failed_migration_finishes_the_job(box):
    # Сбой между копированием кода и миграцией: следующий запуск обязан
    # довести дело до конца, а не увидеть «код без изменений».
    (box.state / "alembic-fails").touch()
    failed = box.run()
    assert failed.returncode != 0
    assert not box.did("systemctl restart")

    (box.state / "alembic-fails").unlink()
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.did("alembic upgrade head") and box.did("init-db")
    assert box.did("systemctl restart electro-monitor-api.service")
    assert box.did("pnpm build")


def test_failed_build_leaves_the_running_site_as_it_was(box):
    assert box.run().returncode == 0
    build_id = (box.dst / "frontend" / ".next" / "BUILD_ID").read_text()

    (box.src / "frontend" / "src" / "page.tsx").write_text("export const broken = 1\n")
    (box.state / "build-fails").touch()
    failed = box.run()
    assert failed.returncode != 0
    assert (box.dst / "frontend" / ".next" / "BUILD_ID").read_text() == build_id
    assert not box.did("systemctl restart electro-monitor-frontend.service")

    # Следующий запуск сборку повторяет: неудачная отпечатка не оставила.
    (box.state / "build-fails").unlink()
    assert box.run().returncode == 0
    assert box.did("pnpm build")


def test_running_scrape_postpones_the_sync(box):
    (box.state / "active-electro-monitor-scrape@STAR.service").touch()
    done = box.run()
    assert done.returncode == 0
    assert "идёт сбор" in done.stdout
    assert not (box.dst / "src" / "main.py").exists()
    assert not box.did("alembic")


def test_unhealthy_api_fails_the_sync(box):
    (box.state / "api-down").touch()
    done = box.run()
    assert done.returncode != 0
    assert "API не отвечает" in done.stderr


@pytest.mark.parametrize(
    "broken_env",
    [
        "DATABASE_URL=postgresql://pm@h/db\n",
        "PM_PROFILE=pharmacy\nDATABASE_URL=x\n",
        "PM_PROFILE=electronics\n",
    ],
)
def test_environment_of_another_profile_or_without_a_database_is_refused(box, broken_env):
    box.env.write_text(broken_env, encoding="utf-8")
    done = box.run()
    assert done.returncode != 0
    assert not (box.dst / "src" / "main.py").exists()
    assert box.calls() == []
