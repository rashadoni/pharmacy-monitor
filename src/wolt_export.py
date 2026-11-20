"""Excel и числа сравнения аптек Wolt — для утреннего письма.

Решение владельца 2026-11-20: сравнение аптек Wolt приходит тем же утренним
письмом, что и разница цен сайтов, — одним письмом, вторым файлом во
вложении. Строки — те же, что показывает раздел `/wolt` без фильтров
(`wolt.comparison`); здесь нет своей логики цен, только оформление и подсчёт.

Колонки — `wolt.COLUMNS`: первым владелец (цены его сайта), за ним аптеки
Wolt. Сравниваются цены того, что можно купить: товар «нет в наличии» в
строке остаётся, но в разницу, «дешевле всего» и позицию владельца не идёт —
то же правило, что у страницы.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from src import comparison_export, wolt
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
    _write_text,
    baku_time,
)

_STRINGS: dict[str, dict[str, str]] = {
    "az": {
        "note_discount": "{venue}: endirimli qiymət, endirimsiz {price}",
        "info_title": "Wolt-dakı apteklər — qiymət müqayisəsi",
        "info_owner": "{owner} — {owner} saytındakı qiymətlər (Wolt-da öz apteki yoxdur)",
        "info_venue": "{venue}: kataloq yığılıb",
        "info_rule": "«Fərqli qiymətlər» vərəqi: stokda olan məhsulların qiyməti eyni olmayan "
        "bütün məhsullar, ən böyük fərqdən başlayaraq. Eyni məhsul adına, dozasına və "
        "qablaşdırmasına görə tanınır; qiymətləri üç dəfədən çox fərqlənən sətirlər "
        "müqayisəyə daxil deyil.",
        "filename": "wolt-aptekleri",
    },
    "ru": {
        "note_discount": "{venue}: цена со скидкой, без скидки {price}",
        "info_title": "Аптеки на Wolt — сравнение цен",
        "info_owner": "{owner} — цены сайта {owner} (своей аптеки на Wolt нет)",
        "info_venue": "{venue}: каталог собран",
        "info_rule": "Лист «Разные цены»: все товары, у которых цена товара в наличии не "
        "совпадает, начиная с самой большой разницы. Одинаковые товары узнаются по названию, "
        "дозе и фасовке; строки, где цены расходятся больше чем втрое, в сравнение не входят.",
        "filename": "wolt-apteki",
    },
    "en": {
        "note_discount": "{venue}: discounted price, {price} without the discount",
        "info_title": "Pharmacies on Wolt — price comparison",
        "info_owner": "{owner} — prices from the {owner} website (no own pharmacy on Wolt)",
        "info_venue": "{venue}: catalogue collected",
        "info_rule": "Sheet “Price differences”: every product whose in-stock price is not the "
        "same, largest difference first. Products are matched by name, dose and pack size; "
        "rows whose prices differ more than threefold are left out.",
        "filename": "wolt-pharmacies",
    },
}


def strings(locale: str) -> dict[str, str]:
    """Подписи файла: общие со страницей сравнения плюс свои."""
    return {**comparison_export.strings(locale), **_STRINGS.get(locale, _STRINGS["az"])}


def column_title(column: str) -> str:
    return wolt.OWNER_TITLE if column == wolt.OWNER else wolt.VENUES[column]


def filename(locale: str, generated_at: datetime) -> str:
    return f"{strings(locale)['filename']}-{baku_time(generated_at):%Y-%m-%d}.xlsx"


def _in_stock(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in row["prices"].values() if entry.get("in_stock")]


def has_price_difference(row: dict[str, Any]) -> bool:
    """Цены товаров в наличии не равны (минимум и максимум — только по ним)."""
    return len(_in_stock(row)) >= 2 and row["min_price"] != row["max_price"]


def owner_position(row: dict[str, Any]) -> str | None:
    """Где цена владельца среди цен товаров в наличии.

    `equal` — цены равны; `cheapest` — у владельца самая низкая, в том числе
    поровну с кем-то; `priciest` — самая высокая; `middle` — между ними. None —
    цена владельца в сравнение не вошла (его товара в строке нет или нет в
    наличии) либо сравнивать не с чем.
    """
    entry = row["prices"].get(wolt.OWNER)
    if entry is None or not entry.get("in_stock") or len(_in_stock(row)) < 2:
        return None
    if row["min_price"] == row["max_price"]:
        return "equal"
    if entry["price"] == row["min_price"]:
        return "cheapest"
    if entry["price"] == row["max_price"]:
        return "priciest"
    return "middle"


def difference_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Числа раздела Wolt в утреннем письме — те же, что видны в файле.

    - `compared` — строк в сравнении, лист «Всё сравнение»;
    - `differing` — из них с разной ценой, лист «Разные цены»;
    - `rival_cheaper` — из `differing`: цена владельца выше минимальной, то
      есть хотя бы одна аптека Wolt дешевле;
    - `owner_cheapest` — из `differing`: у владельца самая низкая, в том числе
      поровну с кем-то;
    - `no_owner_price` — из `differing`: владельца в сравнении нет — товара нет,
      нет в наличии или его цена втрое и больше расходится с аптеками Wolt
      (`wolt._build_rows` убирает её из строки как, скорее всего, другой
      товар), — различаются цены аптек Wolt между собой;
    - `same_price` — цены товаров в наличии равны;
    - `not_comparable` — в наличии меньше чем в двух местах.

    `compared` = `differing` + `same_price` + `not_comparable`, `differing` —
    сумма трёх своих частей.
    """
    counts = dict.fromkeys(
        (
            "compared",
            "differing",
            "rival_cheaper",
            "owner_cheapest",
            "no_owner_price",
            "same_price",
            "not_comparable",
        ),
        0,
    )
    for row in rows:
        counts["compared"] += 1
        if has_price_difference(row):
            counts["differing"] += 1
            position = owner_position(row)
            if position is None:
                counts["no_owner_price"] += 1
            elif position == "cheapest":
                counts["owner_cheapest"] += 1
            else:
                counts["rival_cheaper"] += 1
        elif len(_in_stock(row)) < 2:
            counts["not_comparable"] += 1
        else:
            counts["same_price"] += 1
    return counts


def _note(row: dict[str, Any], t: dict[str, str]) -> str:
    notes: list[str] = []
    for column in wolt.COLUMNS:
        entry = row["prices"].get(column)
        if entry is None:
            continue
        if not entry.get("in_stock"):
            notes.append(t["note_out_of_stock"].format(site=column_title(column)))
        elif entry.get("original_price"):
            notes.append(
                t["note_discount"].format(
                    venue=column_title(column), price=f"{entry['original_price']:.2f}"
                )
            )
    return ". ".join(notes)


def _write_sheet(ws: Any, rows: list[dict[str, Any]], t: dict[str, str]) -> None:
    columns = wolt.COLUMNS
    headers = [
        t["name"],
        *(t["price"].format(site=column_title(c)) for c in columns),
        t["cheapest"],
        t["diff_azn"],
        t["diff_pct"],
        t["position"].format(client=column_title(wolt.OWNER)),
        t["note"],
        *(t["link"].format(site=column_title(c)) for c in columns),
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
    ws.row_dimensions[1].height = 32

    price_col = {column: 2 + i for i, column in enumerate(columns)}
    after = 2 + len(columns)
    link_col = {column: after + 5 + i for i, column in enumerate(columns)}
    for index, row in enumerate(rows, start=2):
        _write_text(ws.cell(row=index, column=1), row.get("name"))
        differs = has_price_difference(row)
        for column in columns:
            entry = row["prices"].get(column)
            if entry is None:
                continue
            cell = ws.cell(row=index, column=price_col[column], value=round(entry["price"], 2))
            cell.number_format = _MONEY
            if not entry.get("in_stock"):
                cell.font = _MUTED_FONT
            elif differs and entry["price"] == row["min_price"]:
                cell.font = _LOW_FONT
            elif differs and entry["price"] == row["max_price"]:
                cell.font = _HIGH_FONT
            url = entry.get("url") or ""
            if url.startswith(("http://", "https://")) and not ILLEGAL_CHARACTERS_RE.search(url):
                link = ws.cell(row=index, column=link_col[column], value=column_title(column))
                link.hyperlink = url
                link.font = _LINK_FONT
        if differs:
            _write_text(
                ws.cell(row=index, column=after),
                ", ".join(column_title(c) for c in columns if c in row.get("cheapest", [])),
            )
            diff = ws.cell(
                row=index, column=after + 1, value=round(row["max_price"] - row["min_price"], 2)
            )
            diff.number_format = _MONEY
            pct = ws.cell(row=index, column=after + 2, value=(row.get("spread_pct") or 0.0) / 100)
            pct.number_format = _PERCENT
        position = owner_position(row)
        _write_text(ws.cell(row=index, column=after + 3), t[f"pos_{position}"] if position else "")
        _write_text(ws.cell(row=index, column=after + 4), _note(row, t))

    widths = [52, *[14] * len(columns), 24, 12, 10, 20, 60, *[16] * len(columns)]
    for column, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows) + 1, 1)}"


def build_workbook(
    rows: list[dict[str, Any]],
    *,
    locale: str,
    generated_at: datetime,
    collected_at: dict[str, datetime | None],
) -> bytes:
    """Собрать .xlsx из строк `wolt.comparison`.

    Лист 1 — товары с разной ценой, лист 2 — всё сравнение, лист 3 — когда
    собран каждый каталог Wolt (`collected_at`: аптека → конец последнего
    удачного сбора, UTC без пояса) и как читать файл.
    """
    t = strings(locale)
    differing = [row for row in rows if has_price_difference(row)]

    wb = Workbook()
    ws_diff = wb.active
    ws_diff.title = t["sheet_diff"]
    _write_sheet(ws_diff, differing, t)
    _write_sheet(wb.create_sheet(t["sheet_all"]), rows, t)

    info = wb.create_sheet(t["sheet_info"])
    lines: list[tuple[str, Any]] = [
        (t["info_title"], ""),
        (t["info_generated"], f"{baku_time(generated_at):%d.%m.%Y %H:%M} (Bakı)"),
        (t["info_owner"].format(owner=column_title(wolt.OWNER)), ""),
    ]
    for venue in wolt.VENUES:
        moment = collected_at.get(venue)
        lines.append(
            (
                t["info_venue"].format(venue=column_title(venue)),
                f"{baku_time(moment):%d.%m.%Y %H:%M} (Bakı)" if moment else "—",
            )
        )
    lines += [
        (t["info_diff_rows"], len(differing)),
        (t["info_all_rows"], len(rows)),
        ("", ""),
        (t["info_rule"], ""),
        (t["info_colors"], ""),
    ]
    for index, (label, value) in enumerate(lines, start=1):
        _write_text(info.cell(row=index, column=1), label)
        if isinstance(value, int):
            info.cell(row=index, column=2, value=value)
        else:
            _write_text(info.cell(row=index, column=2), value)
    info["A1"].font = Font(bold=True, size=13)
    info.column_dimensions["A"].width = 52
    info.column_dimensions["B"].width = 28

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
