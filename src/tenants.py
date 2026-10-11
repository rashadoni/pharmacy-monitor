"""Multi-tenant: создание тенантов, пользователей, magic-link auth.

Это **скелет** Q2 SaaS-trek'а. На стадии M3 / Q1 не требуется в продакшене —
один тенант (default) обслуживает pharmonline.

Когда Q2 решение принято в пользу SaaS:
1. Все queries в analyzer/matcher/etc обернуть в WHERE tenant_id = current
2. Streamlit добавляет tenant-switcher в sidebar
3. Magic-link логин: send_magic_link() → email → проверка токена
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from src._time import utcnow

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.email_address import normalize_address
from src.storage import Tenant, TenantUser

log = structlog.get_logger()

DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_ID = 1
# Под этой записью живёт первый администратор, пока ему не задали настоящий
# адрес: по ней входят паролем (`ADMIN_LOGIN`), писем на неё нет — адресом она
# не является, и при отправке её пропускают, как любую запись не по правилу.
BOOTSTRAP_ADMIN_EMAIL = "admin@local"


def current_tenant_id() -> int:
    """Текущий tenant_id для запросов.

    Сейчас всегда 1 (default). В будущем возьмём из:
    1. Streamlit session_state (после авторизации)
    2. ENV var (для CLI)
    3. Magic-link cookie (для multi-user dashboard)
    """
    import os

    val = os.environ.get("PHARMACY_CURRENT_TENANT_ID")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    # Streamlit-aware (если работаем внутри дашборда):
    try:
        import streamlit as st

        return int(st.session_state.get("current_tenant_id", DEFAULT_TENANT_ID))
    except Exception:
        return DEFAULT_TENANT_ID


def get_or_create_default(session: Session) -> Tenant:
    """Default тенант для backward-compat — всё что было до multi-tenant принадлежит ему."""
    t = session.scalar(select(Tenant).where(Tenant.slug == DEFAULT_TENANT_SLUG))
    if t:
        return t
    t = Tenant(
        slug=DEFAULT_TENANT_SLUG,
        name="Default (pharmonline)",
        client_site="pharmonline",
        plan="basic",
    )
    session.add(t)
    session.commit()
    log.info("tenant_default_created", tenant_id=t.id)
    return t


def create_tenant(
    session: Session,
    slug: str,
    name: str,
    client_site: str | None = None,
    plan: str = "trial",
) -> Tenant:
    slug = slug.strip().lower()
    existing = session.scalar(select(Tenant).where(Tenant.slug == slug))
    if existing:
        raise ValueError(f"Tenant '{slug}' already exists")
    t = Tenant(slug=slug, name=name, client_site=client_site, plan=plan)
    session.add(t)
    session.commit()
    return t


def list_tenants(session: Session, active_only: bool = True) -> list[Tenant]:
    stmt = select(Tenant).order_by(Tenant.created_at)
    if active_only:
        stmt = stmt.where(Tenant.is_active.is_(True))
    return list(session.scalars(stmt).all())


def get_tenant(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug.strip().lower()))


# === USERS ===


def add_user(
    session: Session,
    tenant_id: int,
    email: str,
    *,
    name: str | None = None,
    role: str = "admin",
) -> TenantUser:
    """Запись, которая не адрес, — `InvalidEmailAddress`; кроме заглушки первого
    администратора (`BOOTSTRAP_ADMIN_EMAIL`)."""
    email = email.strip().lower()
    if email != BOOTSTRAP_ADMIN_EMAIL:
        email = normalize_address(email)
    existing = session.scalar(
        select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.email == email)
    )
    if existing:
        if name:
            existing.name = name
        existing.is_active = True
        session.commit()
        return existing
    u = TenantUser(tenant_id=tenant_id, email=email, name=name, role=role)
    session.add(u)
    session.commit()
    return u


def list_users(session: Session, tenant_id: int | None = None) -> list[TenantUser]:
    stmt = select(TenantUser)
    if tenant_id:
        stmt = stmt.where(TenantUser.tenant_id == tenant_id)
    return list(session.scalars(stmt).all())


def issue_magic_token(session: Session, email: str, ttl_minutes: int = 30) -> str | None:
    """Сгенерировать magic-token. Вернуть токен (для отправки в email).

    Если такого юзера нет — None. Не создаёт user'а автоматически (security).
    """
    u = session.scalar(
        select(TenantUser).where(
            TenantUser.email == email.strip().lower(), TenantUser.is_active.is_(True)
        )
    )
    if not u:
        return None
    token = secrets.token_urlsafe(32)
    u.magic_token = token
    u.magic_token_expires_at = utcnow() + timedelta(minutes=ttl_minutes)
    session.commit()
    return token


def verify_magic_token(session: Session, token: str) -> TenantUser | None:
    """Проверить токен. Если валиден — отметить last_login_at, обнулить токен, вернуть user."""
    if not token:
        return None
    u = session.scalar(
        select(TenantUser).where(TenantUser.magic_token == token, TenantUser.is_active.is_(True))
    )
    if not u:
        return None
    if u.magic_token_expires_at and u.magic_token_expires_at < utcnow():
        return None
    u.magic_token = None
    u.magic_token_expires_at = None
    u.last_login_at = utcnow()
    session.commit()
    return u
