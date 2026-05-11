"""Multi-tenant isolation tests.

Goal: prove that tenant-A data is invisible to tenant-B's queries via the
tenancy.scoped() helper, even when both tenants have rows in the same table.
"""
from __future__ import annotations

import pytest

from src import storage, tenancy
from src._time import utcnow


@pytest.fixture
def two_tenants(db_session):
    """Create two tenants with one product each. Returns (tenant_a, tenant_b)."""
    s = db_session
    a = storage.Tenant(slug="tenant-a", name="Tenant A", is_active=True, created_at=utcnow())
    b = storage.Tenant(slug="tenant-b", name="Tenant B", is_active=True, created_at=utcnow())
    s.add_all([a, b])
    s.flush()

    pa = storage.Product(
        site="pharmonline", external_id="A-001", url="x", name="Product A",
        name_normalized="product a", brand="BrandA",
        first_seen_at=utcnow(), last_seen_at=utcnow(),
        tenant_id=a.id,
    )
    pb = storage.Product(
        site="pharmonline", external_id="B-001", url="y", name="Product B",
        name_normalized="product b", brand="BrandB",
        first_seen_at=utcnow(), last_seen_at=utcnow(),
        tenant_id=b.id,
    )
    s.add_all([pa, pb])
    s.commit()
    return a, b


def test_scoped_filter_returns_only_current_tenant(db_session, two_tenants):
    a, b = two_tenants

    with tenancy.set_current_tenant(a.id):
        rows = db_session.scalars(tenancy.scoped(db_session, storage.Product)).all()
        names = {r.name for r in rows}
        assert "Product A" in names
        assert "Product B" not in names

    with tenancy.set_current_tenant(b.id):
        rows = db_session.scalars(tenancy.scoped(db_session, storage.Product)).all()
        names = {r.name for r in rows}
        assert "Product B" in names
        assert "Product A" not in names


def test_default_tenant_when_unset(db_session):
    """If no context is set, falls back to DEFAULT_TENANT_ID = 1."""
    s = db_session
    p = storage.Product(
        site="pharmonline", external_id="DEFAULT-001", url="x", name="Default Product",
        name_normalized="default product",
        first_seen_at=utcnow(), last_seen_at=utcnow(),
        # tenant_id default = 1
    )
    s.add(p)
    s.commit()

    rows = s.scalars(tenancy.scoped(s, storage.Product)).all()
    names = {r.name for r in rows}
    assert "Default Product" in names


def test_assert_same_tenant_passes(db_session, two_tenants):
    a, b = two_tenants
    pa = db_session.scalar(
        tenancy.scoped(db_session, storage.Product, tenant_id=a.id)
        .where(storage.Product.tenant_id == a.id)
    )
    # Should not raise
    tenancy.assert_same_tenant(pa, tenant_id=a.id)


def test_assert_same_tenant_raises_on_mismatch(db_session, two_tenants):
    a, b = two_tenants
    pa = db_session.scalar(
        tenancy.scoped(db_session, storage.Product, tenant_id=a.id)
    )
    assert pa is not None
    with pytest.raises(ValueError, match="Tenant mismatch"):
        tenancy.assert_same_tenant(pa, tenant_id=b.id)


def test_context_var_isolation_in_concurrent_contexts(db_session, two_tenants):
    """Nested set_current_tenant() should restore previous value on exit."""
    a, b = two_tenants

    with tenancy.set_current_tenant(a.id):
        assert tenancy.current_tenant_id() == a.id

        with tenancy.set_current_tenant(b.id):
            assert tenancy.current_tenant_id() == b.id

        # After inner context exits, restored
        assert tenancy.current_tenant_id() == a.id

    # After outer context exits, back to default
    assert tenancy.current_tenant_id() == tenancy.DEFAULT_TENANT_ID


def test_models_without_tenant_id_field_dont_filter(db_session):
    """Model without tenant_id attribute → scoped() returns unfiltered SELECT."""
    # PriceSnapshot has no tenant_id (relies on Run.tenant_id)
    stmt = tenancy.scoped(db_session, storage.PriceSnapshot)
    # Should compile without error and be a generic SELECT
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id" not in compiled.lower()


def test_alert_event_tenant_isolation(db_session, two_tenants):
    a, b = two_tenants
    s = db_session
    s.add(storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-a", severity="warning",
        title="A alert", tenant_id=a.id,
    ))
    s.add(storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-b", severity="warning",
        title="B alert", tenant_id=b.id,
    ))
    s.commit()

    with tenancy.set_current_tenant(a.id):
        rows = s.scalars(tenancy.scoped(s, storage.AlertEvent)).all()
        titles = {r.title for r in rows}
        assert "A alert" in titles
        assert "B alert" not in titles
