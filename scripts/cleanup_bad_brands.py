#!/usr/bin/env python
"""One-off: установить Product.brand=NULL для записей где brand попал в blacklist.

P0.2 (PO Audit 2026-05-17): generic слова (Gigiyenik, Optik, Diapers,
Antiperspirant, Günəşdən, Daha, Linkas) показывались как «бренды» на
/analytics top-15 и /site/X top brands. Расширили _BLOCKLIST_FIRST_WORD
в brand_catalog.py, но существующие записи в БД остались грязные.

Этот скрипт чистит их одним SQL. Идемпотентен — повторный запуск ничего
не меняет (brand уже NULL).

Запуск (на проде):
    cd /opt/pharmacy-monitor && \\
      set -a; . /etc/pharmacy-monitor/env; set +a; \\
      .venv/bin/python scripts/cleanup_bad_brands.py
"""
from __future__ import annotations

import sys

from sqlalchemy import func, update

from src import storage
from src.brand_catalog import _BLOCKLIST_FIRST_WORD, is_brand_blacklisted


def main() -> int:
    storage.init_db()
    Session = storage.make_session()
    with Session() as session:
        # Сначала статистика — сколько Product.brand попадают в blacklist
        all_rows = session.execute(
            "SELECT brand, COUNT(*) AS n "
            "FROM products WHERE brand IS NOT NULL "
            "GROUP BY brand ORDER BY n DESC"
        ).fetchall() if False else []

        # SQLAlchemy core путь — безопаснее без raw SQL
        bad_brands = set()
        rows = session.execute(
            storage.Product.__table__.select()
            .with_only_columns(
                storage.Product.brand, func.count(storage.Product.id).label("n")
            )
            .where(storage.Product.brand.is_not(None))
            .group_by(storage.Product.brand)
        ).all()
        for brand, n in rows:
            if is_brand_blacklisted(brand):
                bad_brands.add(brand)

        if not bad_brands:
            print("nothing to clean — all brands look ok")
            return 0

        print(f"found {len(bad_brands)} blacklisted brand values:")
        for b in sorted(bad_brands):
            count = session.scalar(
                storage.Product.__table__.select()
                .with_only_columns(func.count())
                .where(storage.Product.brand == b)
            )
            print(f"  {b!r:<25}  {count} products")

        affected = session.execute(
            update(storage.Product)
            .where(storage.Product.brand.in_(bad_brands))
            .values(brand=None)
        ).rowcount
        session.commit()
        print(f"\nupdated {affected} rows (brand → NULL)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
