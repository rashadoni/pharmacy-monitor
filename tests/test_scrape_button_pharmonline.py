"""Кнопка «Запустить scrape» и pharmonline на сервере с маркером автономного режима.

Кнопка кладёт строку в `scrape_requests`, серверный вотчер зовёт
`run --mode category --no-alerts --request-id N` (+ `--site` на каждый выбранный
сайт; фронтенд сайтов не выбирает — значит, все три). Маркер переводит в
public_api только `run --site pharmonline` в режиме `auto`, поэтому до правки
2026-10-14 кнопка собирала pharmonline сборщиком из env — на сервере это DDP —
мимо проверки личностей и автодопуска, а полная попытка решала доверие к его
каталогу. Теперь на хосте с маркером сбор каталога pharmonline не берёт.

Команда берётся из самого скрипта вотчера: если он сменит форму запуска, тест
пойдёт за ним, а не за копией.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import storage
from src._time import utcnow
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult

REPO = Path(__file__).resolve().parents[1]
WATCHER = REPO / "infra/server/watch-scrape-queue.sh"
SCRAPE_UNIT = REPO / "infra/systemd/pharmacy-monitor-scrape@.service"
ALL_SITES = sorted(main_mod.SCRAPER_CLASSES)


def _watcher_run_args(
    request_id: int,
    *,
    sites: tuple[str, ...] = (),
    category_id: int | None = None,
) -> list[str]:
    """Аргументы `pharmacy-monitor`, с которыми вотчер исполняет заявку."""
    text = WATCHER.read_text(encoding="utf-8")
    base = re.search(r"^ARGS=\((.*)\)$", text, re.MULTILINE)
    assert base, "в вотчере нет строки ARGS=(…) — форма запуска заявки сменилась"
    args = [a.replace("$req_id", str(request_id)) for a in shlex.split(base[1])]
    # Ниже по скрипту: `--site` на каждый сайт заявки, `--category-id` — если есть.
    assert 'ARGS+=("--site" "$st")' in text
    assert 'ARGS+=("--category-id" "$category_id")' in text
    for site in sites:
        args += ["--site", site]
    if category_id is not None:
        args += ["--category-id", str(category_id)]
    return args


def _unit_run_args(site: str) -> list[str]:
    for line in SCRAPE_UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            argv = shlex.split(line.removeprefix("ExecStart=").replace("%i", site))
            return argv[1:]
    raise AssertionError("в юните нет ExecStart")


class _ScrapeReached(Exception):
    """Сбор дошёл до скрейпера — дальше пайплайн тесту не нужен."""


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


@pytest.fixture
def scrape_calls(db_session, monkeypatch, tmp_path):
    """`run` на тестовой БД; скрейпер подменён и записывает, что ему дали."""
    calls: list[list[str]] = []

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        calls.append(sorted(sites_with_slugs))
        raise _ScrapeReached

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {})
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    _routes(monkeypatch, {site: ["cat"] for site in ALL_SITES})
    # Автономный режим пишет в os.environ напрямую: ключи регистрируются через
    # setenv, иначе запись утекла бы в следующие тесты.
    for name in (
        "PHARMONLINE_PUBLIC_API",
        "PHARMONLINE_PUBLIC_API_TRANSPORT",
        "PHARMONLINE_PUBLIC_API_REQUIRE_CATALOG_BASELINE",
        "PHARMONLINE_LEGACY_ID_BRIDGE",
        "PHARMONLINE_DECODO_BACKCONNECT_STICKY",
        "AI_FALLBACK_ENABLED",
        "SCRAPE_REPORT_EMAIL",
    ):
        monkeypatch.setenv(name, "")
    # Как в /etc/pharmacy-monitor/env на сервере (сверено 2026-10-14).
    monkeypatch.setenv("PHARMONLINE_USE_DDP", "1")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(tmp_path / "no-marker"))
    return calls


@pytest.fixture
def marker(monkeypatch, tmp_path):
    """Как на сервере: маркер на месте, транспорт выбран в EnvironmentFile."""
    path = tmp_path / "pharmonline-public-api-autonomous-v1"
    path.write_text(main_mod._PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER_CONTENT, encoding="utf-8")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(path))
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "direct")
    return path


def _routes(monkeypatch, by_site: dict[str, list[str]]) -> None:
    """Маршруты категорий: {сайт: [слаги]} — одни и те же для любой категории."""
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: list(by_site.get(site, [])),
    )


def _request(db_session, *, mode: str = "all", sites: str | None = None, category_id=None) -> int:
    """Заявка в том виде, в каком её отдаёт вотчеру /internal/pending-scrape."""
    request = storage.ScrapeRequest(
        tenant_id=1,
        mode=mode,
        sites=sites,
        category_id=category_id,
        status="running",
        started_at=utcnow(),
    )
    db_session.add(request)
    db_session.commit()
    return request.id


def _saved_request(db_session, request_id: int) -> storage.ScrapeRequest:
    db_session.expire_all()
    return db_session.get(storage.ScrapeRequest, request_id)


def _run_count(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(storage.Run))


def _latest_run(db_session) -> storage.Run:
    db_session.expire_all()
    return db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))


# ─── Кнопка «все» ────────────────────────────────────────────────────────────


def test_button_without_sites_collects_others_and_leaves_pharmonline(
    db_session, scrape_calls, marker
):
    """Главный путь кнопки: заявка без сайтов. pharmonline в сбор не попадает."""
    request_id = _request(db_session)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id))

    assert scrape_calls == [[s for s in ALL_SITES if s != "pharmonline"]]
    run = _latest_run(db_session)
    assert run.catalog_scope == "full"
    assert "pharmonline" not in run.full_catalog_sites.split(",")
    # Полной попытки pharmonline эта заявка не делала — доверие к его каталогу
    # она не трогает.
    attempts = storage.latest_full_catalog_attempts_by_site(db_session, ["pharmonline"])
    assert attempts.get("pharmonline") is None
    assert "pharmonline не собирался" in result.output


def test_failed_button_run_puts_its_cause_before_the_pharmonline_note(
    db_session, scrape_calls, marker
):
    """Плашка «Сбой» начинается с причины сбоя, а не с пометки о pharmonline."""
    request_id = _request(db_session)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id))

    assert result.exit_code != 0
    saved = _saved_request(db_session, request_id)
    assert saved.status == "failed"
    assert saved.error_message.startswith("_ScrapeReached")
    assert "pharmonline не собирался" in saved.error_message


# ─── Заявка, в которой pharmonline назван ───────────────────────────────────


@pytest.mark.parametrize("sites", [("pharmonline",), ("pharmonline", "aloe")])
def test_button_request_naming_pharmonline_is_refused_before_any_run(
    db_session, scrape_calls, marker, sites
):
    request_id = _request(db_session, sites=",".join(sites))
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id, sites=sites))

    assert result.exit_code != 0
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before
    saved = _saved_request(db_session, request_id)
    assert saved.status == "failed"
    assert saved.completed_at is not None
    assert saved.run_id is None
    assert saved.error_message.startswith("pharmonline этим запуском не собирается")
    # Админ должен узнать, куда идти: строка называет раздел RUNBOOK.
    assert "RUNBOOK «Сбор pharmonline" in saved.error_message


# ─── Кнопка у строки категории ──────────────────────────────────────────────


def test_category_only_on_pharmonline_is_refused(db_session, scrape_calls, marker, monkeypatch):
    """Строки `pharma_*` из `category sync-pharmonline` есть только у pharmonline."""
    _routes(monkeypatch, {"pharmonline": ["dermokosmetika"]})
    request_id = _request(db_session, mode="category", category_id=7)
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id, category_id=7))

    assert result.exit_code != 0
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before
    saved = _saved_request(db_session, request_id)
    assert saved.status == "failed"
    assert saved.error_message.startswith("категория #7 есть только у pharmonline")


def test_category_shared_with_another_site_collects_that_site_only(
    db_session, scrape_calls, marker, monkeypatch
):
    _routes(monkeypatch, {"pharmonline": ["vitaminler"], "aloe": ["vitaminler"]})
    request_id = _request(db_session, mode="category", category_id=7)

    CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id, category_id=7))

    assert scrape_calls == [["aloe"]]
    assert "pharmonline не собирался" in _saved_request(db_session, request_id).error_message


def test_category_without_pharmonline_route_gets_no_note(
    db_session, scrape_calls, marker, monkeypatch
):
    """Терять было нечего — пометка о pharmonline была бы шумом."""
    _routes(monkeypatch, {"aloe": ["dermanlar"]})
    request_id = _request(db_session, mode="category", category_id=7)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id, category_id=7))

    assert scrape_calls == [["aloe"]]
    assert "pharmonline" not in (_saved_request(db_session, request_id).error_message or "")
    assert "pharmonline не собирался" not in result.output


# ─── Тот же путь руками ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["run", "--force"], id="force-without-site"),
        pytest.param(["run", "--mode", "category", "--force"], id="category-without-site"),
        pytest.param(["run", "--limit", "5"], id="limit-without-site"),
    ],
)
def test_operator_catalog_scan_without_sites_leaves_pharmonline(
    db_session, scrape_calls, marker, args
):
    result = CliRunner().invoke(main_mod.cli, args)

    assert scrape_calls == [[s for s in ALL_SITES if s != "pharmonline"]]
    assert "pharmonline не собирался" in result.output


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["run", "--site", "pharmonline", "--mode", "category"], id="pharmonline"),
        pytest.param(
            ["run", "--site", "pharmonline", "--site", "aloe", "--force"], id="with-another-site"
        ),
        pytest.param(
            ["run", "--site", "pharmonline", "--mode", "category", "--category-id", "7"],
            id="one-category",
        ),
    ],
)
def test_operator_catalog_scan_naming_pharmonline_is_refused(
    db_session, scrape_calls, marker, args
):
    runs_before = _run_count(db_session)

    result = CliRunner().invoke(main_mod.cli, args)

    assert result.exit_code != 0
    assert scrape_calls == []
    assert _run_count(db_session) == runs_before
    assert "pharmonline этим запуском не собирается" in result.output


# ─── Что не меняется ────────────────────────────────────────────────────────


def test_nightly_timer_still_collects_pharmonline_through_public_api(
    db_session, scrape_calls, marker, monkeypatch
):
    """Тот же маркер, что запрещает сбор каталога, уводит юнит в public_api."""
    from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE

    routes: dict[str, list[str]] = {}

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        routes.update(sites_with_slugs)
        raise _ScrapeReached

    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)

    CliRunner().invoke(main_mod.cli, _unit_run_args("pharmonline"))

    assert routes == {"pharmonline": [PUBLIC_CATALOG_ROUTE]}
    assert _latest_run(db_session).full_catalog_sites == "pharmonline"


@pytest.mark.parametrize("marker_content", [None, "something else\n"])
def test_without_a_valid_marker_the_button_collects_all_sites(
    db_session, scrape_calls, monkeypatch, tmp_path, marker_content
):
    """Без маркера (или с испорченным) pharmonline и ночью идёт сбором каталога —
    кнопка ведёт себя как раньше."""
    if marker_content is not None:
        path = tmp_path / "marker"
        path.write_text(marker_content, encoding="utf-8")
        monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(path))
    request_id = _request(db_session)

    result = CliRunner().invoke(main_mod.cli, _watcher_run_args(request_id))

    assert scrape_calls == [ALL_SITES]
    assert "pharmonline не собирался" not in result.output
    assert "pharmonline" not in (_saved_request(db_session, request_id).error_message or "")


# ─── Удачный сбор кнопкой: пометка доживает до конца ────────────────────────


def _patch_verified_aloe_pipeline(db_session, monkeypatch) -> list[list[str]]:
    """Сбор aloe проходит целиком; скрейпер записывает, какие сайты ему дали."""
    from src import roi

    calls: list[list[str]] = []

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        calls.append(sorted(sites_with_slugs))
        return [
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="aloe-sku",
                        url="https://aloe.example/sku",
                        name="Trusted Aloe SKU",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={
                    "cat": RouteStatus(complete=True, raw_items=1, parsed_items=1, expected_items=1)
                },
            )
        ]

    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 1})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", lambda session: [])
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)
    monkeypatch.setattr(roi, "refresh_all_cached_actions", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main_mod.analyzer, "analyze", lambda *args: SimpleNamespace(run_started_at=utcnow())
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")
    return calls


def test_successful_full_button_run_keeps_the_note(db_session, scrape_calls, marker, monkeypatch):
    """Заявка «все» на сервере из двух сайтов: aloe собран и проверен, заявка ok.

    Сайтов в тесте два (pharmonline и aloe), чтобы сбор без pharmonline был
    полным и дошёл до конца: итог заявки пишет последний блок `run`.
    """
    monkeypatch.setattr(
        main_mod,
        "SCRAPER_CLASSES",
        {site: main_mod.SCRAPER_CLASSES[site] for site in ("pharmonline", "aloe")},
    )
    calls = _patch_verified_aloe_pipeline(db_session, monkeypatch)
    request_id = _request(db_session)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main_mod.cli, _watcher_run_args(request_id))

    assert result.exit_code == 0, result.output
    assert calls == [["aloe"]]
    run = _latest_run(db_session)
    assert (run.status, run.catalog_verified, run.full_catalog_sites) == ("ok", True, "aloe")
    saved = _saved_request(db_session, request_id)
    assert (saved.status, saved.run_id) == ("ok", run.id)
    assert saved.error_message == main_mod._PHARMONLINE_LEFT_TO_PUBLIC_API_NOTE


def test_successful_category_button_run_keeps_the_note(
    db_session, scrape_calls, marker, monkeypatch
):
    """Частичный сбор кнопкой ставит итог заявки раньше конца — пометка остаётся."""
    _routes(monkeypatch, {"pharmonline": ["cat"], "aloe": ["cat"]})
    calls = _patch_verified_aloe_pipeline(db_session, monkeypatch)
    request_id = _request(db_session, mode="category", category_id=7)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main_mod.cli, _watcher_run_args(request_id, category_id=7))

    assert result.exit_code == 0, result.output
    assert calls == [["aloe"]]
    saved = _saved_request(db_session, request_id)
    assert saved.status == "ok"
    assert saved.error_message == main_mod._PHARMONLINE_LEFT_TO_PUBLIC_API_NOTE
