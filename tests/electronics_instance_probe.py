"""Экземпляр электроники целиком, в своём процессе: сбор командой `run`.

Запускается из tests/test_electronics_instance.py с ``PM_PROFILE=electronics``
и своей базой SQLite. Подменены только сами сайты: сбор, запись, проверка
полноты, сопоставление, алерты, отчёт и рекомендации идут настоящим путём.
Печатает ``RESULT=<json>``.
"""

from __future__ import annotations

import asyncio
import json
import sys

from click.testing import CliRunner
from sqlalchemy import func, select

from src import main, site_profile, site_routes, storage
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo

assert site_profile.PROFILE.name == "electronics", site_profile.PROFILE.name

# Один и тот же телефон и телевизор на трёх сайтах; у каждого сайта свои
# идентификаторы и своё написание названия.
CATALOG = {
    "kontakt": {
        "telefonlar": [
            ("TM-DG-SBP-1105-SM-2531", "Samsung Galaxy A55 8/256GB Awesome Navy", 899.99, 849.99),
            ("TM-DG-SBP-1105-AP-0001", "Apple iPhone 15 128GB Black", 1899.0, None),
        ],
        "tv": [("TM-TV-LG-0001", 'LG 55UR78006LK 55" 4K Smart TV', 1299.0, None)],
    },
    "irshad": {
        "telefonlar": [
            ("108753", "Samsung Galaxy A55 8/256 GB Navy", 879.0, None),
            ("108751", "Apple iPhone 15 128 GB Black", 1949.0, 1849.0),
        ],
        "tv-ve-audio": [("85759", "LG 55UR78006LK", 1249.0, None)],
    },
    "bakuelectronics": {
        "telefonlar-qadcetler/smartfonlar-mobil-telefonlar": [
            ("235291", "Smartfon Samsung Galaxy A55 8GB/256GB Awesome Navy", 869.99, None),
            ("218122", "Smartfon Apple iPhone 15 128GB Black", 1899.99, None),
        ],
        "tv-audio-video": [("200100", 'Televizor LG 55UR78006LK 55"', 1279.99, 1199.99)],
    },
}


def _fake(site: str) -> type[BaseScraper]:
    class _Site(BaseScraper):
        site_name = site
        base_url = f"https://{site}.invalid"

        def __init__(self, **_kwargs):
            super().__init__()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape_category(self, category_slug: str, limit: int | None = None):
            rows = CATALOG[site][category_slug]
            for external_id, name, price, discount in rows:
                await asyncio.sleep(0)
                yield ScrapedProduct(
                    site=site,
                    external_id=external_id,
                    url=f"https://{site}.invalid/p/{external_id}",
                    name=name,
                    price=price,
                    discount_price=discount,
                    is_on_sale=discount is not None,
                    category=category_slug,
                    identity_verified=True,
                    offer_availability_status="in_stock",
                    availability_source=f"{site}_fixture",
                )
            self._set_route_status(
                category_slug,
                complete=True,
                raw_items=len(rows),
                parsed_items=len(rows),
                expected_items=len(rows),
            )

        async def scrape_promos(self):
            return [ScrapedPromo(site=site, title="Promo")]

    _Site.__name__ = f"Fake_{site}"
    return _Site


def main_probe() -> None:
    for site in site_profile.SITES:
        main.SCRAPER_CLASSES[site] = _fake(site)

    storage.init_db()
    Session = storage.make_session()
    with Session() as session:
        for site in site_profile.SITES:
            plan = site_routes.plan_route_sync(session, site, list(CATALOG[site]))
            site_routes.apply_route_sync(session, plan)
        session.commit()

    runner = CliRunner()
    outcome: dict[str, object] = {"runs": []}
    for site in sys.argv[1].split(","):
        result = runner.invoke(main.cli, ["run", "--site", site, "--force"], catch_exceptions=True)
        outcome["runs"].append(
            {
                "site": site,
                "exit_code": result.exit_code,
                "output_tail": result.output[-1500:],
                "exception": repr(result.exception) if result.exception else None,
            }
        )

    with Session() as session:
        outcome["run_rows"] = [
            {
                "sites": run.sites_completed,
                "status": run.status,
                "scope": run.catalog_scope,
                "verified": run.catalog_verified,
                "reason": run.catalog_verification_reason,
                "products": run.products_scraped,
                "error": run.error_message,
            }
            for run in session.scalars(select(storage.Run).order_by(storage.Run.id)).all()
        ]
        outcome["products_per_site"] = dict(
            session.execute(
                select(storage.Product.site, func.count()).group_by(storage.Product.site)
            ).all()
        )
        outcome["snapshots"] = session.scalar(
            select(func.count()).select_from(storage.PriceSnapshot)
        )
        outcome["matches"] = session.scalar(select(func.count()).select_from(storage.Match))
        outcome["matched_products"] = session.scalar(
            select(func.count())
            .select_from(storage.Product)
            .where(storage.Product.canonical_id.is_not(None))
        )
        outcome["tenants"] = [
            (tenant.slug, tenant.client_site)
            for tenant in session.scalars(select(storage.Tenant)).all()
        ]
        # «Свой магазин»: сравнение и Excel глазами каждого из трёх сайтов.
        import io

        from openpyxl import load_workbook

        from src import comparison_export, comparison_rows

        outcome["viewers"] = {}
        for viewer in site_profile.SITES:
            rows = comparison_rows.comparison_rows(session, tenant_id=1, client_site=viewer)
            workbook = load_workbook(
                io.BytesIO(
                    comparison_export.build_workbook(
                        rows,
                        locale="ru",
                        generated_at=storage.utcnow(),
                        client_site=viewer,
                    )
                )
            )
            outcome["viewers"][viewer] = {
                "rows": len(rows),
                "counts": comparison_export.difference_counts(rows, viewer),
                "first_price_column": workbook.worksheets[1].cell(row=1, column=3).value,
                "roi_cached": session.scalar(
                    select(func.count())
                    .select_from(storage.RoiActionsCache)
                    .where(storage.RoiActionsCache.client_site == viewer)
                ),
            }
    print("RESULT=" + json.dumps(outcome, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main_probe()
