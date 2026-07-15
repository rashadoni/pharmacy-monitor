"""Regression tests for the production Postgres backup wrapper."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "infra" / "scripts" / "backup.sh"
FIXED_TIMESTAMP = "2026-07-10T120000Z"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_backup(
    tmp_path: Path,
    *,
    pg_dump_body: str,
    extra_env_file: str = "",
    fake_gpg_body: str | None = None,
    fake_b2_body: str | None = None,
    minimum_bytes: int = 1,
    restricted_path: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "pg_dump", pg_dump_body)
    if fake_gpg_body is not None:
        _write_executable(fake_bin / "gpg", fake_gpg_body)
    if fake_b2_body is not None:
        _write_executable(fake_bin / "b2", fake_b2_body)

    env_file = tmp_path / "backup.env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://pm:secret@db/pharmacy_monitor\n"
        f"{extra_env_file}",
        encoding="utf-8",
    )

    backup_dir = tmp_path / "backups"
    env = os.environ.copy()
    env.update(
        {
            "PATH": (
                f"{fake_bin}:/usr/bin:/bin"
                if restricted_path
                else f"{fake_bin}:{env['PATH']}"
            ),
            "BACKUP_DIR": str(backup_dir),
            "ENV_FILE": str(env_file),
            "BACKUP_MIN_BYTES": str(minimum_bytes),
            "BACKUP_TIMESTAMP": FIXED_TIMESTAMP,
            "TMPDIR": str(tmp_path / "runtime"),
        }
    )
    completed = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed, backup_dir


def test_successful_backup_is_verified_and_published_atomically(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
    )

    assert completed.returncode == 0, completed.stderr
    published = backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz"
    assert published.is_file()
    assert not list(backup_dir.glob(".*.tmp"))
    with gzip.open(published, "rt", encoding="utf-8") as stream:
        assert "CREATE TABLE proof" in stream.read()
    assert "Dump verified" in completed.stdout
    assert "Published atomically" in completed.stdout


def test_failed_pg_dump_leaves_no_published_or_temporary_archive(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- incomplete dump'; exit 1",
    )

    assert completed.returncode != 0
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []


def test_suspiciously_small_successful_dump_is_not_published(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf 'x'",
        minimum_bytes=1024,
    )

    assert completed.returncode != 0
    assert "suspiciously small" in completed.stderr
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []


def test_failed_gpg_leaves_no_published_or_temporary_archive(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file="BACKUP_GPG_PASSPHRASE=test-passphrase\n",
        fake_gpg_body="""
output=''
previous=''
for argument in "$@"; do
    if [[ "$previous" == '--output' ]]; then
        output="$argument"
        break
    fi
    previous="$argument"
done
[[ -z "$output" ]] || printf 'partial encrypted data' > "$output"
exit 1
""",
    )

    assert completed.returncode != 0
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []


@pytest.mark.parametrize(
    "partial_config,missing_name",
    [
        ("B2_APPLICATION_KEY_ID=configured-id\n", "B2_APPLICATION_KEY"),
        ("B2_APPLICATION_KEY=configured-key\n", "B2_APPLICATION_KEY_ID"),
    ],
)
def test_partial_b2_configuration_fails_before_dump(
    tmp_path: Path,
    partial_config: str,
    missing_name: str,
) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- should not be called'; exit 99",
        extra_env_file=partial_config,
    )

    assert completed.returncode != 0
    assert f"{missing_name} is missing" in completed.stderr
    assert not backup_dir.exists()


def test_configured_b2_without_cli_fails_but_keeps_verified_local_backup(
    tmp_path: Path,
) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file=(
            "B2_APPLICATION_KEY_ID=configured-id\n"
            "B2_APPLICATION_KEY=configured-key\n"
        ),
        restricted_path=True,
    )

    assert completed.returncode != 0
    assert "b2 CLI is not installed" in completed.stderr
    assert (backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz").is_file()


def test_b2_upload_failure_fails_but_keeps_verified_local_backup(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file=(
            "B2_APPLICATION_KEY_ID=configured-id\n"
            "B2_APPLICATION_KEY=configured-key\n"
        ),
        fake_b2_body="""
if [[ "$1 $2" == 'account authorize' ]]; then
    exit 0
fi
exit 23
""",
    )

    assert completed.returncode != 0
    assert (backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz").is_file()


def test_b2_upload_success_completes_backup(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file=(
            "B2_APPLICATION_KEY_ID=configured-id\n"
            "B2_APPLICATION_KEY=configured-key\n"
        ),
        fake_b2_body="exit 0",
    )

    assert completed.returncode == 0, completed.stderr
    assert (backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz").is_file()
    assert "B2 upload" in completed.stdout


def test_successful_encryption_round_trip_is_published(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file="BACKUP_GPG_PASSPHRASE=test-passphrase\n",
        fake_gpg_body="""
if [[ " $* " == *' --decrypt '* ]]; then
    cat "${@: -1}"
    exit 0
fi
output=''
input="${@: -1}"
previous=''
for argument in "$@"; do
    if [[ "$previous" == '--output' ]]; then
        output="$argument"
        break
    fi
    previous="$argument"
done
cp "$input" "$output"
""",
    )

    assert completed.returncode == 0, completed.stderr
    encrypted = backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz.gpg"
    assert encrypted.is_file()
    assert not (backup_dir / f"pharmacy-monitor-{FIXED_TIMESTAMP}.sql.gz").exists()
    assert "Encryption round-trip verified" in completed.stdout


def test_failed_decrypt_round_trip_is_not_published(tmp_path: Path) -> None:
    completed, backup_dir = _run_backup(
        tmp_path,
        pg_dump_body="printf '%s\\n' '-- PostgreSQL dump' 'CREATE TABLE proof (id int);'",
        extra_env_file="BACKUP_GPG_PASSPHRASE=test-passphrase\n",
        fake_gpg_body="""
if [[ " $* " == *' --decrypt '* ]]; then
    exit 42
fi
output=''
input="${@: -1}"
previous=''
for argument in "$@"; do
    if [[ "$previous" == '--output' ]]; then
        output="$argument"
        break
    fi
    previous="$argument"
done
cp "$input" "$output"
""",
    )

    assert completed.returncode != 0
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []
