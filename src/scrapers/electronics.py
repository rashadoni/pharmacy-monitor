"""Сборщики магазинов электроники и их проба с живого сайта.

Реестр ``ELECTRONICS_SCRAPERS`` — сборщики профиля «электроника» (клиент —
Kontakt Home, конкуренты — Irshad и Baku Electronics). Экземпляр с
``PM_PROFILE=electronics`` берёт их в общий прогон (`src.main.SCRAPER_CLASSES`);
аптечный экземпляр о них не знает. Сам профиль — `src/site_profile.py`.

Проба — посмотреть, что сборщик читает с сайта, ничего не записывая в базу:

    python -m src.scrapers.electronics --site irshad --discover
    python -m src.scrapers.electronics --site irshad --category foto-texnika --limit 20

Для kontakt и bakuelectronics с сервера нужен прокси: задать ``DECODO_USERNAME``,
``DECODO_PASSWORD`` и перечислить сайт в ``DECODO_SITES``.
"""

from __future__ import annotations

import asyncio
import json

import click

from src import site_profile
from src.scrapers.bakuelectronics import BakuElectronicsScraper
from src.scrapers.http_catalog import HttpCatalogScraper
from src.scrapers.irshad import IrshadScraper
from src.scrapers.kontakt import KontaktScraper

CLIENT_SITE = site_profile.ELECTRONICS.client_site
ELECTRONICS_SCRAPERS: dict[str, type[HttpCatalogScraper]] = {
    "kontakt": KontaktScraper,
    "irshad": IrshadScraper,
    "bakuelectronics": BakuElectronicsScraper,
}


async def _probe(
    site: str, category: str | None, limit: int | None, discover: bool, rate: float | None
) -> dict:
    out: dict = {"site": site}
    async with ELECTRONICS_SCRAPERS[site](rate_limit_sec=rate) as scraper:
        if discover:
            out["categories"] = await scraper.discover_categories()
        if category:
            result = await scraper.scrape([category], limit_per_category=limit)
            status = result.route_statuses.get(category)
            out["route"] = {
                "complete": bool(status and status.complete),
                "abort_reason": status.abort_reason if status else None,
                "expected_items": status.expected_items if status else None,
                "expected_pages": status.expected_pages if status else None,
                "visited_pages": status.visited_pages if status else None,
                "raw_items": status.raw_items if status else None,
                "parsed_items": status.parsed_items if status else None,
            }
            out["errors"] = result.errors
            out["products"] = [
                {
                    "id": p.external_id,
                    "name": p.name,
                    "price": p.price,
                    "discount_price": p.discount_price,
                    "availability": p.offer_availability_status,
                    "quantity": p.offer_quantity,
                    "url": p.url,
                }
                for p in result.products
            ]
        out["fetch_retries"] = scraper.fetch_retries
    return out


@click.command()
@click.option("--site", required=True, type=click.Choice(list(ELECTRONICS_SCRAPERS)))
@click.option("--category", default=None, help="Раздел сайта, как его понимает сборщик.")
@click.option("--limit", default=None, type=int, help="Не больше стольких товаров.")
@click.option("--discover", is_flag=True, help="Показать разделы полного каталога.")
@click.option("--rate", default=None, type=float, help="Пауза между запросами, секунд.")
def main(
    site: str, category: str | None, limit: int | None, discover: bool, rate: float | None
) -> None:
    """Прочитать раздел живого сайта и напечатать, что получилось. В базу не пишет."""
    if not category and not discover:
        raise click.UsageError("нужен --category или --discover")
    click.echo(
        json.dumps(
            asyncio.run(_probe(site, category, limit, discover, rate)),
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
