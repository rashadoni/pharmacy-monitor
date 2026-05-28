"""Подкладка реалистичных fake-данных для демо/тестирования.

Используется через CLI: `pharmacy-monitor seed-demo`.

Что создаётся:
- 2 Run'а (вчера + сегодня, оба status=ok)
- 30 Product'ов: 10 на каждом из 3 сайтов
- 10 Match-кластеров (по 3 продукта в каждом — один на каждом сайте)
- 5 unmatched products на каждом сайте (для gap-анализа)
- 60 PriceSnapshots (30 продуктов × 2 прогона) с лёгкими изменениями цен
- 3 Promo на сегодняшний прогон

Не трогает Categories / Recipients / TrackedProducts (это конфиг пользователя).
"""

from __future__ import annotations

from datetime import timedelta
from src._time import utcnow

import structlog
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.normalize import normalize_name
from src.storage import (
    Match,
    MatchRejection,
    PriceSnapshot,
    Product,
    Promo,
    Run,
)

log = structlog.get_logger()


# === SEED-ДАННЫЕ — реалистичные товары на 3 сайтах ===

# Каждый кортеж: (canonical_name, brand, dosage, pack_size,
#                 ph_name, ap_name, al_name,
#                 ph_price, ap_price, al_price)
# ph_name/ap_name/al_name — варианты имени на каждом сайте (немного отличаются)

SEED_MATCHED = [
    (
        "Paracetamol 500mg N20",
        "Bayer",
        "500mg",
        "N20",
        "Paracetamol 500mq № 20 (Tabletlər)",
        "Paracetamol 500mg #20 tablet",
        "Paracetamol 500 mq 20 əd",
        1.20,
        1.15,
        1.18,
    ),
    (
        "Aspirin Cardio 100mg N30",
        "Bayer",
        "100mg",
        "N30",
        "Aspirin Cardio 100 mq № 30",
        "Aspirin Cardio 100mg N30",
        "Aspirin Cardio 100 mq 30 əd",
        8.50,
        7.99,
        8.20,
    ),
    (
        "Diazolin 0.05g N10",
        "Darnitsa",
        "0.05g",
        "N10",
        "Diazolin 0,05 q № 10 (Tabletlər)",
        "Diazolin 50mg N10",
        "Diazolin 0.05 q 10 əd",
        0.85,
        0.95,
        0.90,
    ),
    (
        "Amoxicillin 500mg N20",
        "Sandoz",
        "500mg",
        "N20",
        "Amoxicillin 500 mq № 20",
        "Amoxicillin 500mg #20 capsules",
        "Amoxicillin 500 mq 20 əd",
        4.20,
        3.95,
        4.10,
    ),
    (
        "Vitamin D3 2000 IU N60",
        "Solgar",
        "2000IU",
        "N60",
        "Vitamin D3 2000 IU № 60 (Solgar)",
        "Solgar Vitamin D3 2000 IU 60 tab",
        "Vitamin D3 Solgar 2000 IU 60 əd",
        24.50,
        22.80,
        23.50,
    ),
    (
        "Friso Gold 1 800g",
        "Friso",
        None,
        "800g",
        "Friso Gold 1 (0-6 ay) 800 q",
        "Friso Gold 1 800g sud qarışığı",
        "Friso Gold 1 800 q",
        38.50,
        36.99,
        37.50,
    ),
    (
        "Friso Gold 2 800g",
        "Friso",
        None,
        "800g",
        "Friso Gold 2 (6-12 ay) 800 q",
        "Friso Gold 2 800g sud qarışığı",
        "Friso Gold 2 800 q",
        38.50,
        36.99,
        39.99,
    ),
    (
        "Pampers Premium Care S2 132 шт",
        "Pampers",
        None,
        "132pcs",
        "Pampers Premium Care 2 (4-8 kq) № 132",
        "Pampers Premium Care 2 132 ədəd",
        "Pampers Premium Care S2 132 əd",
        49.90,
        48.50,
        47.99,
    ),
    (
        "Nivea Cream 200ml",
        "Nivea",
        None,
        "200ml",
        "Nivea Cream 200 ml",
        "Nivea Creme 200ml",
        "Nivea Cream 200 ml",
        7.20,
        6.99,
        7.50,
    ),
    (
        "Bepanthen Cream 30g",
        "Bayer",
        None,
        "30g",
        "Bepanthen krem 30 q",
        "Bepanthen 5% cream 30g",
        "Bepanthen krem 30 q",
        12.50,
        11.99,
        12.20,
    ),
]

# Unmatched товары на каждом сайте (для gap-анализа, видны во вкладке Обзор)
SEED_UNMATCHED = {
    "pharmonline": [
        ("Citramon P № 10", "Darnitsa", 0.45),
        ("Validol № 10", "Pharmstandard", 0.60),
        ("Pharmonline-эксклюзив", "Local", 5.00),
    ],
    "aptekonline": [
        ("Mezym Forte N20", "Berlin Chemie", 12.30),
        ("No-Spa 40mg N24", "Sanofi", 6.80),
        ("Aptekonline-эксклюзив", "Local", 4.50),
    ],
    "aloe": [
        ("Aloe Vera Gel 200ml", "Aloe", 9.99),
        ("Multivitamin Plus N30", "Aloe", 14.50),
        ("Aloe-эксклюзив", "Aloe", 3.99),
    ],
}

SEED_PROMOS = [
    {
        "site": "aptekonline",
        "title": "Скидка 30% на витамины Solgar до конца недели",
        "landing_url": "https://www.aptekonline.az/promo/solgar-30",
    },
    {
        "site": "aloe",
        "title": "Bepanthen — 2 по цене 1",
        "landing_url": "https://aloe.az/promo/bepanthen",
    },
    {
        "site": "pharmonline",
        "title": "Friso детское питание -15%",
        "landing_url": "https://pharmonline.az/promo/friso",
    },
]


def _wipe_demo_data(session: Session) -> None:
    """Очистить только данные прогонов — категории/получателей/watchlist оставляем."""
    session.execute(delete(MatchRejection))
    session.execute(delete(PriceSnapshot))
    session.execute(delete(Promo))
    # Снимаем canonical_id с продуктов чтобы можно было удалить Match'и
    session.execute(Product.__table__.update().values(canonical_id=None))
    session.execute(delete(Match))
    session.execute(delete(Product))
    session.execute(delete(Run))
    session.commit()


def has_existing_run_data(session: Session) -> bool:
    """Есть ли в БД реальные данные прогонов?"""
    return (
        session.scalar(Run.__table__.select().limit(1)) is not None
        or session.scalar(Product.__table__.select().limit(1)) is not None
    )


def seed_demo(session: Session, force: bool = False) -> dict:
    """Заполнить БД реалистичными fake-данными.

    Returns:
        {"runs": 2, "products": 30, "matches": 10, "promos": 3}
    """
    if has_existing_run_data(session) and not force:
        raise RuntimeError(
            "В БД уже есть данные прогонов. "
            "Запусти `seed-demo --force` если хочешь стереть их и заполнить fake'ом."
        )

    if force:
        _wipe_demo_data(session)

    today = utcnow().replace(hour=6, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    # --- Runs ---
    n_seed_products = 3 * len(SEED_MATCHED) + sum(len(v) for v in SEED_UNMATCHED.values())
    run_yesterday = Run(
        started_at=yesterday,
        finished_at=yesterday + timedelta(minutes=2),
        status="ok",
        sites_completed="pharmonline,aptekonline,aloe",
        products_scraped=n_seed_products,
    )
    run_today = Run(
        started_at=today,
        finished_at=today + timedelta(minutes=2),
        status="ok",
        sites_completed="pharmonline,aptekonline,aloe",
        products_scraped=n_seed_products,
    )
    session.add_all([run_yesterday, run_today])
    session.flush()

    products_total = 0
    matches_total = 0

    # --- Сматченные кластеры ---
    for idx, (
        canon_name,
        brand,
        dosage,
        pack,
        ph_name,
        ap_name,
        al_name,
        ph_price,
        ap_price,
        al_price,
    ) in enumerate(SEED_MATCHED):
        m = Match(
            canonical_name=canon_name,
            canonical_brand=brand,
            canonical_dosage=dosage,
            canonical_pack_size=pack,
            confidence=1.0,
            is_manual=False,
        )
        session.add(m)
        session.flush()
        matches_total += 1

        for site, name, price in [
            ("pharmonline", ph_name, ph_price),
            ("aptekonline", ap_name, ap_price),
            ("aloe", al_name, al_price),
        ]:
            external_id = f"demo-{site}-m{idx}"
            url = _demo_url(site, external_id, name)
            p = Product(
                site=site,
                external_id=external_id,
                url=url,
                name=name,
                name_normalized=normalize_name(name),
                brand=brand,
                dosage=dosage,
                pack_size=pack,
                category="demo",
                canonical_id=m.id,
                first_seen_at=yesterday,
                last_seen_at=today,
            )
            session.add(p)
            session.flush()
            products_total += 1

            # 2 snapshot'а: вчера и сегодня (с лёгким движением цены ±3%)
            yest_price = round(price * (1 + (idx % 5 - 2) * 0.01), 2)
            session.add(
                PriceSnapshot(
                    run_id=run_yesterday.id,
                    product_id=p.id,
                    price=yest_price,
                    captured_at=yesterday,
                )
            )
            session.add(
                PriceSnapshot(
                    run_id=run_today.id,
                    product_id=p.id,
                    price=price,
                    discount_price=round(price * 0.85, 2) if idx == 4 else None,
                    is_on_sale=(idx == 4),
                    promo_label="-15%" if idx == 4 else None,
                    captured_at=today,
                )
            )

    # --- Unmatched товары ---
    for site, items in SEED_UNMATCHED.items():
        for u_idx, (uname, ubrand, uprice) in enumerate(items):
            external_id = f"demo-{site}-u{u_idx}"
            url = _demo_url(site, external_id, uname)
            p = Product(
                site=site,
                external_id=external_id,
                url=url,
                name=uname,
                name_normalized=normalize_name(uname),
                brand=ubrand,
                category="demo",
                first_seen_at=yesterday,
                last_seen_at=today,
            )
            session.add(p)
            session.flush()
            products_total += 1

            session.add(
                PriceSnapshot(
                    run_id=run_yesterday.id,
                    product_id=p.id,
                    price=uprice,
                    captured_at=yesterday,
                )
            )
            session.add(
                PriceSnapshot(
                    run_id=run_today.id,
                    product_id=p.id,
                    price=uprice,
                    captured_at=today,
                )
            )

    # --- Promos ---
    for promo in SEED_PROMOS:
        session.add(
            Promo(
                run_id=run_today.id,
                site=promo["site"],
                title=promo["title"],
                landing_url=promo["landing_url"],
                captured_at=today,
            )
        )

    session.commit()

    summary = {
        "runs": 2,
        "products": products_total,
        "matches": matches_total,
        "promos": len(SEED_PROMOS),
    }
    log.info("seed_demo_done", **summary)
    return summary


def _demo_url(site: str, external_id: str, name: str) -> str:
    """Псевдо-URL для демо-данных. Не должен открываться, но выглядеть как настоящий."""
    slug = name.lower().replace(" ", "-").replace("/", "-")[:40]
    if site == "pharmonline":
        return f"https://pharmonline.az/product/{slug}-{external_id}"
    if site == "aptekonline":
        return f"https://www.aptekonline.az/product/{slug}"
    if site == "aloe":
        return f"https://aloe.az/{slug}/"
    return f"#{external_id}"
