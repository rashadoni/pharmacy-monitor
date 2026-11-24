"""Приём каталога Kontakt на сервере (`infra/electronics/kontakt-receive.sh`) и
установка ключа (`kontakt-upload-setup.sh`) — в песочнице.

Пути сервера заменены каталогами песочницы, `systemd-run`, `systemctl` и
`journalctl` — заглушки, которые записывают, как их позвали. Настоящих systemd,
sshd и базы песочница не видит.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIVE = ROOT / "infra" / "electronics" / "kontakt-receive.sh"
SETUP = ROOT / "infra" / "electronics" / "kontakt-upload-setup.sh"
HEADER = "name,url,price,sale_price,category,availability"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def server(tmp_path):
    box = tmp_path
    (box / "bin").mkdir()
    (box / "etc").mkdir()
    (box / "etc" / "env").write_text("PM_PROFILE=electronics\n")
    python = box / "python"
    _exe(python, "exit 0\n")
    calls = box / "calls"
    running = box / "running"
    _exe(
        box / "bin" / "systemd-run",
        f'echo "systemd-run $*" >> {calls}\nexit "${{FAKE_DRY_RUN_CODE:-0}}"\n',
    )
    _exe(
        box / "bin" / "systemctl",
        f'echo "systemctl $*" >> {calls}\n[[ -f {running} ]] && cat {running}\nexit 0\n',
    )
    _exe(box / "bin" / "journalctl", f'echo "journalctl $*" >> {calls}\necho "итог загрузки"\n')
    script = (
        RECEIVE.read_text()
        .replace("/var/lib/electro-monitor/kontakt-inbox", str(box / "inbox"))
        .replace("/var/lib/electro-monitor", str(box / "lib"))
        .replace("/etc/electro-monitor/env", str(box / "etc" / "env"))
        .replace("/opt/pharmacy-monitor/.venv/bin/python", str(python))
        .replace("/run/electro-monitor-kontakt-receive.lock", str(box / "lock"))
    )
    # `install -o root` в песочнице не от root не пройдёт: каталог создаётся сам.
    script = script.replace("install -d -o root -g root -m 755", "install -d -m 755")
    receive = box / "kontakt-receive"
    _exe(receive, script.split("\n", 1)[1])

    def run(command: str | None, data: bytes = b"", **env):
        environ = {
            "PATH": f"{box / 'bin'}:{os.environ['PATH']}",
            "HOME": str(box),
            **{k: str(v) for k, v in env.items()},
        }
        if command is not None:
            environ["SSH_ORIGINAL_COMMAND"] = command
        return subprocess.run(
            ["bash", str(receive)], input=data, capture_output=True, env=environ, timeout=30
        )

    run.box = box
    run.calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    run.running = running
    return run


def _csv(rows=1) -> bytes:
    lines = [HEADER] + [f"T{n},https://kontakt.az/t-{n},{n}.99,,tv,in_stock" for n in range(rows)]
    return ("\n".join(lines) + "\n").encode()


def test_ping_and_unknown_command(server):
    from src.kontakt_collect import COLUMNS

    pong = server("ping")
    # Мак сверяет этот заголовок со своими колонками до обхода.
    assert pong.returncode == 0 and pong.stdout.decode().strip() == ",".join(COLUMNS)
    refused = server("bash -i")
    assert refused.returncode == 64
    assert server.calls() == []


def test_import_checks_the_file_then_starts_a_detached_import(server):
    result = server("import", _csv(3))
    assert result.returncode == 0, result.stderr.decode()
    files = sorted((server.box / "inbox").glob("kontakt-*.csv"))
    assert len(files) == 1 and files[0].read_bytes() == _csv(3)
    assert oct(files[0].stat().st_mode & 0o777) == "0o644"  # читает pm
    runs = [line for line in server.calls() if line.startswith("systemd-run")]
    assert len(runs) == 2
    dry, real = runs
    assert "--wait" in dry and "--dry-run" in dry and "User=pm" in dry
    assert "--no-block" in real and "--dry-run" not in real
    assert "SyslogIdentifier=kontakt-import" in real
    assert f"catalog import --site kontakt {files[0]}" in real


def test_a_file_the_dry_run_rejects_is_not_imported(server):
    result = server("import", _csv(), FAKE_DRY_RUN_CODE=1)
    assert result.returncode != 0
    runs = [line for line in server.calls() if line.startswith("systemd-run")]
    assert len(runs) == 1 and "--dry-run" in runs[0]


def test_dry_run_command_starts_nothing(server):
    assert server("dry-run", _csv()).returncode == 0
    runs = [line for line in server.calls() if line.startswith("systemd-run")]
    assert len(runs) == 1 and "--dry-run" in runs[0]


@pytest.mark.parametrize(
    "data",
    [b"", b"<html>Just a moment</html>\n", b"name,url,price\nx,https://kontakt.az/x,1\n"],
)
def test_foreign_or_empty_input_is_refused_and_not_kept(server, data):
    result = server("import", data)
    assert result.returncode == 65
    assert list((server.box / "inbox").iterdir()) == []
    assert not any(line.startswith("systemd-run") for line in server.calls())


def test_a_running_import_is_not_doubled(server):
    server.running.write_text(
        "electro-monitor-kontakt-import-20261124T000000Z.service loaded active running x\n"
    )
    result = server("import", _csv())
    assert result.returncode == 75
    assert not any(line.startswith("systemd-run") for line in server.calls())


def test_only_the_last_files_are_kept(server):
    inbox = server.box / "inbox"
    inbox.mkdir()
    for n in range(20):
        old = inbox / f"kontakt-2026010{n:02d}.csv"
        old.write_text(HEADER)
        os.utime(old, (1_700_000_000 + n, 1_700_000_000 + n))
    assert server("import", _csv()).returncode == 0
    assert len(list(inbox.glob("kontakt-*.csv"))) == 14


def test_status_reads_the_journal_tag(server):
    result = server("status")
    assert result.returncode == 0
    assert any("journalctl -t kontakt-import" in line for line in server.calls())


# -- установка ключа ------------------------------------------------------------


@pytest.fixture
def setup(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAAOTHERKEYOTHERKEYOTHERKEYOTHERKEYOTHERKEY owner@mac\n")
    work = tmp_path / "electronics"
    work.mkdir()
    script = (
        SETUP.read_text()
        .replace("LIB=/usr/local/lib/electro-monitor", f"LIB={lib}")
        .replace("KEYS=/root/.ssh/authorized_keys", f"KEYS={keys}")
        .replace("[[ $EUID -eq 0 ]] ||", "true ||")
        .replace("install -d -m 700 /root/.ssh", ":")
        .replace("install -o root -g root -m 755", "install -m 755")
    )
    (work / "kontakt-upload-setup.sh").write_text(script)
    (work / "kontakt-receive.sh").write_text(RECEIVE.read_text())

    def run(arg):
        return subprocess.run(
            ["bash", str(work / "kontakt-upload-setup.sh"), arg], capture_output=True, timeout=30
        )

    run.keys, run.lib = keys, lib
    return run


KEY = "ssh-ed25519 " + "A" * 68 + " kontakt-upload"


def test_setup_adds_a_restricted_key_and_keeps_the_others(setup):
    assert setup(KEY).returncode == 0
    assert setup(KEY).returncode == 0  # повтор не дублирует строку
    lines = setup.keys.read_text().splitlines()
    assert lines[0].endswith("owner@mac")
    ours = [line for line in lines if line.endswith(" kontakt-upload")]
    assert ours == [f'restrict,command="{setup.lib}/kontakt-receive" {KEY}']
    assert (setup.lib / "kontakt-receive").read_text() == RECEIVE.read_text()


def test_setup_refuses_what_is_not_a_public_key(setup):
    before = setup.keys.read_text()
    assert setup('command="sh" ssh-ed25519 AAAA kontakt-upload').returncode == 2
    assert setup("-----BEGIN OPENSSH PRIVATE KEY-----").returncode == 2
    assert setup.keys.read_text() == before


def test_setup_remove_takes_away_only_our_key(setup):
    setup(KEY)
    assert setup("--remove").returncode == 0
    assert setup.keys.read_text().splitlines() == [
        "ssh-ed25519 AAAAOTHERKEYOTHERKEYOTHERKEYOTHERKEYOTHERKEY owner@mac"
    ]
    assert not (setup.lib / "kontakt-receive").exists()
