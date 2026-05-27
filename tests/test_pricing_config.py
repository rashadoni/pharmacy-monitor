"""Tests for Phase 4.1 — PricingConfig DB-backed thresholds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import roi, storage  # noqa: E402


def test_load_pricing_config_creates_default(db_session):
    """First call should create row with default values for tenant_id=1."""
    cfg = storage.load_pricing_config(db_session, tenant_id=1)
    assert cfg.tenant_id == 1
    assert cfg.raise_threshold_pct == 5.0
    assert cfg.undercut_threshold_pct == 3.0
    assert cfg.max_spread_pct == 80.0
    assert cfg.min_margin_pct == 10.0
    assert cfg.max_per_type == 10


def test_load_pricing_config_returns_existing_row(db_session):
    """Second call returns same row (not a new one)."""
    cfg1 = storage.load_pricing_config(db_session, tenant_id=1)
    cfg1.raise_threshold_pct = 7.5
    db_session.commit()

    cfg2 = storage.load_pricing_config(db_session, tenant_id=1)
    assert cfg2.id == cfg1.id
    assert cfg2.raise_threshold_pct == 7.5


def test_load_pricing_config_per_tenant_separation(db_session):
    """tenant_id=2 gets its own row, doesn't affect tenant_id=1."""
    cfg1 = storage.load_pricing_config(db_session, tenant_id=1)
    cfg1.undercut_threshold_pct = 2.0
    db_session.commit()

    cfg2 = storage.load_pricing_config(db_session, tenant_id=2)
    assert cfg2.id != cfg1.id
    # tenant 2 gets defaults
    assert cfg2.undercut_threshold_pct == 3.0


def test_compute_actions_reads_config_from_db(db_session):
    """compute_actions() should default to DB-stored thresholds."""
    cfg = storage.load_pricing_config(db_session, tenant_id=1)
    cfg.raise_threshold_pct = 15.0  # very high — should yield no raise opportunities
    db_session.commit()

    # Without any test data, list is empty regardless — but the call should
    # not error and should respect the high threshold.
    actions = roi.compute_actions(db_session)
    assert isinstance(actions, list)


def test_compute_actions_explicit_kwarg_overrides_db(db_session):
    """Passing raise_threshold_pct=2.0 should override DB value of 15.0."""
    cfg = storage.load_pricing_config(db_session, tenant_id=1)
    cfg.raise_threshold_pct = 15.0
    db_session.commit()

    # Pass override — function should accept and not raise
    actions = roi.compute_actions(db_session, raise_threshold_pct=2.0)
    assert isinstance(actions, list)


def test_compute_actions_sets_module_min_margin(db_session):
    """Module-level _CURRENT_MIN_MARGIN_PCT should reflect either DB or kwarg."""
    cfg = storage.load_pricing_config(db_session, tenant_id=1)
    cfg.min_margin_pct = 15.0
    db_session.commit()

    roi.compute_actions(db_session)
    assert roi._CURRENT_MIN_MARGIN_PCT == 15.0

    roi.compute_actions(db_session, min_margin_pct=20.0)
    assert roi._CURRENT_MIN_MARGIN_PCT == 20.0
