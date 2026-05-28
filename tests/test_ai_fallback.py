"""Tests for Phase 1.4 — AI crawler fallback orchestration.

Покрываем decision logic _should_trigger_ai_fallback + baselines_for_sites
DB-query helper. Сам merge тестируется интеграционно (мокаем AICrawler.crawl).
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main, storage  # noqa: E402
from src._time import utcnow  # noqa: E402


def _ensure_fallback_disabled(monkeypatch):
    monkeypatch.delenv(main.AI_FALLBACK_ENABLED_ENV, raising=False)


def test_fallback_disabled_when_env_unset(monkeypatch):
    _ensure_fallback_disabled(monkeypatch)
    assert main._should_trigger_ai_fallback(primary_yield=0, baseline=1000) is False


def test_fallback_disabled_when_env_false(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "false")
    assert main._should_trigger_ai_fallback(primary_yield=0, baseline=1000) is False


def test_fallback_disabled_without_baseline(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    assert main._should_trigger_ai_fallback(primary_yield=0, baseline=None) is False


def test_fallback_disabled_when_baseline_too_small(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main.AI_FALLBACK_MIN_BASELINE_ENV, "100")
    # baseline = 50, below the 100 floor — signal too noisy
    assert main._should_trigger_ai_fallback(primary_yield=0, baseline=50) is False


def test_fallback_triggers_below_ratio(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main.AI_FALLBACK_RATIO_ENV, "0.5")
    # baseline=1000, ratio=0.5 → threshold=500. Yield=300 < 500.
    assert main._should_trigger_ai_fallback(primary_yield=300, baseline=1000) is True


def test_fallback_skips_when_primary_recovered(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main.AI_FALLBACK_RATIO_ENV, "0.5")
    # Yield=800 > threshold=500 — primary good enough
    assert main._should_trigger_ai_fallback(primary_yield=800, baseline=1000) is False


def test_fallback_custom_ratio_strict(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main.AI_FALLBACK_RATIO_ENV, "0.9")  # be strict: 90% required
    # Yield=850 < 900 (0.9 * 1000) → triggers
    assert main._should_trigger_ai_fallback(primary_yield=850, baseline=1000) is True


def test_fallback_handles_bad_ratio_env(monkeypatch):
    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setenv(main.AI_FALLBACK_RATIO_ENV, "not-a-float")
    # Falls back to default 0.5, threshold=500
    assert main._should_trigger_ai_fallback(primary_yield=400, baseline=1000) is True


def test_baselines_for_sites_no_history(db_session):
    """No prior runs → returns {site: None} for all requested."""
    out = main.baselines_for_sites(db_session, ["pharmonline", "aloe"])
    assert out == {"pharmonline": None, "aloe": None}


def test_baselines_for_sites_uses_latest_ok_run(db_session):
    """Pulls products_per_site from latest status=ok run."""
    # Старый failed run — игнорируется
    failed = storage.Run(
        status="failed", products_per_site={"pharmonline": 9999},
        started_at=utcnow(), finished_at=utcnow(),
    )
    db_session.add(failed)
    db_session.flush()
    # Свежий ok run — берётся
    ok = storage.Run(
        status="ok",
        products_per_site={"pharmonline": 4500, "aptekonline": 2200, "aloe": 1100},
        started_at=utcnow(), finished_at=utcnow(),
    )
    db_session.add(ok)
    db_session.commit()

    out = main.baselines_for_sites(db_session, ["pharmonline", "aptekonline", "aloe"])
    assert out == {"pharmonline": 4500, "aptekonline": 2200, "aloe": 1100}


def test_baselines_ignores_zero_and_missing(db_session):
    """Zero/missing products_per_site values → None, not 0."""
    ok = storage.Run(
        status="ok",
        products_per_site={"pharmonline": 0, "aptekonline": 1500},  # aloe missing
        started_at=utcnow(), finished_at=utcnow(),
    )
    db_session.add(ok)
    db_session.commit()

    out = main.baselines_for_sites(db_session, ["pharmonline", "aptekonline", "aloe"])
    assert out["pharmonline"] is None  # 0 ignored
    assert out["aptekonline"] == 1500
    assert out["aloe"] is None  # missing key
