"""Тесты testable helper'ов src/main.py.

CLI commands не покрываем — их легче тестировать через subprocess (отдельная
история). Покрываем helpers с чистой логикой / DB-операциями:
- _ai_fallback_enabled / _should_trigger_ai_fallback
- baselines_for_sites
- categories_for_site_from_db / maybe_seed_categories
- collect_watchlist_urls / auto_match_watchlist
- _per_category_breakdown
- _snapshot_payload_changed
"""

from __future__ import annotations

from datetime import datetime, timedelta

import click
from click.testing import CliRunner

from src import main as main_mod
from src import roi, storage, watchlist
from src.product_policy import policy_fingerprint, trusted_catalog_epoch
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult


class _FakeLockConnection:
    def __init__(self, acquired=True, fail_acquire=False):
        self.acquired = acquired
        self.fail_acquire = fail_acquire
        self.calls = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, params))
        if self.fail_acquire and "advisory_lock" in sql:
            raise RuntimeError("lock connection failed")
        if "pg_try_advisory_lock" in sql:
            return self.acquired
        return True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakeLockEngine:
    url = "postgresql://test"

    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


class _FakeLockFactory:
    def __init__(self, connection):
        self.kw = {"bind": _FakeLockEngine(connection)}


def test_scrape_lock_busy_closes_dedicated_connection():
    connection = _FakeLockConnection(acquired=False)

    with click.Context(click.Command("test")):
        acquired = main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(connection), wait=False
        )

    assert acquired is False
    assert connection.commits == 1
    assert connection.closed is True
    assert any("pg_try_advisory_lock" in sql for sql, _ in connection.calls)


def test_scrape_lock_released_when_click_context_closes():
    connection = _FakeLockConnection(acquired=True)
    context = click.Context(click.Command("test"))

    with context:
        acquired = main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(connection), wait=False
        )
        assert acquired is True
        assert connection.commits == 1
        assert connection.closed is False

    assert connection.commits == 2
    assert connection.closed is True
    assert any("pg_advisory_unlock" in sql for sql, _ in connection.calls)


def test_scrape_lock_acquire_exception_rolls_back_and_closes():
    import pytest

    connection = _FakeLockConnection(fail_acquire=True)
    with click.Context(click.Command("test")), pytest.raises(
        RuntimeError, match="lock connection failed"
    ):
        main_mod._hold_scrape_lock_until_command_exit(
            _FakeLockFactory(connection), wait=False
        )

    assert connection.rollbacks == 1
    assert connection.closed is True


# === AI fallback toggles ===


def test_ai_fallback_disabled_by_default(monkeypatch):
    monkeypatch.delenv(main_mod.AI_FALLBACK_ENABLED_ENV, raising=False)
    assert main_mod._ai_fallback_enabled() is False


def test_ai_fallback_enabled_via_env(monkeypatch):
    for val in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, val)
        assert main_mod._ai_fallback_enabled() is True


def test_ai_fallback_not_triggered_when_disabled(monkeypatch):
    monkeypatch.delenv(main_mod.AI_FALLBACK_ENABLED_ENV, raising=False)
    assert main_mod._should_trigger_ai_fallback(10, baseline=1000) is False


def test_ai_fallback_not_triggered_without_baseline(monkeypatch):
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    assert main_mod._should_trigger_ai_fallback(10, baseline=None) is False


def test_ai_fallback_not_triggered_if_baseline_too_small(monkeypatch):
    """baseline < min_baseline (default 100) → не trigger'ит."""
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.delenv(main_mod.AI_FALLBACK_MIN_BASELINE_ENV, raising=False)
    assert main_mod._should_trigger_ai_fallback(0, baseline=50) is False


def test_ai_fallback_triggers_when_yield_below_threshold(monkeypatch):
    """yield < ratio * baseline → trigger."""
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.delenv(main_mod.AI_FALLBACK_RATIO_ENV, raising=False)
    # baseline=1000, ratio=0.5 → threshold=500. yield=100 < 500 → trigger.
    assert main_mod._should_trigger_ai_fallback(100, baseline=1000) is True


def test_ai_fallback_not_triggered_when_yield_above_threshold(monkeypatch):
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    # yield=900 > threshold (500) → не trigger.
    assert main_mod._should_trigger_ai_fallback(900, baseline=1000) is False


def test_ai_fallback_respects_custom_ratio(monkeypatch):
    """`PHARMACY_AI_FALLBACK_RATIO=0.8` → strict mode."""
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main_mod.AI_FALLBACK_RATIO_ENV, "0.8")
    # threshold = 800. yield=700 < 800 → trigger.
    assert main_mod._should_trigger_ai_fallback(700, baseline=1000) is True
    assert main_mod._should_trigger_ai_fallback(900, baseline=1000) is False


def test_ai_fallback_invalid_ratio_falls_back_to_default(monkeypatch):
    """Невалидный float в env → дефолт 0.5."""
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main_mod.AI_FALLBACK_RATIO_ENV, "not-a-number")
    # Использует дефолтный 0.5
    assert main_mod._should_trigger_ai_fallback(400, baseline=1000) is True
    assert main_mod._should_trigger_ai_fallback(600, baseline=1000) is False


def test_ai_fallback_invalid_min_baseline_falls_back_to_default(monkeypatch):
    """Невалидный int в env → дефолт 100."""
    monkeypatch.setenv(main_mod.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main_mod.AI_FALLBACK_MIN_BASELINE_ENV, "abc")
    # Дефолт 100 — baseline=50 < 100 → не trigger
    assert main_mod._should_trigger_ai_fallback(0, baseline=50) is False
    # baseline=150 >= 100 + yield=10 < 75 → trigger
    assert main_mod._should_trigger_ai_fallback(10, baseline=150) is True


def test_reap_stale_running_runs_marks_old_orphans_failed(db_session):
    old = storage.Run(
        started_at=main_mod.utcnow() - timedelta(hours=8),
        status="running",
        products_scraped=0,
    )
    fresh = storage.Run(
        started_at=main_mod.utcnow() - timedelta(minutes=30),
        status="running",
        products_scraped=0,
    )
    ok = storage.Run(
        started_at=main_mod.utcnow() - timedelta(hours=9),
        finished_at=main_mod.utcnow() - timedelta(hours=8),
        status="ok",
        products_scraped=10,
    )
    classified_orphan = storage.Run(
        started_at=main_mod.utcnow() - timedelta(hours=7),
        status="ok",
        products_scraped=10,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
        },
    )
    legacy_failed = storage.Run(
        started_at=main_mod.utcnow() - timedelta(days=2),
        status="failed",
        products_scraped=0,
    )
    db_session.add_all([old, fresh, ok, classified_orphan, legacy_failed])
    db_session.commit()

    count = main_mod.reap_stale_running_runs(db_session, max_age_hours=6)

    assert count == 3
    db_session.refresh(old)
    db_session.refresh(fresh)
    db_session.refresh(ok)
    db_session.refresh(classified_orphan)
    db_session.refresh(legacy_failed)
    assert old.status == "failed"
    assert old.finished_at is not None
    assert "reaped stale unfinished run" in (old.error_message or "")
    assert fresh.status == "running"
    assert fresh.finished_at is None
    assert ok.status == "ok"
    assert ok.finished_at is not None
    assert classified_orphan.status == "failed"
    assert classified_orphan.finished_at is not None
    assert classified_orphan.run_quality["full_catalog_verified"] is False
    assert classified_orphan.run_quality["financially_eligible"] is False
    assert classified_orphan.run_quality["recovery"]["previous_status"] == "ok"
    assert classified_orphan.run_quality["recovery"]["recovered_at"]
    assert legacy_failed.status == "failed"
    assert legacy_failed.finished_at is not None
    assert legacy_failed.finished_at == legacy_failed.started_at
    assert "previous_status=failed" in (legacy_failed.error_message or "")
    assert "recovered_at=" in (legacy_failed.error_message or "")
    # The newer classified orphan honestly supersedes `ok`; the much older
    # legacy failed row must not jump to the front merely because it was reaped.
    assert storage.latest_terminal_run(db_session).id == classified_orphan.id
    assert storage.latest_terminal_run(db_session).id != legacy_failed.id


def _full_quality(*, eligible: bool = True) -> dict:
    status = "ok" if eligible else "failed"
    return {
        "baseline_enforced": True,
        "full_catalog_verified": eligible,
        "financially_eligible": eligible,
        "sites": {
            site: {"status": status}
            for site in storage.FULL_CATALOG_SITES
        },
    }


def test_reap_old_classified_orphan_preserves_newer_healthy_lineage_and_cache(db_session):
    now = main_mod.utcnow()
    orphan = storage.Run(
        started_at=now - timedelta(days=2),
        status="ok",
        run_quality=_full_quality(),
        catalog_scope="full",
        full_catalog_sites=",".join(storage.FULL_CATALOG_SITES),
        catalog_verified=True,
    )
    healthy = storage.Run(
        started_at=now - timedelta(hours=20),
        finished_at=now - timedelta(hours=19),
        status="ok",
        run_quality=_full_quality(),
        catalog_scope="full",
        full_catalog_sites=",".join(storage.FULL_CATALOG_SITES),
        catalog_verified=True,
    )
    db_session.add_all([orphan, healthy])
    db_session.flush()
    epoch = trusted_catalog_epoch(db_session)
    assert epoch is not None
    db_session.add(
        storage.RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"title": "trusted"}],
            computed_at=now,
            run_id=healthy.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=epoch,
        )
    )
    db_session.commit()

    assert main_mod.reap_stale_running_runs(db_session, max_age_hours=6) == 1

    attempts = storage.latest_full_catalog_attempts_by_site(
        db_session,
        storage.FULL_CATALOG_SITES,
    )
    assert storage.latest_terminal_run(db_session).id == healthy.id
    assert {site: run.id for site, run in attempts.items()} == {
        site: healthy.id for site in storage.FULL_CATALOG_SITES
    }
    assert roi.get_cached_actions(db_session, "pharmonline") == [{"title": "trusted"}]


def test_reap_newer_classified_orphan_supersedes_older_healthy_lineage(db_session):
    now = main_mod.utcnow()
    healthy = storage.Run(
        started_at=now - timedelta(hours=20),
        finished_at=now - timedelta(hours=19),
        status="ok",
        run_quality=_full_quality(),
        catalog_scope="full",
        full_catalog_sites=",".join(storage.FULL_CATALOG_SITES),
        catalog_verified=True,
    )
    orphan = storage.Run(
        started_at=now - timedelta(hours=7),
        status="ok",
        run_quality=_full_quality(),
        catalog_scope="full",
        full_catalog_sites=",".join(storage.FULL_CATALOG_SITES),
        catalog_verified=True,
    )
    db_session.add_all([healthy, orphan])
    db_session.flush()
    epoch = trusted_catalog_epoch(db_session)
    assert epoch is not None
    db_session.add(
        storage.RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"title": "superseded"}],
            computed_at=now,
            run_id=healthy.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=epoch,
        )
    )
    db_session.commit()

    assert main_mod.reap_stale_running_runs(db_session, max_age_hours=6) == 1

    attempts = storage.latest_full_catalog_attempts_by_site(
        db_session,
        storage.FULL_CATALOG_SITES,
    )
    assert storage.latest_terminal_run(db_session).id == orphan.id
    assert {site: run.id for site, run in attempts.items()} == {
        site: orphan.id for site in storage.FULL_CATALOG_SITES
    }
    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_reap_stale_running_runs_noops_when_none_stale(db_session):
    db_session.add(
        storage.Run(
            started_at=main_mod.utcnow() - timedelta(minutes=10),
            status="running",
            products_scraped=0,
        )
    )
    db_session.commit()

    assert main_mod.reap_stale_running_runs(db_session, max_age_hours=6) == 0


def test_reap_stale_runs_cli_refuses_when_scrape_lock_is_busy(monkeypatch):
    sentinel_factory = object()
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: sentinel_factory)
    monkeypatch.setattr(
        main_mod,
        "_hold_scrape_lock_until_command_exit",
        lambda factory, *, wait: factory is not sentinel_factory,
    )

    result = CliRunner().invoke(main_mod.cli, ["reap-stale-runs"])

    assert result.exit_code != 0
    assert "active scrape/rematch producer" in result.output


def test_count_duplicate_products_executes_on_product_table(db_session):
    db_session.add_all(
        [
            storage.Product(
                site="pharmonline",
                external_id="ph-1",
                url="https://example.test/ph-1",
                name="A",
                name_normalized="a",
            ),
            storage.Product(
                site="aloe",
                external_id="al-1",
                url="https://example.test/al-1",
                name="A",
                name_normalized="a",
            ),
        ]
    )
    db_session.commit()

    assert main_mod.count_duplicate_products(db_session) == 0


# === scrape report email toggle ===


def test_report_email_enabled_by_default(monkeypatch):
    """Env не задан → письмо шлётся (обратная совместимость)."""
    monkeypatch.delenv(main_mod.SCRAPE_REPORT_EMAIL_ENV, raising=False)
    assert main_mod._report_email_enabled() is True


def test_report_email_disabled_via_env(monkeypatch):
    """Явно falsy значение → отключено (вкл. регистр/пробелы)."""
    for val in ("0", "false", "no", "off", "FALSE", "Off", " 0 "):
        monkeypatch.setenv(main_mod.SCRAPE_REPORT_EMAIL_ENV, val)
        assert main_mod._report_email_enabled() is False, val


def test_report_email_enabled_for_non_falsy(monkeypatch):
    """Любое не-falsy значение → письмо шлётся (только явный opt-out отключает)."""
    for val in ("1", "true", "yes", "on", "anything"):
        monkeypatch.setenv(main_mod.SCRAPE_REPORT_EMAIL_ENV, val)
        assert main_mod._report_email_enabled() is True, val


# === baselines_for_sites ===


def test_baselines_returns_none_when_no_runs(db_session):
    """Нет ok runs → все sites get None."""
    out = main_mod.baselines_for_sites(db_session, ["pharmonline", "aloe"])
    assert out == {"pharmonline": None, "aloe": None}


def test_baselines_returns_per_site_counts(db_session):
    """Last ok run.products_per_site → проброс в out."""
    run = storage.Run(
        started_at=datetime.utcnow(),
        status="ok",
        products_per_site={"pharmonline": 5000, "aloe": 2000},
    )
    db_session.add(run)
    db_session.commit()
    out = main_mod.baselines_for_sites(db_session, ["pharmonline", "aloe", "aptekonline"])
    assert out["pharmonline"] == 5000
    assert out["aloe"] == 2000
    assert out["aptekonline"] is None  # отсутствует в last run


def test_baselines_skips_zero_or_negative_counts(db_session):
    """0 или negative → не используем (None)."""
    run = storage.Run(
        started_at=datetime.utcnow(),
        status="ok",
        products_per_site={"pharmonline": 0, "aloe": -1},
    )
    db_session.add(run)
    db_session.commit()
    out = main_mod.baselines_for_sites(db_session, ["pharmonline", "aloe"])
    assert out == {"pharmonline": None, "aloe": None}


def test_baselines_uses_only_ok_status(db_session):
    """Failed run не используется."""
    fail_run = storage.Run(
        started_at=datetime.utcnow(),
        status="failed",
        products_per_site={"pharmonline": 9999},
    )
    db_session.add(fail_run)
    db_session.commit()
    out = main_mod.baselines_for_sites(db_session, ["pharmonline"])
    assert out == {"pharmonline": None}


def test_baselines_picks_latest_ok_run(db_session):
    """Если несколько ok runs — берём latest."""
    older = storage.Run(
        started_at=datetime.utcnow() - timedelta(days=2),
        status="ok",
        products_per_site={"pharmonline": 1000},
    )
    newer = storage.Run(
        started_at=datetime.utcnow(),
        status="ok",
        products_per_site={"pharmonline": 5000},
    )
    db_session.add_all([older, newer])
    db_session.commit()
    out = main_mod.baselines_for_sites(db_session, ["pharmonline"])
    assert out == {"pharmonline": 5000}


# === categories_for_site_from_db / maybe_seed_categories ===


def test_categories_for_site_from_db_passes_through(db_session):
    """Delegates to watchlist.categories_for_site."""
    watchlist.add_category(
        db_session,
        key="med",
        label_ru="Лекарства",
        pharmonline_slug="ph-med",
        is_active=True,
    )
    out = main_mod.categories_for_site_from_db(db_session, "pharmonline")
    assert out == ["ph-med"]


def test_maybe_seed_categories_no_op_if_categories_exist(db_session, monkeypatch):
    """Если в БД уже есть категории → seed не вызывается."""
    watchlist.add_category(db_session, key="existing", label_ru="X")
    called = []
    monkeypatch.setattr(watchlist, "seed_categories_from_yaml", lambda s, p: called.append(p))
    main_mod.maybe_seed_categories(db_session)
    assert called == []


def test_maybe_seed_categories_no_op_if_no_yaml(db_session, monkeypatch, tmp_path):
    """Если CONFIG_PATH не существует → seed не вызывается."""
    monkeypatch.setattr(main_mod, "CONFIG_PATH", tmp_path / "nope.yaml")
    called = []
    monkeypatch.setattr(watchlist, "seed_categories_from_yaml", lambda s, p: called.append(p))
    main_mod.maybe_seed_categories(db_session)
    assert called == []


# === collect_watchlist_urls ===


def test_collect_watchlist_urls_groups_by_site(db_session):
    """confirmed URLs группируются по site."""
    watchlist.add_tracked_product(
        db_session,
        canonical_name="Test SKU",
        pharmonline_url="https://pharmonline.az/p/1",
        aloe_url="https://aloe.az/p/1",
    )
    # add_tracked_product ставит status=confirmed для links с URL автоматически

    out = main_mod.collect_watchlist_urls(db_session)
    assert "https://pharmonline.az/p/1" in out["pharmonline"]
    assert "https://aloe.az/p/1" in out["aloe"]


def test_collect_watchlist_urls_skips_pending_status(db_session):
    """pending-status links не собираются — только confirmed."""
    tp = watchlist.add_tracked_product(
        db_session,
        canonical_name="Pending SKU",
        pharmonline_url="https://pharmonline.az/p/2",
    )
    # add_tracked_product ставит confirmed для URL'ов — переключим обратно
    # в pending чтобы симулировать unconfirmed link
    for link in tp.links:
        link.status = "pending"
    db_session.commit()
    out = main_mod.collect_watchlist_urls(db_session)
    assert out["pharmonline"] == []


def test_collect_watchlist_urls_empty_when_no_tracked(db_session):
    out = main_mod.collect_watchlist_urls(db_session)
    assert all(v == [] for v in out.values())


# === auto_match_watchlist ===


def test_auto_match_watchlist_creates_match_and_links_products(db_session):
    """Создаёт Match + привязывает Product через URL."""
    tp = watchlist.add_tracked_product(
        db_session,
        canonical_name="Friso Gold 800g",
        brand="Friso",
        pharmonline_url="https://pharmonline.az/friso",
        aloe_url="https://aloe.az/friso",
    )
    # Создаём products с этими URL
    p1 = storage.Product(
        site="pharmonline",
        external_id="f-ph",
        url="https://pharmonline.az/friso",
        name="Friso PH",
        name_normalized="friso ph",
    )
    p2 = storage.Product(
        site="aloe",
        external_id="f-al",
        url="https://aloe.az/friso",
        name="Friso AL",
        name_normalized="friso al",
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    linked = main_mod.auto_match_watchlist(db_session)
    assert linked == 2
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.canonical_id == p2.canonical_id
    # Match — manual
    match = db_session.get(storage.Match, p1.canonical_id)
    assert match.is_manual is True
    assert match.canonical_name == "Friso Gold 800g"


def test_auto_match_watchlist_idempotent(db_session):
    """Повторный вызов не создаёт дубли — увеличивает 0."""
    watchlist.add_tracked_product(
        db_session,
        canonical_name="X",
        pharmonline_url="https://ph.az/x",
    )
    p = storage.Product(
        site="pharmonline",
        external_id="x",
        url="https://ph.az/x",
        name="X",
        name_normalized="x",
    )
    db_session.add(p)
    db_session.commit()
    first = main_mod.auto_match_watchlist(db_session)
    second = main_mod.auto_match_watchlist(db_session)
    assert first == 1
    assert second == 0  # уже привязан


def test_auto_match_watchlist_skips_missing_products(db_session):
    """TrackedProduct с URL'ами но без существующих Product'ов → 0."""
    watchlist.add_tracked_product(
        db_session,
        canonical_name="Ghost SKU",
        pharmonline_url="https://ph.az/ghost",
    )
    # Никакого Product с этим URL не создавали
    n = main_mod.auto_match_watchlist(db_session)
    assert n == 0


def test_auto_match_watchlist_rejects_country_conflict_and_oos(db_session):
    watchlist.add_tracked_product(
        db_session,
        canonical_name="Strict SKU",
        pharmonline_url="https://ph.az/strict",
        aloe_url="https://aloe.az/strict",
        aptekonline_url="https://aptek.az/strict",
    )
    pharm = storage.Product(
        site="pharmonline",
        external_id="strict-ph",
        url="https://ph.az/strict",
        name="Strict SKU",
        name_normalized="strict sku",
        manufacturer_country_code="ua",
        country_resolution_status="resolved",
    )
    aloe = storage.Product(
        site="aloe",
        external_id="strict-aloe",
        url="https://aloe.az/strict",
        name="Strict SKU",
        name_normalized="strict sku",
        manufacturer_country_code="rs",
        country_resolution_status="resolved",
    )
    aptek = storage.Product(
        site="aptekonline",
        external_id="strict-aptek",
        url="https://aptek.az/strict",
        name="Strict SKU",
        name_normalized="strict sku",
        manufacturer_country_code="ua",
        country_resolution_status="resolved",
        offer_availability_status="out_of_stock",
        availability_observed_at=main_mod.utcnow(),
    )
    db_session.add_all([pharm, aloe, aptek])
    db_session.commit()

    linked = main_mod.auto_match_watchlist(db_session)

    assert linked == 1
    assert pharm.canonical_id is not None
    assert aloe.canonical_id is None
    assert aptek.canonical_id is None


def test_auto_match_watchlist_isolates_second_tenant(db_session):
    url = "https://ph.example/shared-url"
    watchlist.add_tracked_product(
        db_session,
        canonical_name="Tenant two SKU",
        pharmonline_url=url,
        tenant_id=2,
    )
    tenant_one = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="tenant-one-shared",
        url=url,
        name="Tenant one SKU",
        name_normalized="tenant one sku",
    )
    tenant_two = storage.Product(
        tenant_id=2,
        site="pharmonline",
        external_id="tenant-two-shared",
        url=url,
        name="Tenant two SKU",
        name_normalized="tenant two sku",
    )
    db_session.add_all([tenant_one, tenant_two])
    db_session.commit()

    linked = main_mod.auto_match_watchlist(db_session, tenant_id=2)

    assert linked == 1
    assert tenant_one.canonical_id is None
    assert tenant_two.canonical_id is not None
    match = db_session.get(storage.Match, tenant_two.canonical_id)
    assert match is not None
    assert match.tenant_id == 2


# === _per_category_breakdown ===


def _sp(name="X", category="meds"):
    return ScrapedProduct(
        site="pharmonline",
        external_id=name,
        url=f"http://x/{name}",
        name=name,
        price=10.0,
        category=category,
    )


def test_per_category_breakdown_counts():
    """Counter по category из всех ScrapeResults."""
    results = [
        ScrapeResult(
            site="pharmonline",
            products=[_sp("a", "meds"), _sp("b", "meds"), _sp("c", "cosmetics")],
        ),
        ScrapeResult(site="aloe", products=[_sp("d", "meds")]),
    ]
    out = main_mod._per_category_breakdown(results)
    assert out["meds"] == 3
    assert out["cosmetics"] == 1


def test_per_category_breakdown_uncategorized_bucket():
    """Product без category → '(uncategorized)' bucket."""
    sp = _sp("a", category=None)
    results = [ScrapeResult(site="pharmonline", products=[sp])]
    out = main_mod._per_category_breakdown(results)
    assert out["(uncategorized)"] == 1


def test_per_category_breakdown_empty():
    """Пустой список results → пустой dict."""
    assert main_mod._per_category_breakdown([]) == {}


def test_full_catalog_verifier_requires_every_category_route_nonzero():
    result = ScrapeResult(
        site="aloe",
        products=[_sp("a", "meds")],
        category_counts={"meds": 1, "cosmetics": 0},
    )

    verified, reason = main_mod._verify_full_catalog_results(
        [result],
        sites=["aloe"],
        expected_slugs={"aloe": ["meds", "cosmetics"]},
        baselines={"aloe": 1},
    )

    assert verified is False
    assert "zero_categories=aloe:1" in reason


def test_full_catalog_verifier_accepts_upstream_verified_empty_route():
    result = ScrapeResult(
        site="aptekonline",
        products=[_sp("a", "meds")],
        category_counts={"meds": 1, "empty": 0},
        route_statuses={
            "meds": RouteStatus(
                complete=True,
                expected_pages=1,
                visited_pages=1,
                raw_items=1,
                parsed_items=1,
                expected_items=1,
            ),
            "empty": RouteStatus(
                complete=True, expected_pages=1, visited_pages=1, expected_items=0
            ),
        },
    )

    verified, reason = main_mod._verify_full_catalog_results(
        [result],
        sites=["aptekonline"],
        expected_slugs={"aptekonline": ["meds", "empty"]},
        baselines={"aptekonline": 1},
    )

    assert verified is True
    assert reason == "complete_nonzero_routes_coverage_ok"


def test_full_catalog_verifier_rejects_below_ninety_percent_baseline():
    products = [_sp(str(index), "meds") for index in range(89)]
    result = ScrapeResult(
        site="aloe",
        products=products,
        category_counts={"meds": len(products)},
    )

    verified, reason = main_mod._verify_full_catalog_results(
        [result],
        sites=["aloe"],
        expected_slugs={"aloe": ["meds"]},
        baselines={"aloe": 100},
    )

    assert verified is False
    assert "coverage=aloe" in reason


def test_full_catalog_verifier_accepts_complete_routes_and_coverage():
    products = [_sp(str(index), "meds") for index in range(90)]
    result = ScrapeResult(
        site="aloe",
        products=products,
        category_counts={"meds": 45, "cosmetics": 45},
        route_statuses={
            "meds": RouteStatus(complete=True, expected_pages=4, visited_pages=4),
            "cosmetics": RouteStatus(
                complete=True, expected_pages=4, visited_pages=4
            ),
        },
    )

    verified, reason = main_mod._verify_full_catalog_results(
        [result],
        sites=["aloe"],
        expected_slugs={"aloe": ["meds", "cosmetics"]},
        baselines={"aloe": 100},
    )

    assert verified is True
    assert reason == "complete_nonzero_routes_coverage_ok"


def test_full_catalog_verifier_rejects_silent_page_skip():
    products = [_sp(str(index), "meds") for index in range(95)]
    result = ScrapeResult(
        site="aptekonline",
        products=products,
        category_counts={"meds": 95},
        route_statuses={
            "meds": RouteStatus(
                complete=False,
                pages_skipped=1,
                abort_reason="consecutive_page_failures",
                expected_pages=10,
                visited_pages=9,
            )
        },
    )

    verified, reason = main_mod._verify_full_catalog_results(
        [result],
        sites=["aptekonline"],
        expected_slugs={"aptekonline": ["meds"]},
        baselines={"aptekonline": 100},
    )

    assert verified is False
    assert "incomplete_routes=aptekonline:1" in reason


def test_aloe_country_mapping_is_durable_and_versioned(db_session):
    result = ScrapeResult(
        site="aloe",
        verified_country_mappings={
            "14": {
                "country_code": "gb",
                "country_raw": "Англия",
                "source_url": "https://aloe.az/ornafer/",
                "sample_count": 2,
            }
        },
    )

    assert main_mod.persist_aloe_country_mappings(db_session, [result]) == 1
    db_session.commit()
    loaded = main_mod.load_aloe_country_map(db_session)
    assert loaded["14"]["country_code"] == "gb"
    assert loaded["14"]["version"] == 1

    result.verified_country_mappings["14"].update(
        country_code="rs", country_raw="Сербия"
    )
    assert main_mod.persist_aloe_country_mappings(db_session, [result]) == 1
    db_session.commit()
    assert main_mod.load_aloe_country_map(db_session)["14"]["version"] == 2


# === _snapshot_payload_changed ===


def test_snapshot_changed_when_no_history():
    """last=None → True (нужно записать первый snapshot)."""
    sp = _sp("x")
    assert main_mod._snapshot_payload_changed(None, sp) is True


def test_snapshot_not_changed_when_identical():
    """Все поля совпадают → False (не пишем)."""
    sp = _sp("x")
    last = {
        "price": sp.price,
        "discount_price": sp.discount_price,
        "discount_percent": sp.discount_percent,
        "is_on_sale": sp.is_on_sale,
        "promo_label": sp.promo_label,
    }
    assert main_mod._snapshot_payload_changed(last, sp) is False


def test_snapshot_changed_when_price_differs():
    sp = _sp("x")
    last = {
        "price": sp.price + 1.0,  # цена другая
        "discount_price": sp.discount_price,
        "discount_percent": sp.discount_percent,
        "is_on_sale": sp.is_on_sale,
        "promo_label": sp.promo_label,
    }
    assert main_mod._snapshot_payload_changed(last, sp) is True


def test_snapshot_changed_when_promo_label_differs():
    sp = _sp("x")
    last = {
        "price": sp.price,
        "discount_price": sp.discount_price,
        "discount_percent": sp.discount_percent,
        "is_on_sale": sp.is_on_sale,
        "promo_label": "OLD PROMO",
    }
    assert main_mod._snapshot_payload_changed(last, sp) is True


def test_snapshot_changed_when_sale_flag_flips():
    sp = ScrapedProduct(
        site="pharmonline",
        external_id="x",
        url="http://x",
        name="X",
        price=10.0,
        is_on_sale=True,
    )
    last = {
        "price": sp.price,
        "discount_price": sp.discount_price,
        "discount_percent": sp.discount_percent,
        "is_on_sale": False,  # flipped
        "promo_label": sp.promo_label,
    }
    assert main_mod._snapshot_payload_changed(last, sp) is True


# ── _sync_pharmonline_categories (2026-05-29: фикс покрытия 53→205) ───────────


def test_sync_pharmonline_categories_adds_missing(db_session):
    s = db_session
    watchlist.add_category(
        s, key="pharma_existing", label_ru="Existing", pharmonline_slug="existing-slug"
    )
    discovered = [
        ("existing-slug", "Existing"),  # уже замаплен → skip
        ("new-cat-1", "Новая 1"),  # новый → add
        ("new-cat-2", "Новая 2"),  # новый → add
        ("", "пустой slug"),  # пустой → skip
    ]
    added, skipped = main_mod._sync_pharmonline_categories(s, discovered)
    assert added == 2
    assert skipped == 2
    c = watchlist.get_category(s, "pharma_new-cat-1")
    assert c is not None
    assert c.pharmonline_slug == "new-cat-1"
    assert c.label_ru == "Новая 1"


def test_sync_pharmonline_categories_idempotent(db_session):
    s = db_session
    discovered = [("cat-x", "X")]
    a1, _ = main_mod._sync_pharmonline_categories(s, discovered)
    a2, sk2 = main_mod._sync_pharmonline_categories(s, discovered)
    assert a1 == 1
    assert a2 == 0 and sk2 == 1  # повторный запуск ничего не добавляет


def test_intraday_product_limit_is_bounded(monkeypatch):
    monkeypatch.delenv("INTRADAY_PRODUCT_LIMIT", raising=False)
    assert main_mod._intraday_product_limit() == 600
    monkeypatch.setenv("INTRADAY_PRODUCT_LIMIT", "80")
    assert main_mod._intraday_product_limit() == 80
    monkeypatch.setenv("INTRADAY_PRODUCT_LIMIT", "0")
    assert main_mod._intraday_product_limit() == 1
    monkeypatch.setenv("INTRADAY_PRODUCT_LIMIT", "999999")
    assert main_mod._intraday_product_limit() == 600
    monkeypatch.setenv("INTRADAY_PRODUCT_LIMIT", "invalid")
    assert main_mod._intraday_product_limit() == 600
