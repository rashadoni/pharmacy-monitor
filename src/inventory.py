"""Inventory & supplier prices: импорт из CSV + margin-расчёты.

CSV-форматы:

**stock.csv** (минимум):
    sku,qty,name
    PHM-12345,15,Paracetamol 500mg

**purchase_prices.csv**:
    sku,supplier_name,purchase_price,currency,name
    PHM-12345,Pharma Wholesale,2.30,AZN,Paracetamol 500mg

`sku` — уникальный идентификатор. Привязка к Product:
    - сначала пытаемся найти Product по `external_id == sku` на любом сайте
    - fallback: ищем по `name` (fuzzy-match)
    - если не нашли — запись хранится без product_id (висит "сама по себе")
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from src._time import utcnow
from pathlib import Path

import structlog
from rapidfuzz import fuzz, process
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.storage import (
    Match,
    PriceSnapshot,
    Product,
    Run,
    StockLevel,
    SupplierPrice,
)

log = structlog.get_logger()


@dataclass
class ImportResult:
    total: int
    matched_to_product: int
    unmatched: int


def _find_product_by_sku_or_name(
    session: Session, sku: str | None, name: str | None
) -> Product | None:
    """Привязка CSV-строки к Product:
    1) external_id == sku (точно)
    2) fuzzy-match по name среди клиентских (pharmonline) товаров
    """
    if sku:
        p = session.scalar(select(Product).where(Product.external_id == sku.strip()))
        if p:
            return p
    if name:
        client_products = session.scalars(
            select(Product).where(Product.site == "pharmonline")
        ).all()
        if not client_products:
            return None
        # Поиск по нормализованному имени
        choices = {p.id: (p.name or "") for p in client_products}
        match = process.extractOne(
            name, choices.values(), scorer=fuzz.token_set_ratio, score_cutoff=85
        )
        if match:
            matched_name, score, _idx = match
            for p in client_products:
                if p.name == matched_name:
                    return p
    return None


def import_stock_from_csv(
    session: Session, csv_path: Path | str, *, source: str = "manual_csv"
) -> ImportResult:
    """Импорт остатков. Переписывает все StockLevel записи с указанным source."""
    csv_path = Path(csv_path)
    # Очищаем старые записи этого source
    session.execute(delete(StockLevel).where(StockLevel.source == source))

    total = matched = unmatched = 0
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            name = (row.get("name") or "").strip()
            qty_raw = (row.get("qty") or "0").strip().replace(",", ".")
            try:
                qty = float(qty_raw)
            except ValueError:
                qty = 0.0
            if not sku and not name:
                continue
            total += 1
            product = _find_product_by_sku_or_name(session, sku, name)
            if product:
                matched += 1
            else:
                unmatched += 1
            session.add(StockLevel(
                product_id=product.id if product else None,
                canonical_id=product.canonical_id if product else None,
                sku=sku or None,
                name=name or (product.name if product else None),
                qty=qty,
                is_in_stock=qty > 0,
                source=source,
                updated_at=utcnow(),
            ))
    session.commit()
    log.info(
        "stock_import_done",
        source=source, total=total, matched=matched, unmatched=unmatched,
    )
    return ImportResult(total=total, matched_to_product=matched, unmatched=unmatched)


def import_supplier_prices_from_csv(
    session: Session, csv_path: Path | str, *, source: str = "manual_csv"
) -> ImportResult:
    """Импорт закупочных цен."""
    csv_path = Path(csv_path)
    session.execute(delete(SupplierPrice).where(SupplierPrice.source == source))

    total = matched = unmatched = 0
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            name = (row.get("name") or "").strip()
            supplier = (row.get("supplier_name") or "Unknown").strip()
            currency = (row.get("currency") or "AZN").strip()
            price_raw = (row.get("purchase_price") or "0").strip().replace(",", ".")
            try:
                price = float(price_raw)
            except ValueError:
                continue
            if price <= 0 or (not sku and not name):
                continue
            total += 1
            product = _find_product_by_sku_or_name(session, sku, name)
            if product:
                matched += 1
            else:
                unmatched += 1
            session.add(SupplierPrice(
                product_id=product.id if product else None,
                canonical_id=product.canonical_id if product else None,
                sku=sku or None,
                name=name or (product.name if product else None),
                supplier_name=supplier,
                purchase_price=price,
                currency=currency,
                source=source,
                updated_at=utcnow(),
            ))
    session.commit()
    log.info(
        "supplier_import_done",
        source=source, total=total, matched=matched, unmatched=unmatched,
    )
    return ImportResult(total=total, matched_to_product=matched, unmatched=unmatched)


# === MARGIN ANALYSIS ===


@dataclass
class MarginRow:
    product_id: int | None
    canonical_id: int | None
    name: str
    sale_price: float
    purchase_price: float
    margin_azn: float
    margin_pct: float
    in_stock: bool


def margin_report(session: Session, *, run_id: int | None = None) -> list[MarginRow]:
    """Маржа по каждому товару клиента: sale - purchase.

    Берём минимум purchase_price среди поставщиков (лучшая margin).
    """
    if run_id is None:
        run_id = session.scalar(
            select(Run.id).where(Run.status == "ok").order_by(Run.id.desc()).limit(1)
        )
        if run_id is None:
            return []

    rows: list[MarginRow] = []
    # Берём все клиентские Products со снимком цены
    products = session.scalars(
        select(Product).where(Product.site == "pharmonline")
    ).all()

    for p in products:
        snap = session.scalar(
            select(PriceSnapshot).where(
                PriceSnapshot.product_id == p.id, PriceSnapshot.run_id == run_id
            )
        )
        if not snap:
            continue
        sale_price = snap.discount_price or snap.price
        if sale_price is None or sale_price <= 0:
            continue

        # Минимальная закупочная цена для этого продукта
        purchase = session.scalar(
            select(SupplierPrice.purchase_price)
            .where(SupplierPrice.product_id == p.id)
            .order_by(SupplierPrice.purchase_price.asc())
            .limit(1)
        )
        if purchase is None or purchase <= 0:
            continue

        # Stock
        stock = session.scalar(
            select(StockLevel.is_in_stock).where(StockLevel.product_id == p.id).limit(1)
        )
        in_stock = bool(stock) if stock is not None else True

        margin_azn = sale_price - purchase
        margin_pct = (margin_azn / sale_price * 100) if sale_price > 0 else 0.0

        rows.append(MarginRow(
            product_id=p.id, canonical_id=p.canonical_id,
            name=p.name, sale_price=sale_price, purchase_price=purchase,
            margin_azn=round(margin_azn, 2), margin_pct=round(margin_pct, 1),
            in_stock=in_stock,
        ))
    rows.sort(key=lambda x: -x.margin_pct)
    return rows


def get_stock_for_product(session: Session, product_id: int) -> StockLevel | None:
    return session.scalar(
        select(StockLevel).where(StockLevel.product_id == product_id).limit(1)
    )


def get_min_purchase_price(session: Session, product_id: int) -> float | None:
    return session.scalar(
        select(SupplierPrice.purchase_price)
        .where(SupplierPrice.product_id == product_id)
        .order_by(SupplierPrice.purchase_price.asc())
        .limit(1)
    )
