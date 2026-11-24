"""Собрать каталог Kontakt Home в настоящем Chrome и записать файл для загрузки.

Запускается на Маке владельца (``infra/local/kontakt/run.sh``), не на сервере:

    python -m src.kontakt_collect --out kontakt.csv --profile ~/…/chrome-profile

Обход — тот же, что у сборщика ``kontakt.py``: от меню сайта все категории-
списки, каждая — цельным проходом с проверкой числа товаров. Итог — CSV в
формате ``catalog import`` (``src/catalog_file.py``): сервер принимает его тем
же путём, что файл, собранный руками, и проверяет ещё раз.

Колонки: name, url, price, sale_price, category, availability. Артикула в файле
нет намеренно: каталог Kontakt впервые загружен 2026-11-13 файлом без артикула,
и товар там узнан по адресу страницы. Колонка с артикулом дала бы каждому
товару новый идентификатор — весь каталог вторыми строками, без истории цен.

Если Cloudflare показывает галочку «Verify you are human», сборщик зовёт
владельца уведомлением и ждёт его клика (``--human-wait-minutes``); сам он
ничего не нажимает (решение владельца 2026-11-24).

Коды выхода: 0 — файл записан; 2 — проверку Cloudflare не прошли (никто не
нажал галочку за отведённое время); 3 — обход неполный (больше допустимого числа категорий
не дочитано) или товаров нет; 4 — обход не уложился в ``--max-minutes``;
1 — прочая ошибка. Файл пишется только при 0.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import subprocess
import sys
from pathlib import Path

import structlog

from src.scrapers.base import CaptchaDetected, ScrapedProduct
from src.scrapers.http_catalog import PageFetchError
from src.scrapers.kontakt_browser import KontaktBrowserScraper, describe

log = structlog.get_logger()

COLUMNS = ("name", "url", "price", "sale_price", "category", "availability")
# Длина колонки ``products.category``; длиннее — файл отвергнут целиком.
_CATEGORY_MAX = 100
# Доля категорий, которые можно не дочитать: их товары идут лучшим из проходов.
# Одна неудачная категория не должна стоить всей ночи; много — значит, что-то
# не так с сайтом, и такой файл не уходит.
DEFAULT_MAX_INCOMPLETE_SHARE = 0.03


class CollectError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def category_label(path: str) -> str:
    """Путь категории, укороченный до колонки базы по границе раздела."""
    if len(path) <= _CATEGORY_MAX:
        return path
    parts = path.split("/")
    while len(parts) > 1 and len("/".join(parts)) > _CATEGORY_MAX:
        parts.pop(0)
    return "/".join(parts)[-_CATEGORY_MAX:]


def _category_rank(row: dict[str, str]) -> tuple[int, str]:
    category = row["category"]
    return (-category.count("/"), category)


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def product_row(product: ScrapedProduct) -> dict[str, str]:
    availability = product.offer_availability_status
    return {
        "name": product.name,
        "url": product.url,
        # price — обычная цена, sale_price — цена со скидкой, если она есть.
        "price": _money(product.price),
        "sale_price": _money(product.discount_price),
        "category": category_label(product.category or ""),
        "availability": availability if availability in ("in_stock", "out_of_stock") else "",
    }


def write_catalog(rows: list[dict[str, str]], out: Path) -> None:
    """Записать файл атомарно: недописанный файл не должен уйти на сервер."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, out)


async def collect(
    scraper: KontaktBrowserScraper,
    *,
    max_incomplete_share: float = DEFAULT_MAX_INCOMPLETE_SHARE,
    category_limit: int | None = None,
) -> list[dict[str, str]]:
    """Обойти каталог и вернуть строки файла; неполный обход — ``CollectError``."""
    try:
        categories = await scraper.discover_categories()
    except CaptchaDetected as exc:
        raise CollectError(f"Cloudflare остановил обход меню: {exc}", 2) from exc
    if category_limit is not None:
        categories = categories[:category_limit]
    log.info("kontakt_collect_categories", count=len(categories))
    by_url: dict[str, dict[str, str]] = {}
    for number, category in enumerate(categories, start=1):
        try:
            async for product in scraper.scrape_category(category):
                row = product_row(product)
                # Товар стоит и в разделе, и во вложенном списке. Категория —
                # самая глубокая (точнее), при равной глубине — меньшая по
                # алфавиту: от порядка обхода она зависеть не должна, иначе
                # группы на дашборде перетасовывались бы каждую ночь.
                earlier = by_url.get(product.url)
                if earlier is None or _category_rank(row) < _category_rank(earlier):
                    by_url[product.url] = row
        except CaptchaDetected as exc:
            raise CollectError(f"Cloudflare остановил сбор на {category}: {exc}", 2) from exc
        if number % 50 == 0:
            log.info(
                "kontakt_collect_progress", done=number, of=len(categories), products=len(by_url)
            )

    statuses = scraper._route_statuses
    incomplete = [slug for slug in categories if not getattr(statuses.get(slug), "complete", False)]
    log.info("kontakt_collect_done", products=len(by_url), summary=describe(scraper))
    allowed = int(len(categories) * max_incomplete_share)
    if len(incomplete) > allowed:
        raise CollectError(
            f"не дочитано категорий: {len(incomplete)} из {len(categories)} "
            f"(допустимо {allowed}); первые: {', '.join(incomplete[:5])}",
            3,
        )
    if not by_url:
        raise CollectError("ни одного товара — файл не записан", 3)
    return list(by_url.values())


def notify_human_needed() -> None:
    """Позвать владельца: Cloudflare показал галочку, нажать её должен человек."""
    print("kontakt: нужна галочка Cloudflare — нажмите её в окне Chrome сборщика", file=sys.stderr)
    if sys.platform != "darwin":
        return
    script = (
        'display notification "Нажмите галочку «Verify you are human» в окне Chrome '
        'сборщика — дальше сбор пойдёт сам" with title "Kontakt" sound name "Glass"'
    )
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


async def _main(args: argparse.Namespace) -> int:
    scraper = KontaktBrowserScraper(
        args.profile,
        headless=args.headless,
        human_wait_sec=args.human_wait_minutes * 60,
        on_human_needed=notify_human_needed,
    )
    try:
        async with scraper:
            rows = await asyncio.wait_for(
                collect(
                    scraper,
                    max_incomplete_share=args.max_incomplete_share,
                    category_limit=args.category_limit,
                ),
                timeout=args.max_minutes * 60 if args.max_minutes else None,
            )
    except TimeoutError:
        print(f"kontakt: обход не уложился в {args.max_minutes} минут", file=sys.stderr)
        return 4
    except CaptchaDetected as exc:
        print(f"kontakt: {exc}", file=sys.stderr)
        return 2
    except CollectError as exc:
        print(f"kontakt: {exc}", file=sys.stderr)
        return exc.code
    except PageFetchError as exc:
        print(f"kontakt: страница не получена: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        if type(exc).__name__ != "TargetClosedError":
            raise
        print("kontakt: окно Chrome сборщика закрыто — сбор прерван", file=sys.stderr)
        return 1
    write_catalog(rows, Path(args.out))
    print(f"kontakt: товаров {len(rows)} → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(build_parser().parse_args(argv)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", required=True, help="куда записать CSV")
    parser.add_argument("--profile", required=True, help="каталог профиля Chrome для сборщика")
    parser.add_argument("--headless", action="store_true", help="без окна (по умолчанию окно есть)")
    parser.add_argument(
        "--max-incomplete-share",
        type=float,
        default=DEFAULT_MAX_INCOMPLETE_SHARE,
        help="доля категорий, которые можно не дочитать",
    )
    parser.add_argument(
        "--human-wait-minutes",
        type=int,
        default=30,
        help="сколько ждать, пока владелец нажмёт галочку Cloudflare (0 — не ждать)",
    )
    parser.add_argument("--max-minutes", type=int, default=None, help="предел на весь обход, минут")
    parser.add_argument(
        "--category-limit", type=int, default=None, help="только первые N категорий (проба)"
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
