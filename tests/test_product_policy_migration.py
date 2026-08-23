from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_identity_offer_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'identity-offer.sqlite'}"
    _alembic(db_url, "upgrade", "0016_supplier_price_unique")
    _alembic(db_url, "upgrade", "0017_product_identity_offer")

    inspector = inspect(create_engine(db_url))
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    run_columns = {column["name"] for column in inspector.get_columns("runs")}
    roi_cache_columns = {column["name"] for column in inspector.get_columns("roi_actions_cache")}
    assert {
        "catalog_scope",
        "full_catalog_sites",
        "catalog_verified",
        "catalog_verification_reason",
    } <= run_columns
    assert "policy_fingerprint" in roi_cache_columns
    assert {
        "manufacturer_country_code",
        "country_resolution_status",
        "offer_availability_status",
        "availability_run_id",
    } <= product_columns
    assert {
        "offer_observations",
        "match_policy_audits",
        "aloe_country_mappings",
    } <= set(inspector.get_table_names())

    _alembic(db_url, "downgrade", "0016_supplier_price_unique")
    inspector = inspect(create_engine(db_url))
    assert "manufacturer_country_code" not in {
        column["name"] for column in inspector.get_columns("products")
    }
    assert "catalog_verified" not in {column["name"] for column in inspector.get_columns("runs")}
    assert "policy_fingerprint" not in {
        column["name"] for column in inspector.get_columns("roi_actions_cache")
    }
    assert "offer_observations" not in inspector.get_table_names()
    assert "match_policy_audits" not in inspector.get_table_names()
    assert "aloe_country_mappings" not in inspector.get_table_names()


def test_pharmonline_public_api_reconciliation_migrations_are_reversible(
    tmp_path: Path,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'pharmonline-public-api-reconciliation.sqlite'}"
    _alembic(db_url, "upgrade", "0020_public_api_admissions")

    inspector = inspect(create_engine(db_url))
    reconciliation_columns = {
        column["name"]
        for column in inspector.get_columns("pharmonline_public_api_identity_reconciliations")
    }
    baseline_columns = {
        column["name"]
        for column in inspector.get_columns("pharmonline_public_api_catalog_baselines")
    }
    admission_columns = {
        column["name"]
        for column in inspector.get_columns("pharmonline_public_api_identity_admissions")
    }
    assert {
        "tenant_id",
        "product_id",
        "legacy_external_id",
        "public_api_external_id",
        "legacy_canonical_url",
        "public_api_canonical_url",
        "source_manifest_sha256",
        "catalog_fingerprint_sha256",
        "preflight_run_ref",
    } <= reconciliation_columns
    assert {
        "catalog_item_count",
        "minimum_catalog_item_count",
        "verified_identity_count",
        "trusted_ddp_item_count",
        "retired_ddp_item_count",
        "reconciled_item_count",
    } <= baseline_columns
    assert {
        "tenant_id",
        "product_id",
        "admission_kind",
        "public_api_external_id",
        "public_api_canonical_url",
        "source_manifest_sha256",
        "catalog_fingerprint_sha256",
        "preflight_run_ref",
    } <= admission_columns
    unique_constraints = inspector.get_unique_constraints(
        "pharmonline_public_api_identity_reconciliations"
    )
    assert {
        "uq_pharmonline_public_api_reconciliation_product",
        "uq_pharmonline_public_api_reconciliation_legacy_id",
        "uq_pharmonline_public_api_reconciliation_public_id",
    } <= {constraint["name"] for constraint in unique_constraints}
    admission_unique_constraints = inspector.get_unique_constraints(
        "pharmonline_public_api_identity_admissions"
    )
    assert {
        "uq_pharmonline_public_api_admission_product",
        "uq_pharmonline_public_api_admission_public_id",
    } <= {constraint["name"] for constraint in admission_unique_constraints}

    _alembic(db_url, "downgrade", "0018_product_manual_category")
    inspector = inspect(create_engine(db_url))
    assert "pharmonline_public_api_identity_reconciliations" not in inspector.get_table_names()
    assert "pharmonline_public_api_identity_admissions" not in inspector.get_table_names()
    assert "pharmonline_public_api_catalog_baselines" not in inspector.get_table_names()
