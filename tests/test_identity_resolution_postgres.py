"""Closing isolation cases on real PostgreSQL: one SERIALIZABLE apply (required in CI)."""

import pytest
from sqlalchemy import func, select, text

from src import main as m
from src import pharmonline_identity_resolution as resolution
from src import storage
from tests.test_identity_isolation_postgres import pg_factory  # noqa: F401
from tests.test_pharmonline_identity_resolution import (
    MANUAL,
    _isolate,
    _manifest,
    _seed,
    _source,
)


def _apply_serializable(session, source, approved):
    session.commit()
    session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    m._apply_pharmonline_public_api_reconciliation(
        session,
        source,
        tenant_id=1,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256=m._pharmonline_public_api_catalog_fingerprint(source),
        source_transport="decodo",
        preflight_run_ref="38463791648",
        allow_identity_retirement=True,
        admission_proof_version=MANUAL,
        expected_plan_manifest_sha256=approved,
    )
    m._verify_pharmonline_public_api_identities(session, source, tenant_id=1)
    session.commit()


def test_cases_close_in_one_transaction_and_the_next_catalog_is_whole(pg_factory):  # noqa: F811
    with pg_factory() as session:
        cards = _seed(session)
        source = _source()
        _, partition, _, _ = _isolate(session, source)
        assert partition["isolated_count"] == 4
        approved = _manifest(session, source)

        _apply_serializable(session, source, approved)

        decisions = sorted(
            session.scalars(select(storage.PharmonlineIdentityConflictResolution.decision))
        )
        assert decisions == sorted(
            [resolution.RETIRED] * 3 + [resolution.SHADOW, resolution.CONFIRMED]
        )
        assert not session.scalar(
            select(storage.Product.id).where(storage.Product.identity_conflicted.is_(True))
        )
        trusted, partition, _, _ = _isolate(session, source)
        assert partition is None
        assert sum(len(r.products) for r in trusted) == len(source[0].products)
        assert cards["shadow"].external_id.startswith("pharmonline-retired-")


def test_failed_apply_leaves_no_decision_retirement_or_archive(pg_factory, monkeypatch):  # noqa: F811
    with pg_factory() as session:
        cards = _seed(session)
        source = _source()
        _isolate(session, source)
        approved = _manifest(session, source)
        original = resolution.apply_resolutions

        def fail_after_writes(*args, **kwargs):
            original(*args, **kwargs)
            session.execute(text("SELECT 1 / 0"))

        monkeypatch.setattr(resolution, "apply_resolutions", fail_after_writes)
        with pytest.raises(Exception):
            _apply_serializable(session, source, approved)
        session.rollback()
        for model in (
            storage.PharmonlineIdentityConflictResolution,
            storage.PharmonlinePublicAPIIdentityRetirement,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        session.expire_all()
        assert all(product.identity_conflicted for product in cards.values())
        assert not cards["shadow"].external_id.startswith("pharmonline-retired-")
