"""Excel-выгрузка раздела «Аптеки на Wolt» — отдельно от сравнения сайтов.

Запрос клиента (2026-11-20): выгрузка в Excel отдельно для Wolt и отдельно для
сайтов. Сайты выгружает `comparison_export` (страница «Сравнение»); здесь —
раздел `/wolt`: Pharmonline (сайт владельца, первая колонка) и аптеки Wolt.

Лист 1 — товары с разной ценой, лист 2 — все сравнимые товары, лист 3 — сводка:
когда выгружено, сколько товаров у каждой аптеки, когда она собрана, и по
каждой паре — общих товаров, у кого дешевле, у скольких цена одинаковая.

Строки приходят из `wolt.comparison` — те же, что видит страница; своей логики
цен здесь нет, только оформление. Оформление (шрифты, ячейки, защита от
формул в тексте с чужих сайтов) — общее с `comparison_export`.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from src.comparison_export import (
    _HEADER_ALIGN,
    _HEADER_FILL,
    _HEADER_FONT,
    _HIGH_FONT,
    _LINK_FONT,
    _LOW_FONT,
    _MONEY,
    _MUTED_FONT,
    _PERCENT,
    XLSX_MEDIA_TYPE,
    _write_text,
    baku_time,
)

__all__ = ["XLSX_MEDIA_TYPE", "build_workbook", "filename"]

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "filename": "wolt-sravnenie",
        "sheet_diff": "Разная цена",
        "sheet_all": "Все товары",
        "sheet_info": "Сводка",
        "name": "Товар",
        "price": "Цена: {venue}",
        "cheapest": "Дешевле всех",
        "diff_azn": "Разница, AZN",
        "diff_pct": "Разница, %",
        "link": "Ссылка: {venue}",
        "open": "открыть",
        "out_of_stock": "нет в наличии",
        "info_title": "Аптеки Wolt и Pharmonline — сравнение цен",
        "info_generated": "Выгружено",
        "info_search": "Поиск",
        "all": "все товары",
        "info_venue": "Аптека",
        "info_items": "Товаров",
        "info_updated": "Обновлено",
        "info_rows": "Товаров минимум в двух местах",
        "info_differ": "Из них с разной ценой",
        "info_everywhere": "Есть везде",
        "info_pairs": "По парам (товары в наличии у обеих)",
        "info_pairs_all": "По парам, по всем товарам без поиска (в наличии у обеих)",
        "pair": "Пара",
        "common": "Общих",
        "same": "Цена одинаковая",
        "cheaper_first": "Дешевле у первой",
        "cheaper_second": "Дешевле у второй",
        "info_rule": (
            "Один и тот же товар узнаётся по названию, дозировке и фасовке. Цена "
            "Pharmonline — с его сайта, цены аптек — с Wolt. Зелёным — самая "
            "низкая цена в строке, красным — самая высокая, серым — нет в "
            "наличии (в сравнение цен не идёт)."
        ),
    },
    "az": {
        "filename": "wolt-muqayise",
        "sheet_diff": "Fərqli qiymət",
        "sheet_all": "Bütün məhsullar",
        "sheet_info": "Xülasə",
        "name": "Məhsul",
        "price": "Qiymət: {venue}",
        "cheapest": "Ən ucuz",
        "diff_azn": "Fərq, AZN",
        "diff_pct": "Fərq, %",
        "link": "Keçid: {venue}",
        "open": "aç",
        "out_of_stock": "stokda yoxdur",
        "info_title": "Wolt aptekləri və Pharmonline — qiymət müqayisəsi",
        "info_generated": "Yüklənib",
        "info_search": "Axtarış",
        "all": "bütün məhsullar",
        "info_venue": "Aptek",
        "info_items": "Məhsul",
        "info_updated": "Yenilənib",
        "info_rows": "Ən azı iki yerdə olan məhsullar",
        "info_differ": "Onlardan qiyməti fərqli",
        "info_everywhere": "Hamısında var",
        "info_pairs": "Cütlər üzrə (hər ikisində stokda olan məhsullar)",
        "info_pairs_all": "Cütlər üzrə, axtarışsız bütün məhsullar (hər ikisində stokda)",
        "pair": "Cüt",
        "common": "Ümumi",
        "same": "Eyni qiymət",
        "cheaper_first": "Birincidə ucuzdur",
        "cheaper_second": "İkincidə ucuzdur",
        "info_rule": (
            "Eyni məhsul ad, doza və qablaşdırmaya görə tanınır. Pharmonline-ın "
            "qiyməti onun saytından, apteklərin qiymətləri Wolt-dan götürülür. "
            "Yaşıl — sətirdə ən aşağı qiymət, qırmızı — ən yüksək, boz — stokda "
            "yoxdur (qiymət müqayisəsinə daxil deyil)."
        ),
    },
    "en": {
        "filename": "wolt-comparison",
        "sheet_diff": "Different price",
        "sheet_all": "All products",
        "sheet_info": "Summary",
        "name": "Product",
        "price": "Price: {venue}",
        "cheapest": "Cheapest",
        "diff_azn": "Difference, AZN",
        "diff_pct": "Difference, %",
        "link": "Link: {venue}",
        "open": "open",
        "out_of_stock": "out of stock",
        "info_title": "Wolt pharmacies and Pharmonline — price comparison",
        "info_generated": "Exported",
        "info_search": "Search",
        "all": "all products",
        "info_venue": "Pharmacy",
        "info_items": "Products",
        "info_updated": "Updated",
        "info_rows": "Products in at least two places",
        "info_differ": "Of them with a different price",
        "info_everywhere": "Everywhere",
        "info_pairs": "By pair (products in stock at both)",
        "info_pairs_all": "By pair, all products regardless of the search (in stock at both)",
        "pair": "Pair",
        "common": "Shared",
        "same": "Same price",
        "cheaper_first": "Cheaper at the first",
        "cheaper_second": "Cheaper at the second",
        "info_rule": (
            "The same product is recognised by name, dose and pack size. "
            "Pharmonline's price comes from its site, the pharmacies' prices "
            "from Wolt. Green is the lowest price in the row, red the highest, "
            "grey is out of stock (not compared)."
        ),
    },
}


def strings(locale: str) -> dict[str, str]:
    return _STRINGS.get(locale, _STRINGS["az"])


def filename(locale: str, generated_at: datetime) -> str:
    return f"{strings(locale)['filename']}-{baku_time(generated_at):%Y-%m-%d}.xlsx"


def _header(ws: Any, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
    ws.row_dimensions[1].height = 32


def _write_rows(ws: Any, rows: list[dict], venues: list[dict], t: dict[str, str]) -> None:
    keys = [v["venue"] for v in venues]
    titles = {v["venue"]: v["title"] for v in venues}
    n = len(keys)
    headers = [
        t["name"],
        *(t["price"].format(venue=titles[k]) for k in keys),
        t["cheapest"],
        t["diff_azn"],
        t["diff_pct"],
        *(t["link"].format(venue=titles[k]) for k in keys),
    ]
    _header(ws, headers)
    price_col = {k: 2 + i for i, k in enumerate(keys)}
    cheapest_col, diff_col, pct_col = 2 + n, 3 + n, 4 + n
    link_col = {k: 5 + n + i for i, k in enumerate(keys)}
    for index, row in enumerate(rows, start=2):
        _write_text(ws.cell(row=index, column=1), row["name"])
        differs = row["min_price"] != row["max_price"]
        for key in keys:
            offer = row["prices"].get(key)
            if offer is None:
                continue
            cell = ws.cell(row=index, column=price_col[key], value=round(offer["price"], 2))
            cell.number_format = _MONEY
            if not offer["in_stock"]:
                cell.font = _MUTED_FONT
            elif differs and offer["price"] == row["min_price"]:
                cell.font = _LOW_FONT
            elif differs and offer["price"] == row["max_price"]:
                cell.font = _HIGH_FONT
            url = offer.get("url") or ""
            if url.startswith(("http://", "https://")) and not ILLEGAL_CHARACTERS_RE.search(url):
                link = ws.cell(
                    row=index,
                    column=link_col[key],
                    value=t["open"] if offer["in_stock"] else t["out_of_stock"],
                )
                link.hyperlink = url
                link.font = _LINK_FONT if offer["in_stock"] else _MUTED_FONT
        _write_text(
            ws.cell(row=index, column=cheapest_col),
            # В порядке колонок, а не по алфавиту ключей.
            ", ".join(titles[k] for k in keys if k in row["cheapest"]),
        )
        if differs:
            diff = ws.cell(
                row=index, column=diff_col, value=round(row["max_price"] - row["min_price"], 2)
            )
            diff.number_format = _MONEY
            pct = ws.cell(row=index, column=pct_col, value=row["spread_pct"] / 100)
            pct.number_format = _PERCENT
    widths = [52, *([14] * n), 26, 12, 10, *([12] * n)]
    for column, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows) + 1, 1)}"


def _write_summary(
    ws: Any, data: dict, t: dict[str, str], *, generated_at: datetime, search: str | None
) -> None:
    titles = {v["venue"]: v["title"] for v in data["venues"]}
    # Итоги — по строкам этого файла: с поиском они про найденное, а не про всё.
    rows = data["rows"]
    keys = [v["venue"] for v in data["venues"]]
    totals = {
        "rows": len(rows),
        "differ": sum(row["min_price"] != row["max_price"] for row in rows),
        "all_venues": sum(all(k in row["prices"] for k in keys) for row in rows),
    }
    line = 1

    def put(label: str, value: Any = "", *, bold: bool = False) -> None:
        nonlocal line
        _write_text(ws.cell(row=line, column=1), label)
        if isinstance(value, int | float) and not isinstance(value, bool):
            ws.cell(row=line, column=2, value=value)
        else:
            _write_text(ws.cell(row=line, column=2), value)
        if bold:
            ws.cell(row=line, column=1).font = Font(bold=True)
        line += 1

    put(t["info_title"])
    ws["A1"].font = Font(bold=True, size=13)
    put(t["info_generated"], f"{baku_time(generated_at):%d.%m.%Y %H:%M} (Bakı)")
    put(t["info_search"], search or t["all"])
    put(t["info_rows"], totals["rows"])
    put(t["info_differ"], totals["differ"])
    put(t["info_everywhere"], totals["all_venues"])
    line += 1

    for col, text in enumerate([t["info_venue"], t["info_items"], t["info_updated"]], start=1):
        cell = ws.cell(row=line, column=col)
        _write_text(cell, text)
        cell.font = Font(bold=True)
    line += 1
    for venue in data["venues"]:
        _write_text(ws.cell(row=line, column=1), venue["title"])
        ws.cell(row=line, column=2, value=venue["items"])
        if venue.get("last_ok_at"):
            moment = baku_time(datetime.fromisoformat(venue["last_ok_at"]))
            _write_text(ws.cell(row=line, column=3), f"{moment:%d.%m.%Y %H:%M}")
        line += 1
    line += 1

    put(t["info_pairs"] if not search else t["info_pairs_all"], bold=True)
    for col, text in enumerate(
        [t["pair"], t["common"], t["same"], t["cheaper_first"], t["cheaper_second"]], start=1
    ):
        cell = ws.cell(row=line, column=col)
        _write_text(cell, text)
        cell.font = Font(bold=True)
    line += 1
    for pair in data["pairs"]:
        a, b = titles.get(pair["a"], pair["a"]), titles.get(pair["b"], pair["b"])
        _write_text(ws.cell(row=line, column=1), f"{a} — {b}")
        ws.cell(row=line, column=2, value=pair["common"])
        ws.cell(row=line, column=3, value=pair["same_price"])
        ws.cell(row=line, column=4, value=pair["a_cheaper"])
        ws.cell(row=line, column=5, value=pair["b_cheaper"])
        line += 1
    line += 1
    _write_text(ws.cell(row=line, column=1), t["info_rule"])
    for column, width in zip("ABCDE", (60, 14, 18, 16, 16), strict=True):
        ws.column_dimensions[column].width = width


def build_workbook(
    data: dict,
    *,
    locale: str,
    generated_at: datetime,
    search: str | None = None,
) -> bytes:
    """Собрать .xlsx из ответа `wolt.comparison` (все строки, без `only_diff`).

    Первый лист — только строки с разной ценой, второй — все.
    """
    t = strings(locale)
    all_rows = data["rows"]
    differing = [row for row in all_rows if row["min_price"] != row["max_price"]]
    wb = Workbook()
    ws = wb.active
    ws.title = t["sheet_diff"]
    _write_rows(ws, differing, data["venues"], t)
    _write_rows(wb.create_sheet(t["sheet_all"]), all_rows, data["venues"], t)
    _write_summary(
        wb.create_sheet(t["sheet_info"]), data, t, generated_at=generated_at, search=search
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
