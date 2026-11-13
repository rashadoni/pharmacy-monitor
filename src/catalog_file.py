"""Каталог сайта из файла: тот же вход в прогон, что у сборщика.

Нужен сайту, который сборщиком не читается (профиль называет такие сайты в
``imported_sites``): каталог с ценами выгружает человек, а дальше файл идёт
обычным путём — запись, проверка полноты, сопоставление, алерты, рекомендации.
Файл читает ``FileCatalogScraper``, и для прогона он ничем не отличается от
сборщика с одним разделом.

Формат — CSV или XLSX с заголовком. Колонки узнаются по названию (на
азербайджанском, русском или английском), порядок любой:

- название и адрес страницы товара — обязательны;
- цена — обязательна как колонка; пустая у строки — товар без цены;
- старая цена, артикул, категория, наличие — если есть.

Товар узнаётся по артикулу, а если его нет — по адресу страницы. Файл либо
принят целиком, либо отвергнут: строка без названия или адреса, цена, которую
не удалось прочитать, и два разных товара под одним идентификатором — отказ
всего файла со списком причин. Тихо пропущенная строка выглядела бы как товар,
снятый с продажи.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo
from src.scrapers.http_catalog import price_or_none, sale_fields

# Единственный «раздел» сайта, чей каталог приходит файлом.
FILE_ROUTE = "catalog-file"

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "title",
        "productname",
        "product",
        "ad",
        "adi",
        "mehsul",
        "mehsulunadi",
        "название",
        "наименование",
        "товар",
    ),
    "url": ("url", "link", "href", "producturl", "productlink", "kecid", "ссылка", "адрес"),
    "price": (
        "price",
        "finalprice",
        "currentprice",
        "saleprice",
        "qiymet",
        "qiymeti",
        "yeniqiymet",
        "цена",
        "ценасоскидкой",
        "новаяцена",
    ),
    "old_price": (
        "oldprice",
        "regularprice",
        "listprice",
        "originalprice",
        "kohneqiymet",
        "evvelkiqiymet",
        "стараяцена",
        "ценабезскидки",
    ),
    "sku": ("sku", "code", "productcode", "kod", "mehsulkodu", "артикул", "код", "кодтовара"),
    "category": ("category", "kateqoriya", "bolme", "категория", "раздел"),
    "availability": ("availability", "stock", "instock", "movcudluq", "stok", "наличие"),
}
_REQUIRED_COLUMNS = ("name", "url", "price")
_IN_STOCK = {
    "1",
    "true",
    "yes",
    "var",
    "stokdavar",
    "movcuddur",
    "да",
    "есть",
    "вналичии",
    "instock",
}
_OUT_OF_STOCK = {
    "0",
    "false",
    "no",
    "yoxdur",
    "stokdayoxdur",
    "bitib",
    "нет",
    "нетвналичии",
    "outofstock",
}
_MAX_PROBLEMS_KEPT = 50


class CatalogFileError(ValueError):
    """Файл каталога нельзя принять; ``problems`` — почему, построчно."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = list(problems or [])


@dataclass
class CatalogFile:
    path: Path
    rows_read: int
    products: list[ScrapedProduct]
    duplicate_rows: int = 0
    without_price: int = 0
    columns: dict[str, str] = field(default_factory=dict)


def _key(text: object) -> str:
    """Название колонки или значение без регистра, пробелов и знаков."""
    folded = str(text or "").strip().lower().translate(str.maketrans("əıöüğşç", "eiougsc"))
    return re.sub(r"[^0-9a-zа-яё]+", "", folded)


def _table(path: Path) -> list[list[object]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        sheet = load_workbook(path, read_only=True, data_only=True).worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    if suffix in {".csv", ".tsv", ".txt"}:
        text = path.read_bytes().decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [list(row) for row in csv.reader(io.StringIO(text), dialect)]
    raise CatalogFileError(f"{path.name}: формат не поддерживается — нужен CSV или XLSX")


def _external_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    tail = parts.path.strip("/")
    return tail or None


def read_catalog_file(path: str | Path, site: str) -> CatalogFile:
    """Прочитать файл каталога целиком; не принят — ``CatalogFileError``."""
    path = Path(path)
    if not path.is_file():
        raise CatalogFileError(f"{path}: файл не найден")
    table = [row for row in _table(path) if any(str(cell or "").strip() for cell in row)]
    if len(table) < 2:
        raise CatalogFileError(f"{path.name}: в файле нет строк с товарами")

    header = [_key(cell) for cell in table[0]]
    columns: dict[str, int] = {}
    for column, aliases in _COLUMN_ALIASES.items():
        for position, name in enumerate(header):
            if name in aliases:
                columns[column] = position
                break
    missing = [column for column in _REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise CatalogFileError(
            f"{path.name}: не найдены колонки: {', '.join(missing)}. "
            f"В заголовке: {', '.join(str(cell) for cell in table[0])}"
        )

    def cell(row: list[object], column: str) -> str:
        position = columns.get(column)
        if position is None or position >= len(row) or row[position] is None:
            return ""
        return " ".join(str(row[position]).split())

    problems: list[str] = []
    by_id: dict[str, tuple[ScrapedProduct, int]] = {}
    duplicate_rows = 0
    for number, row in enumerate(table[1:], start=2):
        name, url = cell(row, "name"), cell(row, "url")
        if not name or not url:
            problems.append(f"строка {number}: нет названия или адреса страницы")
            continue
        external_id = cell(row, "sku") or _external_id_from_url(url)
        if not external_id:
            problems.append(f"строка {number}: адрес страницы не читается: {url[:80]}")
            continue

        raw_price, raw_old = cell(row, "price"), cell(row, "old_price")
        final = price_or_none(raw_price) if raw_price else None
        regular = price_or_none(raw_old) if raw_old else None
        if (raw_price and final is None) or (raw_old and regular is None):
            problems.append(f"строка {number}: цена не читается: {(raw_price or raw_old)[:40]}")
            continue
        price, discount_price, discount_percent, on_sale = sale_fields(regular, final)

        stock = _key(cell(row, "availability"))
        availability = (
            "in_stock"
            if stock in _IN_STOCK
            else "out_of_stock"
            if stock in _OUT_OF_STOCK
            else "unknown"
        )
        product = ScrapedProduct(
            site=site,
            external_id=external_id,
            url=url,
            name=name,
            identity_verified=True,
            offer_availability_status=availability,
            availability_source=f"{site}_catalog_file",
            category=cell(row, "category") or FILE_ROUTE,
            price=price,
            discount_price=discount_price,
            discount_percent=discount_percent,
            is_on_sale=on_sale,
        )
        earlier = by_id.get(external_id)
        if earlier is None:
            by_id[external_id] = (product, number)
            continue
        first, first_number = earlier
        if (first.name, first.price, first.discount_price) == (name, price, discount_price):
            duplicate_rows += 1
            continue
        problems.append(
            f"строка {number}: тот же идентификатор {external_id[:60]!r}, что в строке "
            f"{first_number}, а название или цена другие"
        )

    if problems:
        shown = problems[:_MAX_PROBLEMS_KEPT]
        raise CatalogFileError(
            f"{path.name}: файл не принят, строк с ошибками: {len(problems)}", shown
        )
    products = [product for product, _ in by_id.values()]
    return CatalogFile(
        path=path,
        rows_read=len(table) - 1,
        products=products,
        duplicate_rows=duplicate_rows,
        without_price=sum(1 for product in products if product.price is None),
        columns={column: str(table[0][position]) for column, position in columns.items()},
    )


def file_scraper(site: str, catalog: CatalogFile) -> type[BaseScraper]:
    """Сборщик, который «собирает» сайт из уже прочитанного файла."""

    class FileCatalogScraper(BaseScraper):
        site_name = site
        base_url = ""

        def __init__(self, **_kwargs):
            super().__init__()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape_category(self, category_slug: str, limit: int | None = None):
            products = catalog.products if category_slug == FILE_ROUTE else []
            emitted = 0
            for product in products:
                if limit is not None and emitted >= limit:
                    break
                emitted += 1
                yield product
            self._set_route_status(
                category_slug,
                complete=category_slug == FILE_ROUTE and emitted == len(products),
                abort_reason=None if category_slug == FILE_ROUTE else "not_the_catalog_file_route",
                raw_items=emitted,
                parsed_items=emitted,
                expected_items=len(products),
            )

        async def scrape_promos(self) -> list[ScrapedPromo]:
            return []

    return FileCatalogScraper
