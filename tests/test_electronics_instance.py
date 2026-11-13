"""Экземпляр электроники целиком: свой процесс, свой профиль, своя база.

Профиль читается при импорте, поэтому экземпляр проверяется так же, как будет
жить на сервере: ``PM_PROFILE=electronics`` и отдельный интерпретатор. Сайты
подставные (tests/electronics_instance_probe.py), всё остальное — настоящая
команда ``run``: сбор, запись, проверка полноты, алерты, отчёт, рекомендации.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import site_profile, storage, tenants

ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path: Path, profile: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != site_profile.PROFILE_ENV}
    if profile is not None:
        env[site_profile.PROFILE_ENV] = profile
    for name in ("SENTRY_DSN", "REDIS_URL", "SMTP_HOST", "TELEGRAM_BOT_TOKEN"):
        env.pop(name, None)
    env.update(
        DATABASE_URL=f"sqlite:///{tmp_path / 'instance.db'}",
        SCRAPE_REPORT_EMAIL="0",
        PYTHONPATH=str(ROOT),
    )
    return env


def _probe(tmp_path: Path, sites: str) -> dict:
    done = subprocess.run(
        [sys.executable, "-m", "tests.electronics_instance_probe", sites],
        cwd=tmp_path,
        env=_env(tmp_path, "electronics"),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    line = next(line for line in done.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(line.removeprefix("RESULT="))


def test_three_sites_are_scanned_verified_and_priced(tmp_path):
    outcome = _probe(tmp_path, "kontakt,irshad,bakuelectronics")

    assert [(run["site"], run["exit_code"]) for run in outcome["runs"]] == [
        ("kontakt", 0),
        ("irshad", 0),
        ("bakuelectronics", 0),
    ], outcome["runs"]
    for row in outcome["run_rows"]:
        assert row["status"] == "ok", row
        assert row["scope"] == "full"
        assert row["verified"] is True
        assert row["products"] == 3
    assert [row["sites"] for row in outcome["run_rows"]] == ["kontakt", "irshad", "bakuelectronics"]
    assert outcome["products_per_site"] == {"kontakt": 3, "irshad": 3, "bakuelectronics": 3}
    assert outcome["snapshots"] == 9
    # База помечена профилем: клиент арендатора по умолчанию — Kontakt Home.
    assert outcome["tenants"] == [["default", "kontakt"]]
    # Рекомендации считаются, только когда собраны все три сайта: после третьего.
    last_output = outcome["runs"][-1]["output_tail"]
    assert "roi_cache_refreshed" in last_output
    assert "roi_cache_refresh_deferred_unverified_inputs" in outcome["runs"][0]["output_tail"]


def test_site_without_synced_sections_fails_loudly(tmp_path):
    # Разделы записаны только для сайтов из каталога пробы; сбор сайта, у
    # которого их нет, не должен кончиться «ok» с нулём товаров.
    outcome = _probe(tmp_path, "kontakt")
    assert outcome["runs"][0]["exit_code"] == 0

    done = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--site", "irshad", "--force"],
        cwd=tmp_path,
        env={
            **_env(tmp_path, "electronics"),
            "DATABASE_URL": f"sqlite:///{tmp_path / 'empty.db'}",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode != 0, done.stdout[-2000:]
    assert "no_categories_configured" in done.stdout + done.stderr


def test_pharmacy_command_refuses_the_electronics_database(tmp_path):
    # Команда без PM_PROFILE в каталоге экземпляра электроники — аптечная.
    # Она обязана отказать, а не начать собирать аптечные сайты в чужую базу.
    _probe(tmp_path, "kontakt")
    for profile in (None, "pharmacy"):
        done = subprocess.run(
            [sys.executable, "-m", "src.main", "routes", "list"],
            cwd=tmp_path,
            env=_env(tmp_path, profile),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode != 0
        assert "belongs to a 'electronics' instance" in done.stdout + done.stderr

    done = subprocess.run(
        [sys.executable, "-m", "src.main", "routes", "list"],
        cwd=tmp_path,
        env=_env(tmp_path, "electronics"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    assert "kontakt  telefonlar" in done.stdout


def _factory_with_default_client(db_session, client_site):
    tenant = tenants.get_or_create_default(db_session)
    tenant.client_site = client_site
    db_session.commit()
    return lambda: db_session


def test_default_tenant_of_a_pharmacy_database_is_unchanged(db_session):
    tenant = tenants.get_or_create_default(db_session)
    assert (tenant.slug, tenant.name, tenant.client_site) == (
        "default",
        "Default (pharmonline)",
        "pharmonline",
    )


@pytest.mark.parametrize("client_site", ["pharmonline", "aloe", None, "", "somebody-else"])
def test_pharmacy_database_is_never_refused(db_session, client_site):
    # Отказ — только когда метка называет сайт другого известного профиля.
    # Аптечную базу проверка остановить не может, какой бы ни была её метка.
    storage.refuse_database_of_another_profile(
        _factory_with_default_client(db_session, client_site)
    )


@pytest.mark.parametrize("client_site", ["kontakt", "irshad", "bakuelectronics"])
def test_database_marked_with_another_profiles_site_is_refused(db_session, client_site):
    with pytest.raises(storage.DatabaseOfAnotherProfile) as error:
        storage.refuse_database_of_another_profile(
            _factory_with_default_client(db_session, client_site)
        )
    assert "electronics" in str(error.value)
    assert "PM_PROFILE=electronics" in str(error.value)


def test_unreachable_database_does_not_stop_the_process():
    def broken_factory():
        raise ConnectionError("database is restarting")

    storage.refuse_database_of_another_profile(broken_factory)
