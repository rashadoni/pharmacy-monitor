"""Tests для Phase 2.3 — matcher v2 barcode-priority pass.

Покрываем:
- Two products on different sites with same barcode → matched, confidence=1.0
- Same barcode on SAME site → not duplicated in cluster (skip)
- Empty/zero/short barcodes → ignored
- Barcode match overrides fuzzy passes (skips other heuristics)
- Manual rejection still respected (MatchRejection blocks barcode match)
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import matcher, storage  # noqa: E402
from src.match_actions import add_rejection  # noqa: E402


def _make_product(
    db,
    site: str,
    name: str,
    *,
    barcode: str | None = None,
    ext: str | None = None,
    brand: str | None = None,
) -> storage.Product:
    p = storage.Product(
        tenant_id=1,
        site=site,
        external_id=ext or f"{site}-{name.lower()}",
        url=f"https://{site}.example/p/{name}",
        name=name,
        name_normalized=name.lower(),
        brand=brand,
        barcode=barcode,
    )
    db.add(p)
    db.flush()
    return p


def test_barcode_match_two_sites_same_barcode(db_session):
    """Same barcode + different sites = match cluster, confidence 1.0."""
    a = _make_product(db_session, "pharmonline", "AspirinAlpha", barcode="4607017950021")
    b = _make_product(db_session, "aptekonline", "AspirinBeta", barcode="4607017950021")
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)

    assert a.canonical_id is not None
    assert a.canonical_id == b.canonical_id
    match = db_session.get(storage.Match, a.canonical_id)
    assert match.confidence == 1.0


def test_barcode_match_ignores_empty_and_zero(db_session):
    """Barcode='0', '', 'null' — noise, must not cluster."""
    _make_product(db_session, "pharmonline", "X", barcode="0", ext="x1")
    _make_product(db_session, "aptekonline", "Y", barcode="0", ext="y1")
    _make_product(db_session, "aloe", "Z", barcode="", ext="z1")
    _make_product(db_session, "pharmonline", "Q", barcode=None, ext="q1")
    db_session.commit()

    matcher.match_products(db_session)
    products = db_session.scalars(__import__("sqlalchemy").select(storage.Product)).all()
    assert all(p.canonical_id is None for p in products)


def test_barcode_match_requires_minimum_8_digits(db_session):
    """Barcode shorter than 8 digits — discarded as noise."""
    _make_product(db_session, "pharmonline", "X", barcode="123")
    _make_product(db_session, "aptekonline", "Y", barcode="123")
    db_session.commit()

    matcher.match_products(db_session)
    products = db_session.scalars(__import__("sqlalchemy").select(storage.Product)).all()
    assert all(p.canonical_id is None for p in products)


def test_barcode_match_one_site_only_not_matched(db_session):
    """Two products with same barcode but same site — no cluster."""
    _make_product(db_session, "pharmonline", "X", barcode="4607017950021", ext="x1")
    _make_product(db_session, "pharmonline", "Y", barcode="4607017950021", ext="x2")
    db_session.commit()

    matcher.match_products(db_session)
    products = db_session.scalars(__import__("sqlalchemy").select(storage.Product)).all()
    assert all(p.canonical_id is None for p in products)


def test_barcode_match_respects_manual_rejection(db_session):
    """If user previously rejected this pair → barcode match doesn't override."""
    a = _make_product(db_session, "pharmonline", "X", barcode="4607017950021")
    b = _make_product(db_session, "aptekonline", "Y", barcode="4607017950021")
    db_session.commit()
    add_rejection(db_session, a.id, b.id, reason="user said no")
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    # At least ONE of them is unlinked (the rejection blocked the cluster).
    # Both could remain unlinked, or one could still get a singleton.
    assert a.canonical_id != b.canonical_id or a.canonical_id is None


def test_barcode_match_with_three_sites(db_session):
    """Cross-site cluster of 3 — one product per site."""
    a = _make_product(db_session, "pharmonline", "P", barcode="4607017950021")
    b = _make_product(db_session, "aptekonline", "A", barcode="4607017950021")
    c = _make_product(db_session, "aloe", "L", barcode="4607017950021")
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    db_session.refresh(c)
    assert a.canonical_id is not None
    assert a.canonical_id == b.canonical_id == c.canonical_id


def test_barcode_match_bypasses_name_conflict(db_session):
    """Even if names look like different products (different modifiers,
    series numbers), same barcode forces a match. This is the WHOLE POINT
    of Phase 2 — barcode is ground truth, name fuzzy is approximation.

    Example: "Lopril" vs "Lopril H" — fuzzy matcher refuses because "H" is
    a pharma modifier. But if both ship with same barcode (manufacturer made
    same package, just labelled differently per market) → still match.
    """
    a = _make_product(
        db_session, "pharmonline", "Lopril 10mg", barcode="5712345678901", brand="Lopril"
    )
    b = _make_product(
        db_session, "aptekonline", "Lopril H 10mg", barcode="5712345678901", brand="Lopril"
    )
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.canonical_id is not None
    assert a.canonical_id == b.canonical_id
