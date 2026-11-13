"""Профиль экземпляра: аптечный не изменился, второй подставляет свои сайты.

Списки сайтов объявлены константами модулей и читаются при импорте, поэтому
другой профиль проверяется в отдельном процессе — так же, как он будет жить
на сервере: переменная окружения и свежий интерпретатор.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src import site_profile

ROOT = Path(__file__).resolve().parent.parent

# Что читает каждый реестр. Скрипт исполняется в отдельном процессе.
_REGISTRY_DUMP = """
import json
from src import (alerts, analytics, analyzer, api, cadence, comparison_export,
                 comparison_rows, health, intraday, main, product_policy, roi,
                 site_profile, storage, watchlist)
print("REGISTRIES=" + json.dumps({
    "profile": site_profile.PROFILE.name,
    "alerts": [alerts.CLIENT_SITE, list(alerts.COMPETITOR_SITES)],
    "analytics": [analytics.CLIENT_SITE, list(analytics.COMPETITOR_SITES), list(analytics.ALL_SITES)],
    "analyzer": [analyzer.CLIENT_SITE, list(analyzer.COMPETITOR_SITES)],
    "roi": [roi.CLIENT_SITE, list(roi.COMPETITOR_SITES), list(roi.ALL_SITES)],
    "roi_cache_max_age_hours": roi._CACHE_MAX_AGE_HOURS,
    "export": [list(comparison_export.SITES), comparison_export.CLIENT_SITE,
               comparison_export.SITE_TITLES],
    "comparison_order": comparison_rows._SITE_ORDER,
    "intraday": list(intraday.INTRADAY_SITES),
    "required_catalog_sites": list(product_policy.REQUIRED_CATALOG_SITES),
    "offer_max_age_hours": product_policy.OFFER_MAX_AGE_HOURS,
    "watchlist": list(watchlist.SITES),
    "full_catalog_sites": list(storage.FULL_CATALOG_SITES),
    "api_valid_sites": list(api._VALID_SITES),
    "cadence": cadence.SITE_SCRAPE_CADENCE_HOURS,
    "health_max_age_hours": health._SITE_MAX_AGE_HOURS,
    "scrapers": {site: cls.__name__ for site, cls in main.SCRAPER_CLASSES.items()},
    "validate_links_client": main._VALIDATE_CLIENT_SITE,
    "sites_scraped_without_site_option": main._default_scrape_sites(),
}))
"""


def _registries(profile: str | None, tmp_path: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if key != site_profile.PROFILE_ENV}
    if profile is not None:
        env[site_profile.PROFILE_ENV] = profile
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'profile.db'}"
    # Переменные, которые меняют сборщик pharmonline, тест не проверяет.
    for name in ("SENTRY_DSN", "PHARMONLINE_PUBLIC_API", "PHARMONLINE_USE_DDP"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT)
    done = subprocess.run(
        [sys.executable, "-c", _REGISTRY_DUMP],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(line for line in done.stdout.splitlines() if line.startswith("REGISTRIES="))
    return json.loads(line.removeprefix("REGISTRIES="))


PHARMACY_SITES = ["pharmonline", "aptekonline", "aloe"]


@pytest.mark.parametrize("profile", [None, "", "pharmacy", " Pharmacy "])
def test_pharmacy_instance_keeps_every_registry_it_had(profile, tmp_path):
    # Значения выписаны руками, а не взяты из профиля: тест закрепляет то, что
    # стояло в модулях до появления профиля. Аптечный прод не задаёт PM_PROFILE.
    assert _registries(profile, tmp_path) == {
        "profile": "pharmacy",
        "alerts": ["pharmonline", ["aptekonline", "aloe"]],
        "analytics": ["pharmonline", ["aptekonline", "aloe"], PHARMACY_SITES],
        "analyzer": ["pharmonline", ["aptekonline", "aloe"]],
        "roi": ["pharmonline", ["aptekonline", "aloe"], PHARMACY_SITES],
        "roi_cache_max_age_hours": 30,
        "export": [
            PHARMACY_SITES,
            "pharmonline",
            {"pharmonline": "Pharmonline", "aptekonline": "Aptekonline", "aloe": "Aloe"},
        ],
        "comparison_order": {"pharmonline": 0, "aptekonline": 1, "aloe": 2},
        "intraday": ["aloe"],
        "required_catalog_sites": PHARMACY_SITES,
        "offer_max_age_hours": {"pharmonline": 30, "aptekonline": 30, "aloe": 30},
        "watchlist": PHARMACY_SITES,
        "full_catalog_sites": PHARMACY_SITES,
        "api_valid_sites": PHARMACY_SITES,
        "cadence": {"pharmonline": 24, "aptekonline": 24, "aloe": 24},
        "health_max_age_hours": {"pharmonline": 30, "aptekonline": 30, "aloe": 30},
        "scrapers": {
            "pharmonline": "PharmonlineScraper",
            "aptekonline": "AptekonlineScraper",
            "aloe": "AloeScraper",
        },
        "validate_links_client": "pharmonline",
        "sites_scraped_without_site_option": PHARMACY_SITES,
    }


def test_electronics_instance_sees_only_its_own_sites(tmp_path):
    registries = _registries("electronics", tmp_path)
    sites = ["kontakt", "irshad", "bakuelectronics"]
    assert registries["profile"] == "electronics"
    assert registries["alerts"] == ["kontakt", ["irshad", "bakuelectronics"]]
    assert registries["analytics"] == ["kontakt", ["irshad", "bakuelectronics"], sites]
    assert registries["analyzer"] == ["kontakt", ["irshad", "bakuelectronics"]]
    assert registries["roi"] == ["kontakt", ["irshad", "bakuelectronics"], sites]
    assert registries["export"] == [
        sites,
        "kontakt",
        {"kontakt": "Kontakt Home", "irshad": "Irshad", "bakuelectronics": "Baku Electronics"},
    ]
    assert registries["comparison_order"] == {"kontakt": 0, "irshad": 1, "bakuelectronics": 2}
    assert registries["intraday"] == []
    for name in ("required_catalog_sites", "watchlist", "full_catalog_sites", "api_valid_sites"):
        assert registries[name] == sites, name
    assert registries["cadence"] == {"kontakt": 336, "irshad": 24, "bakuelectronics": 24}
    assert registries["health_max_age_hours"] == {
        "kontakt": 342,
        "irshad": 30,
        "bakuelectronics": 30,
    }
    assert registries["scrapers"] == {
        "kontakt": "KontaktScraper",
        "irshad": "IrshadScraper",
        "bakuelectronics": "BakuElectronicsScraper",
    }
    assert registries["validate_links_client"] == "kontakt"
    # Kontakt Home приходит файлом: команда без --site его не собирает.
    assert registries["sites_scraped_without_site_option"] == ["irshad", "bakuelectronics"]
    # Ни один аптечный сайт не просочился ни в один реестр.
    assert not re.search(r"pharmonline|aptekonline|aloe", json.dumps(registries))


def test_unknown_profile_is_refused_not_treated_as_pharmacy():
    # Экземпляр электроники с опечаткой в переменной не должен молча стать
    # аптечным и начать собирать аптечные сайты в свою базу.
    with pytest.raises(RuntimeError) as error:
        site_profile.profile_from_environment({site_profile.PROFILE_ENV: "elektronika"})
    assert "elektronika" in str(error.value)
    assert "electronics" in str(error.value)


def test_profile_from_environment_defaults_to_pharmacy():
    assert site_profile.profile_from_environment({}) is site_profile.PHARMACY
    assert site_profile.profile_from_environment({"PM_PROFILE": ""}) is site_profile.PHARMACY
    assert (
        site_profile.profile_from_environment({"PM_PROFILE": "Electronics"})
        is site_profile.ELECTRONICS
    )


@pytest.mark.parametrize(
    "broken",
    [
        {"client_site": "aloe"},
        {"sites": ("a", "a")},
        {"titles": {"a": "A"}},
        {"cadence_hours": {"a": 24, "b": 24, "c": 24}},
        {"intraday_sites": ("z",)},
        {"imported_sites": ("z",)},
    ],
)
def test_profile_refuses_an_inconsistent_site_list(broken):
    fields = {
        "name": "x",
        "sites": ("a", "b"),
        "client_site": "a",
        "titles": {"a": "A", "b": "B"},
        "cadence_hours": {"a": 24, "b": 24},
    }
    site_profile.SiteProfile(**fields)
    with pytest.raises(ValueError):
        site_profile.SiteProfile(**{**fields, **broken})


def test_no_module_keeps_its_own_copy_of_the_pharmacy_site_list():
    # Растяжка: перечень аптечных сайтов кортежем, списком или множеством вне профиля —
    # это реестр, который второй экземпляр не увидит. Проверка по тексту:
    # словари с ветками по сайту и сами сборщики её не касаются.
    quoted = r"""["'](?:pharmonline|aptekonline|aloe)["']"""
    listing = re.compile(
        rf"[\(\[\{{]\s*{quoted}\s*,\s*{quoted}(?:\s*,\s*{quoted})?\s*,?\s*[\)\]\}}]"
    )
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name == "site_profile.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if listing.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_viewer_sites_put_the_chosen_site_first():
    from src import site_profile

    assert site_profile.viewer_sites() == site_profile.SITES
    assert site_profile.viewer_sites(site_profile.SITES[2]) == (
        site_profile.SITES[2],
        site_profile.SITES[0],
        site_profile.SITES[1],
    )
    with pytest.raises(ValueError, match="nowhere"):
        site_profile.viewer_sites("nowhere")
