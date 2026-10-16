"""CRUD для TrackedProduct (watchlist) и Recipient.

Используется и из CLI, и из дашборда. Никакой бизнес-логики кроме персистентности.
"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from src.storage import (
    Category,
    Recipient,
    SavedView,
    TrackedCategory,
    TrackedProduct,
    TrackedProductLink,
)

SITES = ("pharmonline", "aptekonline", "aloe")
ALOE_BROAD_CATEGORY_SLUGS = {
    "dermanlar",
    "bad",
    "usaq-dunyasi",
    "uşaq-qidası",
    "kosmetika",
    "gigiyena",
}


def _aloe_category_sort_rank(slug_field) -> object:
    """Sort broad/non-category Aloe filters before precise category slugs."""
    return case(
        (slug_field.in_(ALOE_BROAD_CATEGORY_SLUGS), 0),
        (slug_field.like("%=%"), 0),
        else_=1,
    )


# === SAVED VIEWS ===


def list_saved_views(session: Session, scope: str | None = None) -> list[SavedView]:
    stmt = select(SavedView).order_by(SavedView.name)
    if scope:
        stmt = stmt.where(SavedView.scope == scope)
    return list(session.scalars(stmt).all())


def get_saved_view(session: Session, name: str) -> SavedView | None:
    return session.scalar(select(SavedView).where(SavedView.name == name))


def upsert_saved_view(
    session: Session, name: str, params: dict, *, scope: str = "comparison"
) -> SavedView:
    existing = get_saved_view(session, name)
    if existing:
        existing.params = params
        existing.scope = scope
        session.commit()
        return existing
    v = SavedView(name=name, scope=scope, params=params)
    session.add(v)
    session.commit()
    return v


def delete_saved_view(session: Session, name: str) -> bool:
    v = get_saved_view(session, name)
    if not v:
        return False
    session.delete(v)
    session.commit()
    return True


# === CATEGORIES ===


def list_categories(session: Session, active_only: bool = False) -> list[Category]:
    stmt = select(Category).order_by(Category.label_ru)
    if active_only:
        stmt = stmt.where(Category.is_active.is_(True))
    return list(session.scalars(stmt).all())


def get_category(session: Session, key: str) -> Category | None:
    return session.scalar(select(Category).where(Category.key == key))


def add_category(
    session: Session,
    key: str,
    label_ru: str,
    *,
    label_az: str | None = None,
    pharmonline_slug: str | None = None,
    aptekonline_slug: str | None = None,
    aloe_slug: str | None = None,
    is_active: bool = True,
) -> Category:
    key = key.strip().lower()
    existing = get_category(session, key)
    if existing:
        existing.label_ru = label_ru
        existing.label_az = label_az
        existing.pharmonline_slug = pharmonline_slug or None
        existing.aptekonline_slug = aptekonline_slug or None
        existing.aloe_slug = aloe_slug or None
        existing.is_active = is_active
        session.commit()
        return existing
    cat = Category(
        key=key,
        label_ru=label_ru,
        label_az=label_az,
        pharmonline_slug=pharmonline_slug or None,
        aptekonline_slug=aptekonline_slug or None,
        aloe_slug=aloe_slug or None,
        is_active=is_active,
    )
    session.add(cat)
    session.commit()
    return cat


def update_category(session: Session, cat_id: int, **fields) -> Category | None:
    cat = session.get(Category, cat_id)
    if not cat:
        return None
    for k, v in fields.items():
        if hasattr(cat, k):
            # пустые строки трактуем как null для slug-полей
            if k.endswith("_slug") and v == "":
                v = None
            setattr(cat, k, v)
    session.commit()
    return cat


def remove_category(session: Session, cat_id: int) -> bool:
    cat = session.get(Category, cat_id)
    if not cat:
        return False
    session.delete(cat)
    session.commit()
    return True


def toggle_category(session: Session, cat_id: int) -> Category | None:
    cat = session.get(Category, cat_id)
    if not cat:
        return None
    cat.is_active = not cat.is_active
    session.commit()
    return cat


def categories_for_site(
    session: Session, site: str, only_category_id: int | None = None
) -> list[str]:
    """Активные slug'и категорий для конкретного сайта.

    `only_category_id` — если задан, вернём slug только для этой категории
    (для запуска scrape'а одной категории через UI/CLI).
    """
    if site not in SITES:
        return []
    field_map = {
        "pharmonline": Category.pharmonline_slug,
        "aptekonline": Category.aptekonline_slug,
        "aloe": Category.aloe_slug,
    }
    field = field_map[site]
    stmt = select(field).where(Category.is_active.is_(True), field.is_not(None))
    if only_category_id is not None:
        stmt = stmt.where(Category.id == only_category_id)
    if site == "aloe":
        # Aloe has broad buckets (for example `dermanlar`) plus more precise
        # category_slug filters. Scrape broad/non-category filters first so
        # precise category runs can persist their product.category last.
        stmt = stmt.order_by(
            _aloe_category_sort_rank(field),
            Category.id,
        )
    else:
        stmt = stmt.order_by(Category.id)
    rows = session.scalars(stmt).all()
    return [r for r in rows if r]


def seed_categories_from_yaml(session: Session, yaml_path) -> int:
    """Перенести категории из config/categories.yaml в БД на первом запуске.

    Идемпотентно — если категория с таким key уже есть, она обновится.
    Возвращает количество обработанных записей.
    """
    import yaml as _yaml
    from pathlib import Path

    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        return 0
    with yaml_path.open(encoding="utf-8") as f:
        data = _yaml.safe_load(f) or {}
    cats = data.get("categories", {}) or {}
    n = 0
    for key, body in cats.items():
        if not isinstance(body, dict):
            continue
        add_category(
            session,
            key=key,
            label_ru=body.get("label_ru") or key,
            label_az=body.get("label_az"),
            pharmonline_slug=body.get("pharmonline"),
            aptekonline_slug=body.get("aptekonline"),
            aloe_slug=body.get("aloe"),
        )
        n += 1
    return n


# === RECIPIENTS ===


def add_recipient(session: Session, email: str, name: str | None = None) -> Recipient:
    email = email.strip().lower()
    existing = session.scalar(select(Recipient).where(Recipient.email == email))
    if existing:
        existing.name = name or existing.name
        existing.is_active = True
        session.commit()
        return existing
    r = Recipient(email=email, name=name, is_active=True)
    session.add(r)
    session.commit()
    return r


def list_recipients(session: Session, active_only: bool = False) -> list[Recipient]:
    stmt = select(Recipient).order_by(Recipient.created_at.desc())
    if active_only:
        stmt = stmt.where(Recipient.is_active.is_(True))
    return list(session.scalars(stmt).all())


def remove_recipient(session: Session, email: str) -> bool:
    email = email.strip().lower()
    r = session.scalar(select(Recipient).where(Recipient.email == email))
    if not r:
        return False
    session.delete(r)
    session.commit()
    return True


def toggle_recipient(session: Session, email: str) -> Recipient | None:
    email = email.strip().lower()
    r = session.scalar(select(Recipient).where(Recipient.email == email))
    if not r:
        return None
    r.is_active = not r.is_active
    session.commit()
    return r


def update_recipient(
    session: Session,
    email: str,
    *,
    new_email: str | None = None,
    name: str | None = None,
    telegram_chat_id: str | None = None,
) -> Recipient | None:
    """Изменить имя, email или telegram_chat_id существующей записи."""
    r = session.scalar(select(Recipient).where(Recipient.email == email.strip().lower()))
    if not r:
        return None
    if new_email:
        r.email = new_email.strip().lower()
    if name is not None:
        r.name = name or None
    if telegram_chat_id is not None:
        r.telegram_chat_id = telegram_chat_id.strip() or None
    session.commit()
    return r


def active_recipient_emails(session: Session) -> list[str]:
    return [r.email for r in list_recipients(session, active_only=True)]


# === TRACKED PRODUCTS (WATCHLIST) ===


def add_tracked_product(
    session: Session,
    canonical_name: str,
    *,
    brand: str | None = None,
    dosage: str | None = None,
    pack_size: str | None = None,
    search_query: str | None = None,
    notes: str | None = None,
    pharmonline_url: str | None = None,
    aptekonline_url: str | None = None,
    aloe_url: str | None = None,
    tenant_id: int = 1,
) -> TrackedProduct:
    tp = TrackedProduct(
        tenant_id=tenant_id,
        canonical_name=canonical_name.strip(),
        brand=brand.strip() if brand else None,
        dosage=dosage.strip() if dosage else None,
        pack_size=pack_size.strip() if pack_size else None,
        search_query=search_query.strip() if search_query else None,
        notes=notes,
        is_active=True,
    )
    session.add(tp)
    session.flush()

    urls = {
        "pharmonline": pharmonline_url,
        "aptekonline": aptekonline_url,
        "aloe": aloe_url,
    }
    for site, url in urls.items():
        if url:
            session.add(
                TrackedProductLink(
                    tracked_product_id=tp.id,
                    site=site,
                    url=url.strip(),
                    status="confirmed",
                )
            )
        else:
            session.add(TrackedProductLink(tracked_product_id=tp.id, site=site, status="pending"))
    session.commit()
    return tp


def list_tracked(
    session: Session,
    active_only: bool = True,
    *,
    tenant_id: int | None = None,
) -> list[TrackedProduct]:
    stmt = select(TrackedProduct).order_by(TrackedProduct.canonical_name)
    if tenant_id is not None:
        stmt = stmt.where(TrackedProduct.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(TrackedProduct.is_active.is_(True))
    return list(session.scalars(stmt).all())


def remove_tracked(session: Session, tracked_id: int) -> bool:
    tp = session.get(TrackedProduct, tracked_id)
    if not tp:
        return False
    session.delete(tp)
    session.commit()
    return True


def update_tracked(session: Session, tracked_id: int, **fields) -> TrackedProduct | None:
    tp = session.get(TrackedProduct, tracked_id)
    if not tp:
        return None
    for k, v in fields.items():
        if hasattr(tp, k):
            setattr(tp, k, v)
    session.commit()
    return tp


def set_link_url(
    session: Session, tracked_id: int, site: str, url: str | None, status: str = "confirmed"
) -> TrackedProductLink | None:
    if site not in SITES:
        raise ValueError(f"Unknown site: {site}")
    link = session.scalar(
        select(TrackedProductLink).where(
            TrackedProductLink.tracked_product_id == tracked_id,
            TrackedProductLink.site == site,
        )
    )
    if not link:
        link = TrackedProductLink(tracked_product_id=tracked_id, site=site)
        session.add(link)
    link.url = url
    link.status = status
    session.commit()
    return link


# === TRACKED CATEGORIES (WATCHLIST) ===


def add_tracked_category(
    session: Session,
    category_id: int,
    *,
    tenant_id: int = 1,
    notes: str | None = None,
) -> TrackedCategory:
    category = session.get(Category, category_id)
    if not category:
        raise ValueError(f"Category not found: {category_id}")
    existing = session.scalar(
        select(TrackedCategory).where(
            TrackedCategory.tenant_id == tenant_id,
            TrackedCategory.category_id == category_id,
        )
    )
    if existing:
        existing.notes = notes if notes is not None else existing.notes
        existing.is_active = True
        session.commit()
        return existing
    tc = TrackedCategory(
        tenant_id=tenant_id,
        category_id=category_id,
        notes=notes.strip() if notes else None,
        is_active=True,
    )
    session.add(tc)
    session.commit()
    return tc


def list_tracked_categories(
    session: Session, *, tenant_id: int = 1, active_only: bool = True
) -> list[TrackedCategory]:
    stmt = (
        select(TrackedCategory)
        .join(TrackedCategory.category)
        .where(TrackedCategory.tenant_id == tenant_id)
        .order_by(Category.label_ru)
    )
    if active_only:
        stmt = stmt.where(TrackedCategory.is_active.is_(True))
    return list(session.scalars(stmt).all())


def remove_tracked_category(session: Session, tracked_category_id: int, *, tenant_id: int = 1) -> bool:
    tc = session.scalar(
        select(TrackedCategory).where(
            TrackedCategory.id == tracked_category_id,
            TrackedCategory.tenant_id == tenant_id,
        )
    )
    if not tc:
        return False
    session.delete(tc)
    session.commit()
    return True


def import_from_csv(session: Session, csv_path: Path) -> int:
    """CSV columns (header row required):
        canonical_name, brand, dosage, pack_size, search_query,
        pharmonline_url, aptekonline_url, aloe_url, notes

    Все поля кроме canonical_name опциональны.
    """
    csv_path = Path(csv_path)
    count = 0
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("canonical_name") or "").strip()
            if not name:
                continue
            add_tracked_product(
                session,
                canonical_name=name,
                brand=row.get("brand"),
                dosage=row.get("dosage"),
                pack_size=row.get("pack_size"),
                search_query=row.get("search_query"),
                notes=row.get("notes"),
                pharmonline_url=row.get("pharmonline_url"),
                aptekonline_url=row.get("aptekonline_url"),
                aloe_url=row.get("aloe_url"),
            )
            count += 1
    return count


def export_to_csv(session: Session, csv_path: Path) -> int:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list_tracked(session, active_only=False)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "canonical_name",
                "brand",
                "dosage",
                "pack_size",
                "search_query",
                "pharmonline_url",
                "aptekonline_url",
                "aloe_url",
                "is_active",
                "notes",
            ]
        )
        for tp in rows:
            urls = {link.site: link.url for link in tp.links}
            writer.writerow(
                [
                    tp.canonical_name,
                    tp.brand or "",
                    tp.dosage or "",
                    tp.pack_size or "",
                    tp.search_query or "",
                    urls.get("pharmonline", "") or "",
                    urls.get("aptekonline", "") or "",
                    urls.get("aloe", "") or "",
                    "1" if tp.is_active else "0",
                    tp.notes or "",
                ]
            )
    return len(rows)
