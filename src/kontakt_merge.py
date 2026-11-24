"""Склеить выгрузки каталога Kontakt Home в один файл для `catalog import`.

Kontakt Home сборщиком не читается: Cloudflare не пускает ни сервер, ни Chrome
под управлением программы (2026-11-24: галочка «Verify you are human»
повторялась после каждого клика владельца). Каталог собирает владелец руками
в своём Chrome — расширением Instant Data Scraper по разделам сайта, как
13 ноября 2026, — а этот модуль склеивает выгрузки в один CSV:

    python -m src.kontakt_merge ПАПКА_С_XLSX --out kontakt.csv

Понимает два вида выгрузок (оба встречались 13 ноября):
  - «Название, Цена, Ссылка, Прежняя цена»;
  - колонки карточки сайта: ``product-card__title``, ``product-card__price``,
    ``product-card__image-link href``, ``product-card__old-price``.
Раздел товара берётся из ``catalog-status.csv`` в той же папке (колонки
«Раздел» и «Файл»), если он есть. Читается только сама папка, без подпапок:
в подпапке «отправлено» лежат прошлые выгрузки.

Колонки итога — те же, что ждёт приём на сервере
(``infra/electronics/kontakt-receive.sh``): name, url, price, sale_price,
category, availability. Артикула нет: товар узнаётся по адресу страницы, как в
первой загрузке, — артикул завёл бы весь каталог вторыми строками. Товар,
встреченный в нескольких выгрузках, берётся один раз (первый по имени файла).

Модуль нарочно не тянет зависимостей проекта, кроме openpyxl: на Маке ему
хватает маленького окружения.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = ("name", "url", "price", "sale_price", "category", "availability")
# Длина колонки ``products.category``; длиннее — сервер отверг бы файл целиком.
_CATEGORY_MAX = 100
_SITE = "https://kontakt.az/"
# Длины колонок `products`: идентификатор — путь адреса (200), адрес — 500.
_URL_MAX = len(_SITE) + 200


@dataclass
class MergeResult:
    rows: list[dict[str, str]]
    files: int = 0
    stats: Counter = field(default_factory=Counter)
    price_conflicts: list[tuple[str, str, str]] = field(default_factory=list)


def money(value: object) -> str | None:
    """Цена из ячейки выгрузки («1 899.99 ₼», 1899.99) или ``None``."""
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").replace("₼", "").replace("AZN", "")
    text = text.replace(" ", "").strip()
    if "," in text and "." in text:
        # Оба знака: дробный — тот, что правее («2,199.00» и «1.899,99»).
        thousands = "," if text.rfind(".") > text.rfind(",") else "."
        text = text.replace(thousands, "")
    text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    amount = float(text)
    return f"{amount:.2f}" if amount > 0 else None


def product_url(value: object) -> str | None:
    """Адрес страницы товара kontakt.az без строки запроса; иначе ``None``."""
    url = str(value or "").strip()
    if not url.startswith(_SITE):
        return None
    url = url.split("?")[0].split("#")[0].rstrip("/")
    return url if url != _SITE.rstrip("/") else None


def _sections(folder: Path) -> dict[str, str]:
    """Файл выгрузки → раздел сайта, из ``catalog-status.csv``."""
    sections: dict[str, str] = {}
    # Только сама папка: в её подпапке «отправлено» лежат прошлые выгрузки.
    for status in sorted(folder.glob("catalog-status.csv")):
        with status.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                name = (row.get("Файл") or "").strip()
                if name:
                    sections.setdefault(name, (row.get("Раздел") or "").strip())
    return sections


def _columns(head: list[str]) -> dict[str, int | None] | None:
    if head[:3] == ["Название", "Цена", "Ссылка"]:
        return {"name": 0, "price": 1, "url": 2, "old": 3 if len(head) > 3 else None}
    if "product-card__title" in head and "product-card__price" in head:
        link = next(
            (
                head.index(title)
                for title in ("product-card__title href", "product-card__image-link href")
                if title in head
            ),
            None,
        )
        if link is None:
            return None
        old = head.index("product-card__old-price") if "product-card__old-price" in head else None
        return {
            "name": head.index("product-card__title"),
            "price": head.index("product-card__price"),
            "url": link,
            "old": old,
        }
    return None


def merge(folder: str | Path) -> MergeResult:
    from openpyxl import load_workbook

    folder = Path(folder)
    sections = _sections(folder)
    result = MergeResult(rows=[])
    by_url: dict[str, dict[str, str]] = {}
    # Только верхний уровень папки: прошлые выгрузки уезжают в «отправлено/…»
    # внутри неё и не должны склеиваться заново.
    for path in sorted(folder.glob("*.xlsx")):
        if path.name.startswith(("~$", ".")):
            continue  # служебный файл открытого Excel
        result.files += 1
        book = load_workbook(path, read_only=True, data_only=True)
        rows = list(book.worksheets[0].iter_rows(values_only=True))
        book.close()
        rows = iter(rows)
        head = [str(cell or "").strip() for cell in next(rows, ())]
        col = _columns(head)
        if col is None:
            result.stats["file_unknown_format"] += 1
            continue
        category = sections.get(path.name, "")[:_CATEGORY_MAX]
        for raw in rows:
            if not any(cell not in (None, "") for cell in raw):
                continue
            result.stats["rows"] += 1

            def cell(key: str, raw=raw) -> object:
                position = col[key]
                return raw[position] if position is not None and position < len(raw) else None

            url = product_url(cell("url"))
            name = " ".join(str(cell("name") or "").split())
            current = money(cell("price"))
            old = money(cell("old"))
            if not url:
                result.stats["no_url"] += 1
                continue
            if len(url) > _URL_MAX:
                result.stats["url_too_long"] += 1
                continue
            if not name:
                result.stats["no_name"] += 1
                continue
            if not current:
                result.stats["no_price"] += 1
                continue
            sale = old is not None and float(old) > float(current)
            row = {
                "name": name[:500],
                "url": url,
                # price — обычная цена, sale_price — цена со скидкой.
                "price": old if sale else current,
                "sale_price": current if sale else "",
                "category": category,
                "availability": "",
            }
            earlier = by_url.get(url)
            if earlier is not None:
                result.stats["duplicate"] += 1
                if (earlier["price"], earlier["sale_price"]) != (row["price"], row["sale_price"]):
                    result.price_conflicts.append(
                        (url, earlier["sale_price"] or earlier["price"], current)
                    )
                if not earlier["category"] and category:
                    earlier["category"] = category
                continue
            by_url[url] = row
    result.rows = list(by_url.values())
    return result


def write_catalog(rows: list[dict[str, str]], out: Path) -> None:
    """Записать файл атомарно: недописанный файл не должен уйти на сервер."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("folder", help="папка с выгрузками xlsx")
    parser.add_argument("--out", required=True, help="куда записать CSV")
    args = parser.parse_args(argv)
    if not Path(args.folder).is_dir():
        print(f"kontakt: нет папки {args.folder}", file=sys.stderr)
        return 2
    result = merge(args.folder)
    stats = dict(result.stats)
    print(
        f"kontakt: файлов {result.files}, строк {stats.get('rows', 0)}, товаров {len(result.rows)}"
    )
    skipped = {k: v for k, v in stats.items() if k != "rows" and v}
    if skipped:
        print(f"kontakt: пропущено/повторы: {skipped}")
    if result.price_conflicts:
        print(
            f"kontakt: у {len(result.price_conflicts)} товаров разная цена в разных файлах — взята первая"
        )
    if not result.rows:
        print("kontakt: ни одного товара — файл не записан", file=sys.stderr)
        return 3
    write_catalog(result.rows, Path(args.out))
    print(f"kontakt: записано → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
