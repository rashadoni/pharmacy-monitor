"""Multi-tenant helpers — context-var current tenant, scoped query helpers.

Usage:
    from src.tenancy import set_current_tenant, current_tenant_id, scoped

    # In FastAPI middleware after auth:
    with set_current_tenant(user.tenant_id):
        # all storage queries inside this block can call current_tenant_id()
        ...

    # Or use scoped helper directly:
    products = scoped(session, Product).all()  # auto-filters by tenant_id
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Type, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

T = TypeVar("T")

# Default fallback for single-tenant pilot / scripts that don't set context
DEFAULT_TENANT_ID = 1

_current_tenant: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_tenant", default=DEFAULT_TENANT_ID
)


def current_tenant_id() -> int:
    """Return current tenant id (from context var). Defaults to 1 for backwards compat."""
    return _current_tenant.get()


@contextmanager
def set_current_tenant(tenant_id: int) -> Iterator[None]:
    """Context manager: set tenant_id for the duration of a block.

    Usage in FastAPI middleware:
        @app.middleware("http")
        async def tenant_middleware(request, call_next):
            user = await get_user_from_jwt(request)
            with set_current_tenant(user.tenant_id):
                return await call_next(request)
    """
    token = _current_tenant.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)


def scoped(session: Session, model: Type[T], tenant_id: int | None = None):
    """Build a SELECT statement that auto-filters by tenant_id.

    Falls back to current_tenant_id() if not explicitly passed.
    Returns a SQLAlchemy `Select` you can chain with .where(), .order_by(), etc.

        rows = session.scalars(scoped(session, Product).limit(10)).all()
    """
    tid = tenant_id if tenant_id is not None else current_tenant_id()
    stmt = select(model)
    if hasattr(model, "tenant_id"):
        stmt = stmt.where(model.tenant_id == tid)
    return stmt


def assert_same_tenant(*objects, tenant_id: int | None = None) -> None:
    """Raise ValueError if any object's tenant_id != current/given tenant.

    Use as a guard in functions that take user-supplied IDs to prevent
    cross-tenant manipulation:

        def reject_match(session, match_id, user):
            match = session.get(Match, match_id)
            assert_same_tenant(match, tenant_id=user.tenant_id)
    """
    expected = tenant_id if tenant_id is not None else current_tenant_id()
    for o in objects:
        if o is None:
            continue
        actual = getattr(o, "tenant_id", None)
        if actual is not None and actual != expected:
            raise ValueError(
                f"Tenant mismatch: object tenant_id={actual} != expected {expected}"
            )
