"""Тесты bulk-optimized persist_results.

Регрессионная защита баг 2026-05-08: per-row session.flush() через SSH-tunnel
к Postgres растягивал 8K продуктов на ~75 минут. Фикс: pre-fetch existing
одним SELECT, единый flush на batch новых продуктов, commit per result.
"""

from sqlalchemy import select

from src import storage
from src.main import persist_results
from src.scrapers.base import ScrapedProduct, ScrapedPromo, ScrapeResult


def _make_product(site: str, ext_id: str, name: str, price: float = 10.0) -> ScrapedProduct:
    return ScrapedProduct(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=name,
        price=price,
    )


def test_persist_inserts_new_products(db_session):
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    result = ScrapeResult(
        site="aloe",
        products=[
            _make_product("aloe", "1", "Foo 100mg"),
            _make_product("aloe", "2", "Bar 200mg"),
        ],
    )
    count = persist_results(db_session, run, [result])

    assert count == 2
    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 2
    snaps = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps) == 2
    assert {s.product_id for s in snaps} == {p.id for p in products}


def test_persist_updates_existing_products(db_session):
    """Существующий по (site, external_id) → UPDATE, новый snapshot."""
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()

    persist_results(
        db_session,
        run1,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "X", "Old name", 10.0)])],
    )

    # Новый прогон с тем же external_id, но новым именем + ценой
    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()

    persist_results(
        db_session,
        run2,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "X", "New name", 12.0)])],
    )

    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 1, "должен остаться один Product, а не два"
    assert products[0].name == "New name", "имя должно обновиться"

    snaps = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps) == 2, "должно быть два snapshot'a (по одному на прогон)"


def test_persist_handles_mixed_new_and_existing(db_session):
    """Один result с миксом существующих + новых — bulk-pre-fetch не должен потерять новых."""
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()
    persist_results(
        db_session,
        run1,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "EXIST", "Old")])],
    )

    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()
    count = persist_results(
        db_session,
        run2,
        [
            ScrapeResult(
                site="aloe",
                products=[
                    _make_product("aloe", "EXIST", "Updated"),
                    _make_product("aloe", "NEW", "Brand new"),
                ],
            )
        ],
    )

    assert count == 2
    products = db_session.scalars(select(storage.Product)).all()
    assert {p.external_id for p in products} == {"EXIST", "NEW"}


def test_persist_commits_per_result(db_session):
    """Коммит per result — после persist_results данные видны в новой транзакции."""
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    persist_results(
        db_session,
        run,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "1", "Foo")])],
    )

    # Эмулируем "другую транзакцию" — expire всё и пере-fetch
    db_session.expire_all()
    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 1


def test_persist_handles_multiple_results(db_session):
    """Несколько ScrapeResult по разным сайтам — каждый коммитится независимо."""
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    count = persist_results(
        db_session,
        run,
        [
            ScrapeResult(
                site="aloe",
                products=[
                    _make_product("aloe", "A1", "Aloe One"),
                    _make_product("aloe", "A2", "Aloe Two"),
                ],
            ),
            ScrapeResult(
                site="pharmonline",
                products=[_make_product("pharmonline", "P1", "Pharmonline One")],
            ),
            ScrapeResult(
                site="aptekonline",
                products=[
                    _make_product("aptekonline", "AP1", "Aptek One"),
                    _make_product("aptekonline", "AP2", "Aptek Two"),
                    _make_product("aptekonline", "AP3", "Aptek Three"),
                ],
            ),
        ],
    )

    assert count == 6
    by_site = {}
    for p in db_session.scalars(select(storage.Product)).all():
        by_site.setdefault(p.site, 0)
        by_site[p.site] += 1
    assert by_site == {"aloe": 2, "pharmonline": 1, "aptekonline": 3}


def test_persist_promos(db_session):
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    persist_results(
        db_session,
        run,
        [
            ScrapeResult(
                site="aloe",
                promos=[ScrapedPromo(site="aloe", title="Скидка 20%", description="на витамины")],
            )
        ],
    )

    promos = db_session.scalars(select(storage.Promo)).all()
    assert len(promos) == 1
    assert promos[0].title == "Скидка 20%"


def test_persist_empty_results_returns_zero(db_session):
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    assert persist_results(db_session, run, []) == 0
    assert persist_results(db_session, run, [ScrapeResult(site="aloe")]) == 0


def test_persist_diff_only_skips_unchanged_price(db_session):
    """Цена та же → snapshot НЕ пишется, last_seen_at обновляется.

    Основная оптимизация diff-only (2026-05-09): pharm-цены статичны,
    ~95% продуктов имеют ту же цену что и вчера. Для них writes в
    `price_snapshots` бесполезен — БД растёт линейно, а данные не несут
    новой информации.
    """
    import time

    # Run 1: insert + snapshot
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()
    persist_results(
        db_session,
        run1,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "1", "Foo", price=10.0)])],
    )
    snaps_after_run1 = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps_after_run1) == 1
    last_seen_run1 = db_session.scalars(select(storage.Product)).one().last_seen_at
    time.sleep(0.01)  # чтобы last_seen_at реально отличался

    # Run 2: цена та же → snapshot НЕ пишется
    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()
    count = persist_results(
        db_session,
        run2,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "1", "Foo", price=10.0)])],
    )

    snaps_after_run2 = db_session.scalars(select(storage.PriceSnapshot)).all()
    product_after_run2 = db_session.scalars(select(storage.Product)).one()
    assert count == 1, "products обработан 1"
    assert len(snaps_after_run2) == 1, "snapshot не должен дублироваться"
    assert product_after_run2.last_seen_at > last_seen_run1, (
        "last_seen_at обязан обновиться даже без snapshot'а"
    )

    # Run 3: цена изменилась → snapshot пишется
    run3 = storage.Run(status="running")
    db_session.add(run3)
    db_session.commit()
    persist_results(
        db_session,
        run3,
        [ScrapeResult(site="aloe", products=[_make_product("aloe", "1", "Foo", price=11.50)])],
    )

    snaps_after_run3 = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps_after_run3) == 2, "цена изменилась → новый snapshot"
    latest = max(snaps_after_run3, key=lambda s: s.captured_at)
    assert latest.price == 11.50
    assert latest.run_id == run3.id


def test_persist_diff_only_detects_promo_change(db_session):
    """Промо-лейбл изменился → snapshot пишется даже если price тот же."""
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()

    sp1 = ScrapedProduct(
        site="aloe",
        external_id="1",
        url="http://aloe.az/p/1",
        name="Foo",
        price=10.0,
        is_on_sale=False,
        promo_label=None,
    )
    persist_results(db_session, run1, [ScrapeResult(site="aloe", products=[sp1])])
    assert len(db_session.scalars(select(storage.PriceSnapshot)).all()) == 1

    # Run 2: цена та же, акция включилась
    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()
    sp2 = ScrapedProduct(
        site="aloe",
        external_id="1",
        url="http://aloe.az/p/1",
        name="Foo",
        price=10.0,
        discount_price=8.0,
        is_on_sale=True,
        promo_label="-20%",
    )
    persist_results(db_session, run2, [ScrapeResult(site="aloe", products=[sp2])])

    snaps = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps) == 2, "акция активирована → snapshot обязан"
    latest = max(snaps, key=lambda s: s.captured_at)
    assert latest.is_on_sale is True
    assert latest.discount_price == 8.0
    assert latest.promo_label == "-20%"


def test_persist_chunks_large_results(db_session):
    """Регрессия SSL EOF: ScrapeResult с 500 продуктами должен чанкаться по
    _PERSIST_CHUNK (=200) — несколько мелких INSERT'ов вместо одного гигантского.

    Корень бага run_id=43 (2026-05-08): один add_all на 1000+ snapshot'ов
    + RETURNING давал 233KB SQL, SSH-tunnel захлёбывался посреди INSERT'а.
    """
    from src.main import _PERSIST_CHUNK

    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    # 500 продуктов = ~3 чанка по 200
    products = [_make_product("aloe", str(i), f"Product {i}") for i in range(500)]

    insert_sizes: list[int] = []
    from sqlalchemy import event

    def capture_inserts(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.strip().upper().startswith("INSERT")
            and "price_snapshots" in statement.lower()
        ):
            # SQLAlchemy шлёт executemany как один statement с N параметрами
            if executemany and isinstance(parameters, (list, tuple)):
                insert_sizes.append(len(parameters))
            elif isinstance(parameters, dict):
                # bulk insert: keys типа 'price__0', 'price__1' → находим max indexed
                indexed = [int(k.rsplit("__", 1)[1]) for k in parameters if "__" in k]
                insert_sizes.append(max(indexed) + 1 if indexed else 1)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture_inserts)
    try:
        count = persist_results(db_session, run, [ScrapeResult(site="aloe", products=products)])
    finally:
        event.remove(engine, "before_cursor_execute", capture_inserts)

    assert count == 500
    assert insert_sizes, "должны быть зафиксированы INSERT'ы в price_snapshots"
    assert max(insert_sizes) <= _PERSIST_CHUNK, (
        f"один INSERT на {max(insert_sizes)} строк превышает chunk-limit {_PERSIST_CHUNK} "
        "— SSH-tunnel может не выдержать"
    )

    # Проверим, что snapshots всё-таки записались
    snaps = db_session.scalars(select(storage.PriceSnapshot)).all()
    assert len(snaps) == 500


def test_persist_one_select_per_site_not_per_product(db_session):
    """Регрессия N+1: при 50 продуктах одного сайта должно быть 1 SELECT, не 50."""
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    products = [_make_product("aloe", str(i), f"Product {i}") for i in range(50)]

    select_count = 0
    from sqlalchemy import event

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        nonlocal select_count
        if statement.strip().upper().startswith("SELECT") and "products" in statement.lower():
            select_count += 1

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        persist_results(db_session, run, [ScrapeResult(site="aloe", products=products)])
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    # 1 для existing pre-fetch, плюс возможные внутренние ORM SELECT'ы. До фикса
    # было бы ≥50 (по одному на каждый продукт).
    assert select_count < 10, f"ожидалось ≤10 SELECT'ов, получили {select_count} (N+1 не пофикшен?)"


def test_persist_updates_url_on_existing_product(db_session):
    """Регрессия 2026-05-27: existing.url не обновлялся → 6238 продуктов
    застряли на '/True' после фикса pharmonline_ddp `path` vs `postQuery`.

    Свежий run с правильным URL должен перезаписать broken slug.
    """
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()

    # Симулируем старый прогон: продукт с broken URL '/True'
    broken = ScrapedProduct(
        site="pharmonline",
        external_id="EXT1",
        url="/True",
        name="Aspirin 100mg",
        price=5.0,
    )
    persist_results(db_session, run1, [ScrapeResult(site="pharmonline", products=[broken])])

    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 1
    assert products[0].url == "/True"

    # Свежий прогон с правильным slug
    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()

    fixed = ScrapedProduct(
        site="pharmonline",
        external_id="EXT1",
        url="/product/aspirin-100mg-tab-30",
        name="Aspirin 100mg",
        price=5.0,
    )
    persist_results(db_session, run2, [ScrapeResult(site="pharmonline", products=[fixed])])

    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 1, "тот же external_id → UPDATE, не INSERT"
    assert products[0].url == "/product/aspirin-100mg-tab-30", "URL должен обновиться"


def test_persist_keeps_existing_url_when_scraper_returns_empty(db_session):
    """Defensive: если скрейпер вернёт пустой/None url, сохраняем старый."""
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()

    good = ScrapedProduct(
        site="aloe",
        external_id="EXT2",
        url="https://aloe.az/p/good-url",
        name="Foo",
        price=10.0,
    )
    persist_results(db_session, run1, [ScrapeResult(site="aloe", products=[good])])

    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()

    empty_url = ScrapedProduct(
        site="aloe",
        external_id="EXT2",
        url="",  # эмулируем баг скрейпера
        name="Foo",
        price=10.0,
    )
    persist_results(db_session, run2, [ScrapeResult(site="aloe", products=[empty_url])])

    products = db_session.scalars(select(storage.Product)).all()
    assert len(products) == 1
    assert products[0].url == "https://aloe.az/p/good-url", "пустой url не должен стирать good URL"


def test_persist_missing_or_unknown_offer_never_becomes_out_of_stock(db_session):
    """Absence is not evidence: only an explicit zero may mark website OOS."""
    run1 = storage.Run(status="running")
    db_session.add(run1)
    db_session.commit()
    known = ScrapedProduct(
        site="aloe",
        external_id="STOCK-1",
        url="https://aloe.az/stock-1",
        name="Stocked",
        price=10.0,
        manufacturer_country_raw="Serbia",
        country_source="detail",
        offer_availability_status="in_stock",
        offer_quantity=4,
        availability_source="quantity",
    )
    persist_results(db_session, run1, [ScrapeResult(site="aloe", products=[known])])

    # A later full result that simply does not contain the product must not
    # infer OOS. A later explicit "unknown" observation must not erase it.
    run2 = storage.Run(status="running")
    db_session.add(run2)
    db_session.commit()
    persist_results(db_session, run2, [ScrapeResult(site="aloe", products=[])])

    run3 = storage.Run(status="running")
    db_session.add(run3)
    db_session.commit()
    unknown = ScrapedProduct(
        site="aloe",
        external_id="STOCK-1",
        url="https://aloe.az/stock-1",
        name="Stocked",
        price=10.0,
    )
    persist_results(db_session, run3, [ScrapeResult(site="aloe", products=[unknown])])

    product = db_session.scalar(
        select(storage.Product).where(storage.Product.external_id == "STOCK-1")
    )
    assert product is not None
    assert product.offer_availability_status == "in_stock"
    assert product.offer_quantity == 4
    observations = db_session.scalars(
        select(storage.OfferObservation)
        .where(storage.OfferObservation.product_id == product.id)
        .order_by(storage.OfferObservation.run_id)
    ).all()
    assert [row.availability_status for row in observations] == ["in_stock", "unknown"]
