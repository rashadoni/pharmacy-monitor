"""Доверенная цена: diff-only запись против «деньги — только по проверенным сборам».

`price_snapshots` — журнал изменений: строка пишется, только когда цена
отличается от последней записанной. Денежные читатели берут строки только
проверенных полных сборов. До 2026-10-07 цена, которую первым записал прогон
без проверки (до её появления, частичный тик, сбор, её не прошедший), оставалась
недоверенной, пока не изменится: проверенный сбор видел ту же цену и ничего не
писал. Замер на проде: 85% товаров без проверенной цены, 112 строк сравнения
вместо 3 789, алерт «конкурент дешевле» — 5 в неделю вместо ~200.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from src import alerts, storage
from src import main as main_mod
from src._time import utcnow
from src.main import persist_results
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult

ROOT = Path(__file__).resolve().parents[1]


def _scraped(price: float, ext_id: str = "1", site: str = "aloe") -> ScrapedProduct:
    return ScrapedProduct(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=f"Product {ext_id}",
        price=price,
    )


def _observe(
    db_session, *, scope: str, price: float = 10.0, ext_id: str = "1", site: str = "aloe"
) -> storage.Run:
    """Прогон увидел товар по цене `price`; запись — штатным `persist_results`."""
    run = storage.Run(status="running", catalog_scope=scope, started_at=utcnow())
    db_session.add(run)
    db_session.commit()
    persist_results(
        db_session, run, [ScrapeResult(site=site, products=[_scraped(price, ext_id, site)])]
    )
    return run


def _verify(db_session, run: storage.Run, site: str = "aloe", *, confirm: bool = True) -> int:
    """Сбор прошёл проверку каталога: пайплайн публикует его и подтверждает цены.

    `confirm=False` — прогон из истории до 2026-10-07: проверен, но цен не подтверждал.
    """
    run.status = "ok"
    run.finished_at = utcnow()
    run.catalog_scope = "full"
    run.full_catalog_sites = site
    run.catalog_verified = True
    run.run_quality = {"financially_eligible": True, "sites": {site: {"status": "ok"}}}
    db_session.commit()
    if not confirm:
        return 0
    confirmed = storage.confirm_prices_observed_by_run(db_session, run)
    db_session.commit()
    return confirmed


def _rows(db_session) -> list[tuple[int, float, int | None]]:
    return [
        (snap.run_id, snap.price, snap.confirmed_run_id)
        for snap in db_session.scalars(
            select(storage.PriceSnapshot).order_by(storage.PriceSnapshot.id)
        )
    ]


def _trusted_price(db_session, ext_id: str = "1") -> float | None:
    product = db_session.scalar(
        select(storage.Product).where(storage.Product.external_id == ext_id)
    )
    prices = storage.latest_prices_per_product(
        db_session, [product.id], financially_eligible_only=True
    )
    return prices[product.id][0] if product.id in prices else None


# ─── Подтверждение ───────────────────────────────────────────────────────────


def test_verified_scan_confirms_price_first_recorded_by_untrusted_run(db_session):
    """Главная регрессия: цена не менялась, и проверенной записи о ней не было."""
    tick = _observe(db_session, scope="partial")
    full = _observe(db_session, scope="full")
    assert _rows(db_session) == [(tick.id, 10.0, None)], "diff-only: второй строки нет"
    assert _trusted_price(db_session) is None

    assert _verify(db_session, full) == 1

    assert _rows(db_session) == [(tick.id, 10.0, full.id)], "строка та же, только помечена"
    assert _trusted_price(db_session) == 10.0


def test_scan_that_failed_verification_confirms_nothing(db_session):
    tick = _observe(db_session, scope="partial")
    failed = _observe(db_session, scope="full")
    failed.status = "degraded"
    failed.finished_at = utcnow()
    db_session.commit()

    assert storage.confirm_prices_observed_by_run(db_session, failed) == 0
    assert _rows(db_session) == [(tick.id, 10.0, None)]
    assert _trusted_price(db_session) is None


def test_repeated_scans_never_add_rows_or_rewrite_a_valid_confirmation(db_session):
    tick = _observe(db_session, scope="partial")
    first = _observe(db_session, scope="full")
    _verify(db_session, first)

    for _ in range(3):
        failed = _observe(db_session, scope="full")
        failed.status = "degraded"
        db_session.commit()
        assert storage.confirm_prices_observed_by_run(db_session, failed) == 0
    assert _verify(db_session, _observe(db_session, scope="full")) == 0

    assert _rows(db_session) == [(tick.id, 10.0, first.id)]


def test_scan_that_fails_after_confirming_does_not_erase_earlier_trust(db_session):
    """Сбор подтвердил цены, а потом упал в постобработке."""
    _observe(db_session, scope="partial")
    first = _observe(db_session, scope="full")
    _verify(db_session, first)

    second = _observe(db_session, scope="full")
    _verify(db_session, second)
    second.status = "failed"
    db_session.commit()

    assert _trusted_price(db_session) == 10.0


def test_next_verified_scan_reconfirms_after_confirming_scan_lost_trust(db_session):
    tick = _observe(db_session, scope="partial")
    first = _observe(db_session, scope="full")
    _verify(db_session, first)
    first.status = "failed"
    db_session.commit()
    assert _trusted_price(db_session) is None

    second = _observe(db_session, scope="full")
    assert _verify(db_session, second) == 1

    assert _rows(db_session) == [(tick.id, 10.0, second.id)]
    assert _trusted_price(db_session) == 10.0


def test_change_first_seen_by_partial_tick_becomes_trusted_without_a_new_row(db_session):
    first = _observe(db_session, scope="full")
    _verify(db_session, first)
    tick = _observe(db_session, scope="partial", price=12.0)
    assert _trusted_price(db_session) == 10.0, "тик денежным выводам не доверен"

    second = _observe(db_session, scope="full", price=12.0)
    _verify(db_session, second)

    assert _rows(db_session) == [(first.id, 10.0, None), (tick.id, 12.0, second.id)]
    assert _trusted_price(db_session) == 12.0


def test_price_that_left_and_came_back_between_verified_scans(db_session):
    first = _observe(db_session, scope="full")
    _verify(db_session, first)
    away = _observe(db_session, scope="partial", price=12.0)
    back = _observe(db_session, scope="partial", price=10.0)

    second = _observe(db_session, scope="full", price=10.0)
    _verify(db_session, second)

    assert _rows(db_session) == [
        (first.id, 10.0, None),
        (away.id, 12.0, None),
        (back.id, 10.0, second.id),
    ]
    assert _trusted_price(db_session) == 10.0


def test_scan_does_not_confirm_a_price_it_saw_changed(db_session):
    """Сбор записал новую цену сам — прежняя запись остаётся недоверенной."""
    tick = _observe(db_session, scope="partial", price=10.0)
    full = _observe(db_session, scope="full", price=8.0)

    assert _verify(db_session, full) == 0

    assert _rows(db_session) == [(tick.id, 10.0, None), (full.id, 8.0, None)]
    assert _trusted_price(db_session) == 8.0


def test_scan_confirms_what_it_saw_not_what_was_recorded_afterwards(db_session):
    """Подтверждение запоздало, и после сбора цену успел поменять другой прогон."""
    tick = _observe(db_session, scope="partial", price=10.0)
    full = _observe(db_session, scope="full", price=10.0)
    later = _observe(db_session, scope="partial", price=12.0)

    assert _verify(db_session, full) == 1

    assert _rows(db_session) == [(tick.id, 10.0, full.id), (later.id, 12.0, None)]
    assert _trusted_price(db_session) == 10.0


def test_scan_confirms_only_products_it_observed(db_session):
    _observe(db_session, scope="partial", ext_id="seen")
    _observe(db_session, scope="partial", ext_id="not-seen")
    full = _observe(db_session, scope="full", ext_id="seen")

    assert _verify(db_session, full) == 1

    assert _trusted_price(db_session, "seen") == 10.0
    assert _trusted_price(db_session, "not-seen") is None


def test_scan_that_persists_a_product_twice_confirms_it_once(db_session):
    """Пайплайн пишет товар дважды за прогон: по категории и в конце целиком."""
    tick = _observe(db_session, scope="partial")
    full = _observe(db_session, scope="full")
    persist_results(db_session, full, [ScrapeResult(site="aloe", products=[_scraped(10.0)])])

    assert _verify(db_session, full) == 1
    assert _rows(db_session) == [(tick.id, 10.0, full.id)]


def test_rows_with_the_same_timestamp_are_resolved_like_the_readers_do(db_session):
    """Товар дважды в одном чанке с разными ценами: последняя запись — с большим id.
    Сравнение, подтверждение и чтение обязаны выбрать одну и ту же."""
    tick = storage.Run(status="running", catalog_scope="partial", started_at=utcnow())
    db_session.add(tick)
    db_session.commit()
    persist_results(
        db_session, tick, [ScrapeResult(site="aloe", products=[_scraped(12.0), _scraped(10.0)])]
    )
    first, second = db_session.scalars(
        select(storage.PriceSnapshot).order_by(storage.PriceSnapshot.id)
    ).all()
    assert first.captured_at == second.captured_at

    full = _observe(db_session, scope="full", price=10.0)
    assert _verify(db_session, full) == 1

    assert _rows(db_session) == [(tick.id, 12.0, None), (tick.id, 10.0, full.id)]
    assert _trusted_price(db_session) == 10.0


def test_first_scan_after_rollout_confirms_previous_verified_scan_of_its_site(db_session):
    """Проверенные сборы до 2026-10-07 цен не подтверждали. Без их подтверждения
    задним числом первый новый сбор сайта не смог бы оценить падение цены."""
    legacy = _observe(db_session, scope="partial", price=10.0)
    old_scan = _observe(db_session, scope="full", price=10.0)
    _verify(db_session, old_scan, confirm=False)
    product = db_session.scalars(select(storage.Product)).one()
    product.offer_availability_status = "in_stock"
    product.availability_observed_at = utcnow()

    new_scan = _observe(db_session, scope="full", price=8.0)
    _verify(db_session, new_scan, confirm=False)
    confirmed = storage.confirm_prices_of_latest_verified_runs(db_session, new_scan)
    db_session.commit()

    assert confirmed == {old_scan.id: 1, new_scan.id: 0}
    assert _rows(db_session) == [(legacy.id, 10.0, old_scan.id), (new_scan.id, 8.0, None)]
    _add_rule(db_session, "price_drop_pct", {"min_pct": 10.0})
    fired = alerts.evaluate_rules(db_session, new_scan.id)
    assert [(event.payload["prev_price"], event.payload["curr_price"]) for event in fired] == [
        (10.0, 8.0)
    ]


# ─── Читатели ────────────────────────────────────────────────────────────────


def _add_rule(db_session, rule_type: str, params: dict | None = None) -> None:
    db_session.add(
        storage.AlertRule(
            name=rule_type, rule_type=rule_type, params=params or {}, channels=["email"]
        )
    )
    db_session.commit()


def test_price_drop_is_measured_from_a_confirmed_price(db_session):
    """2026-10-04 проверенный сбор aptekonline пропустил 329 падений ≥10% из 450:
    прежнюю цену записал прогон без проверки, и сравнивать было не с чем."""
    _observe(db_session, scope="partial", price=10.0)
    _verify(db_session, _observe(db_session, scope="full", price=10.0))
    product = db_session.scalars(select(storage.Product)).one()
    product.offer_availability_status = "in_stock"
    product.availability_observed_at = utcnow()

    dropped = _observe(db_session, scope="full", price=8.0)
    _verify(db_session, dropped)
    _add_rule(db_session, "price_drop_pct", {"min_pct": 10.0})

    fired = alerts.evaluate_rules(db_session, dropped.id)
    assert [(event.rule_type, event.payload["prev_price"]) for event in fired] == [
        ("price_drop_pct", 10.0)
    ]


def test_confirmation_alone_raises_no_alert(db_session):
    """Подтверждение — не событие: письма о давно случившемся не уходят."""
    _verify(db_session, _observe(db_session, scope="full", price=10.0))
    _observe(db_session, scope="partial", price=8.0)

    confirming = _observe(db_session, scope="full", price=8.0)
    _verify(db_session, confirming)
    for rule_type in ("price_drop_pct", "price_change_pct", "new_product"):
        _add_rule(db_session, rule_type, {"min_pct": 5.0})

    assert alerts.evaluate_rules(db_session, confirming.id) == []
    assert _trusted_price(db_session) == 8.0


def test_every_trusted_only_query_goes_through_the_shared_filter():
    """Запрос, фильтрующий snapshot'ы по `run_id IN (проверенные)` напрямую,
    снова потеряет подтверждённые цены — и снова молча."""
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, function: str | None, path: Path = path) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                function = node.name
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"in_", "not_in"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "run_id"
                and "PriceSnapshot" in ast.unparse(node.func.value.value)
                and function not in {"trusted_snapshot_filter", "confirm_prices_observed_by_run"}
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} ({function})")
            for child in ast.iter_child_nodes(node):
                visit(child, function)

        visit(tree, None)

    assert offenders == [], "используйте storage.trusted_snapshot_filter"


# ─── Пайплайн ────────────────────────────────────────────────────────────────


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


def _run_aloe_pipeline(db_session, monkeypatch, *, complete: bool) -> list[float | None]:
    """Полный сбор aloe через штатную команду `run`; что видят алерты — в ответе."""
    seen_by_alerts: list[float | None] = []

    async def fake_scrape_all(*args, **kwargs):
        status = (
            RouteStatus(complete=True, raw_items=1, parsed_items=1, expected_items=1)
            if complete
            else RouteStatus(
                complete=False,
                abort_reason="item_parse_failures",
                raw_items=2,
                parsed_items=1,
                item_failures=1,
                expected_items=2,
            )
        )
        product = _scraped(10.0)
        product.category = "cat"
        return [
            ScrapeResult(
                site="aloe",
                products=[product],
                category_counts={"cat": 1},
                route_statuses={"cat": status},
            )
        ]

    def fake_evaluate(session, run_id):
        seen_by_alerts.append(_trusted_price(session))
        return []

    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 1})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", lambda session: [])
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)
    monkeypatch.setattr(alerts, "evaluate_rules", fake_evaluate)
    monkeypatch.setattr(
        main_mod.analyzer, "analyze", lambda *args: SimpleNamespace(run_started_at=utcnow())
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main_mod.cli, ["run", "--site", "aloe", "--mode", "category"])
    assert (result.exit_code == 0) is complete, result.output
    return seen_by_alerts


def test_pipeline_confirms_prices_of_a_verified_scan_before_alerts(db_session, monkeypatch):
    legacy = _observe(db_session, scope="partial", price=10.0)
    legacy.status = "ok"
    legacy.finished_at = utcnow() - timedelta(days=30)
    db_session.commit()

    seen_by_alerts = _run_aloe_pipeline(db_session, monkeypatch, complete=True)

    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert (run.status, run.catalog_verified) == ("ok", True)
    assert _rows(db_session) == [(legacy.id, 10.0, run.id)]
    assert seen_by_alerts == [10.0], "алерты должны читать уже подтверждённую цену"


def test_pipeline_also_confirms_last_verified_scan_of_other_sites(db_session, monkeypatch):
    """Первый проверенный сбор после выкладки закрывает дыру по всем сайтам:
    undercut-алерту сбора aloe нужна доверенная цена клиента (pharmonline)."""
    legacy = _observe(db_session, scope="partial", price=7.0, ext_id="client", site="pharmonline")
    old_scan = _observe(db_session, scope="full", price=7.0, ext_id="client", site="pharmonline")
    _verify(db_session, old_scan, "pharmonline", confirm=False)
    assert _trusted_price(db_session, "client") is None

    _run_aloe_pipeline(db_session, monkeypatch, complete=True)

    db_session.expire_all()
    client_rows = [row for row in _rows(db_session) if row[0] == legacy.id]
    assert client_rows == [(legacy.id, 7.0, old_scan.id)]
    assert _trusted_price(db_session, "client") == 7.0


def test_pipeline_confirms_nothing_when_the_scan_fails_verification(db_session, monkeypatch):
    legacy = _observe(db_session, scope="partial", price=10.0)
    legacy.status = "ok"
    legacy.finished_at = utcnow() - timedelta(days=30)
    db_session.commit()

    _run_aloe_pipeline(db_session, monkeypatch, complete=False)

    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert run.status == "degraded"
    assert _rows(db_session) == [(legacy.id, 10.0, None)]
    assert _trusted_price(db_session) is None


# ─── Миграция ────────────────────────────────────────────────────────────────


def _alembic(db_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_confirmed_run_column_upgrade_and_downgrade(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'confirmed-run.sqlite'}"
    engine = create_engine(db_url)

    def columns() -> set[str]:
        return {column["name"] for column in inspect(engine).get_columns("price_snapshots")}

    _alembic(db_url, "upgrade", "0022_aloe_cosmetics_hygiene")
    # Migration 0001 builds a new database from the current models, so the
    # column is already there; production at 0022 does not have it. Rebuild the
    # table the way production has it before exercising the revision.
    with engine.begin() as connection:
        kept = ", ".join(sorted(columns() - {"confirmed_run_id"}))
        connection.execute(
            text(f"CREATE TABLE snapshots_at_0022 AS SELECT {kept} FROM price_snapshots")
        )
        connection.execute(text("DROP TABLE price_snapshots"))
        connection.execute(text("ALTER TABLE snapshots_at_0022 RENAME TO price_snapshots"))
    assert "confirmed_run_id" not in columns()

    _alembic(db_url, "upgrade", "0023_snapshot_confirmed_run")
    assert "confirmed_run_id" in columns()
    _alembic(db_url, "upgrade", "0023_snapshot_confirmed_run")  # повтор безвреден

    _alembic(db_url, "downgrade", "0022_aloe_cosmetics_hygiene")
    assert "confirmed_run_id" not in columns()
