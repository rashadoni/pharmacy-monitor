"""Тесты inventory: импорт CSV + margin + stock-aware ROI."""

from src._time import utcnow

from src import inventory, roi
from src.storage import (
    Match,
    PriceSnapshot,
    Product,
    Run,
    StockLevel,
    SupplierPrice,
)


def _add_product(s, site, name, ext_id, canonical_id=None):
    p = Product(
        site=site,
        external_id=ext_id,
        url=f"http://x/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _add_run(s):
    now = utcnow()
    r = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    s.add(r)
    s.flush()
    return r


def _add_snap(s, run, p, price):
    s.add(PriceSnapshot(run_id=run.id, product_id=p.id, price=price))
    s.flush()


def test_import_stock_matches_by_sku(db_session, tmp_path):
    p = _add_product(db_session, "pharmonline", "Foo 500mg", ext_id="PHM-100")
    db_session.commit()

    csv_path = tmp_path / "stock.csv"
    csv_path.write_text(
        "sku,qty,name\nPHM-100,15,Foo 500mg\nPHM-999,0,Out of stock\nPHM-200,5,Unknown SKU\n",
        encoding="utf-8",
    )
    result = inventory.import_stock_from_csv(db_session, csv_path)
    assert result.total == 3
    assert result.matched_to_product == 1  # только PHM-100 нашли в Products
    assert result.unmatched == 2

    stock = inventory.get_stock_for_product(db_session, p.id)
    assert stock is not None
    assert stock.qty == 15
    assert stock.is_in_stock is True


def test_import_stock_overwrites_same_source(db_session, tmp_path):
    p = _add_product(db_session, "pharmonline", "X", ext_id="X-1")
    db_session.commit()

    csv1 = tmp_path / "s1.csv"
    csv1.write_text("sku,qty\nX-1,10\n", encoding="utf-8")
    inventory.import_stock_from_csv(db_session, csv1, source="erp")

    csv2 = tmp_path / "s2.csv"
    csv2.write_text("sku,qty\nX-1,3\n", encoding="utf-8")
    inventory.import_stock_from_csv(db_session, csv2, source="erp")

    # Должна остаться только последняя запись
    stocks = db_session.query(StockLevel).all()
    assert len(stocks) == 1
    assert stocks[0].qty == 3


def test_import_supplier_prices(db_session, tmp_path):
    p = _add_product(db_session, "pharmonline", "Bar", ext_id="BAR-1")
    db_session.commit()

    csv_path = tmp_path / "sup.csv"
    csv_path.write_text(
        "sku,supplier_name,purchase_price,currency,name\n"
        "BAR-1,Wholesale A,5.50,AZN,Bar\n"
        "BAR-1,Wholesale B,5.20,AZN,Bar\n",
        encoding="utf-8",
    )
    result = inventory.import_supplier_prices_from_csv(db_session, csv_path)
    assert result.total == 2
    assert result.matched_to_product == 2

    # Минимальная закупка
    min_price = inventory.get_min_purchase_price(db_session, p.id)
    assert min_price == 5.20


def test_cli_supplier_import_upserts_row_owned_by_another_source(db_session, tmp_path):
    product = _add_product(db_session, "pharmonline", "Shared", ext_id="SHARED-1")
    db_session.add(
        SupplierPrice(
            product_id=product.id,
            sku="SHARED-1",
            supplier_name="Vendor",
            purchase_price=2.0,
            currency="AZN",
            source="dashboard_csv",
        )
    )
    db_session.commit()
    csv_path = tmp_path / "shared.csv"
    csv_path.write_text(
        "sku,supplier_name,purchase_price,currency,name\n"
        "SHARED-1,Vendor,3.25,AZN,Shared\n",
        encoding="utf-8",
    )

    result = inventory.import_supplier_prices_from_csv(
        db_session, csv_path, source="manual_csv"
    )

    assert result.matched_to_product == 1
    rows = db_session.query(SupplierPrice).filter_by(product_id=product.id).all()
    assert len(rows) == 1
    assert rows[0].purchase_price == 3.25
    assert rows[0].source == "manual_csv"


def test_margin_report(db_session, tmp_path):
    p = _add_product(db_session, "pharmonline", "Test 100mg", ext_id="T-1")
    db_session.commit()
    run = _add_run(db_session)
    _add_snap(db_session, run, p, 10.00)

    db_session.add(
        SupplierPrice(
            product_id=p.id,
            supplier_name="WS",
            purchase_price=6.50,
            name="Test 100mg",
        )
    )
    db_session.add(
        StockLevel(
            product_id=p.id,
            qty=20,
            is_in_stock=True,
        )
    )
    db_session.commit()

    rows = inventory.margin_report(db_session)
    assert len(rows) == 1
    r = rows[0]
    assert r.sale_price == 10.0
    assert r.purchase_price == 6.5
    assert r.margin_azn == 3.5
    assert r.margin_pct == 35.0
    assert r.in_stock is True


def test_roi_undercut_skipped_when_out_of_stock(db_session):
    """Если товар out-of-stock — undercut не должен попасть в actions."""
    m = Match(canonical_name="Out", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    p_client = _add_product(db_session, "pharmonline", "Out", "ph-out", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Out", "al-out", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 7.0)
    # Stock = 0
    db_session.add(StockLevel(product_id=p_client.id, qty=0, is_in_stock=False))
    db_session.commit()

    actions = roi.compute_actions(db_session)
    undercuts = [a for a in actions if a.type == "undercut"]
    assert undercuts == []  # пропустили — нечего продавать


def test_roi_undercut_kept_when_in_stock(db_session):
    """Если товар на складе — undercut должен фигурировать."""
    m = Match(canonical_name="In", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    p_client = _add_product(db_session, "pharmonline", "In", "ph-in", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "In", "al-in", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 7.0)
    db_session.add(StockLevel(product_id=p_client.id, qty=5, is_in_stock=True))
    db_session.commit()

    actions = roi.compute_actions(db_session)
    undercuts = [a for a in actions if a.type == "undercut"]
    assert len(undercuts) == 1


def test_roi_undercut_critical_when_target_below_purchase(db_session):
    """Если рекомендуемая цена ниже закупки — severity=critical + warning в detail."""
    m = Match(canonical_name="LowMargin", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    p_client = _add_product(db_session, "pharmonline", "LowMargin", "ph-lm", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "LowMargin", "al-lm", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 5.0)  # сильный undercut
    # Закупка 6 ₼ — рекомендуемая (4.99) ниже закупки!
    db_session.add(
        SupplierPrice(
            product_id=p_client.id,
            supplier_name="X",
            purchase_price=6.0,
        )
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    undercuts = [a for a in actions if a.type == "undercut"]
    assert len(undercuts) == 1
    assert undercuts[0].severity == "critical"
    assert "ниже закупки" in undercuts[0].detail or "убыток" in undercuts[0].detail
