"""Каталог сайта из файла: чтение, отказ на ошибках, полный прогон экземпляром."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

from src import catalog_file, site_profile
from src.catalog_file import FILE_ROUTE, CatalogFileError, read_catalog_file

ROOT = Path(__file__).resolve().parent.parent

GOOD_CSV = """Məhsulun adı;Qiymət;Köhnə qiymət;Link;Kod;Kateqoriya;Stok
Samsung Galaxy A55 8/256GB;849,99;899,99;https://kontakt.az/samsung-galaxy-a55;TM-DG-1;Telefonlar;Stokda var
Apple iPhone 15 128GB;1 899 AZN;;https://kontakt.az/apple-iphone-15;TM-DG-2;Telefonlar;yoxdur
LG 55UR78006LK;;;https://kontakt.az/lg-55ur78006lk;TM-TV-3;TV;
"""


def _write(tmp_path: Path, text: str, name: str = "kontakt.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8-sig")
    return path


def test_csv_with_azerbaijani_headers_is_read_whole(tmp_path):
    catalog = read_catalog_file(_write(tmp_path, GOOD_CSV), "kontakt")
    assert catalog.rows_read == 3 and catalog.duplicate_rows == 0 and catalog.without_price == 1
    assert catalog.columns == {
        "name": "Məhsulun adı",
        "url": "Link",
        "price": "Qiymət",
        "old_price": "Köhnə qiymət",
        "sku": "Kod",
        "category": "Kateqoriya",
        "availability": "Stok",
    }
    samsung, iphone, lg = catalog.products
    assert (samsung.site, samsung.external_id, samsung.url) == (
        "kontakt",
        "TM-DG-1",
        "https://kontakt.az/samsung-galaxy-a55",
    )
    # Старая цена — обычная, текущая ниже неё — цена со скидкой.
    assert (samsung.price, samsung.discount_price, samsung.is_on_sale) == (899.99, 849.99, True)
    assert samsung.discount_percent == 5.6
    assert samsung.offer_availability_status == "in_stock"
    assert samsung.category == "Telefonlar"
    assert samsung.identity_verified is True
    assert (iphone.price, iphone.discount_price, iphone.is_on_sale) == (1899.0, None, False)
    assert iphone.offer_availability_status == "out_of_stock"
    # Пустая цена — товар без цены, а не ошибка; пустое наличие — «неизвестно».
    assert (lg.price, lg.offer_availability_status) == (None, "unknown")


def test_xlsx_with_russian_headers_and_no_sku_uses_the_page_address(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.append(["Ссылка", "Название", "Цена"])
    sheet.append(["https://kontakt.az/telefonlar/samsung-a55/", "Samsung  Galaxy\nA55", 849.99])
    sheet.append([None, None, None])
    sheet.append(["https://kontakt.az/lg-55", "LG 55", "1.299,50"])
    path = tmp_path / "kontakt.xlsx"
    book.save(path)

    catalog = read_catalog_file(path, "kontakt")
    assert [(p.external_id, p.name, p.price) for p in catalog.products] == [
        ("telefonlar/samsung-a55", "Samsung Galaxy A55", 849.99),
        ("lg-55", "LG 55", 1299.5),
    ]
    assert catalog.rows_read == 2
    assert all(product.category == FILE_ROUTE for product in catalog.products)


def test_identical_repeated_row_is_dropped_but_a_conflicting_one_refuses_the_file(tmp_path):
    header = "name,price,url\n"
    same = "A,10,https://kontakt.az/a\n"
    catalog = read_catalog_file(_write(tmp_path, header + same + same), "kontakt")
    assert len(catalog.products) == 1 and catalog.duplicate_rows == 1

    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(
            _write(tmp_path, header + same + "A,12,https://kontakt.az/a\n"), "kontakt"
        )
    assert "строка 3" in error.value.problems[0]


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (",10,https://kontakt.az/a", "нет названия или адреса"),
        ("A,10,", "нет названия или адреса"),
        ("A,10,kontakt.az/a", "адрес страницы не читается"),
        ("A,10,https://kontakt.az/", "адрес страницы не читается"),
        ("A,sorğu ilə,https://kontakt.az/a", "цена не читается"),
    ],
)
def test_one_bad_row_refuses_the_whole_file(tmp_path, row, problem):
    # Тихо пропущенная строка выглядела бы как товар, снятый с продажи.
    text = "name,price,url\nGood,5,https://kontakt.az/good\n" + row + "\n"
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(_write(tmp_path, text), "kontakt")
    assert len(error.value.problems) == 1
    assert problem in error.value.problems[0] and "строка 3" in error.value.problems[0]


def test_file_without_a_required_column_or_rows_is_refused(tmp_path):
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(_write(tmp_path, "name,url\nA,https://kontakt.az/a\n"), "kontakt")
    assert "price" in str(error.value)
    with pytest.raises(CatalogFileError):
        read_catalog_file(_write(tmp_path, "name,price,url\n"), "kontakt")
    with pytest.raises(CatalogFileError):
        read_catalog_file(tmp_path / "missing.csv", "kontakt")
    with pytest.raises(CatalogFileError):
        read_catalog_file(_write(tmp_path, "x", name="kontakt.pdf"), "kontakt")


def _one(tmp_path, header, row):
    catalog = read_catalog_file(_write(tmp_path, header + "\n" + row + "\n"), "kontakt")
    (product,) = catalog.products
    return product.price, product.discount_price, product.is_on_sale


def test_current_price_is_the_sale_price_when_the_file_names_one(tmp_path):
    header = "name;url;Qiymət;Endirimli qiymət;Köhnə qiymət"
    url = "https://kontakt.az/a"
    # Цена 1000, со скидкой 800: действующая 800, обычная 1000.
    assert _one(tmp_path, header, f"A;{url};1000;800;") == (1000.0, 800.0, True)
    # Скидочная пуста — действует обычная цена.
    assert _one(tmp_path, header, f"A;{url};1000;;") == (1000.0, None, False)
    # Старая цена ниже действующей — подорожание: действующей она не становится.
    assert _one(tmp_path, header, f"A;{url};1000;;900") == (1000.0, None, False)
    # Старая выше — скидка от неё.
    assert _one(tmp_path, header, f"A;{url};1000;;1200") == (1200.0, 1000.0, True)
    # Ноль и прочерк в необязательной цене — «не названа».
    assert _one(tmp_path, header, f"A;{url};1000;0;-") == (1000.0, None, False)


def test_empty_price_with_an_old_price_is_a_product_without_a_price(tmp_path):
    text = "name;url;price;old price\nA;https://kontakt.az/a;;900\nB;https://kontakt.az/b;5;\n"
    catalog = read_catalog_file(_write(tmp_path, text), "kontakt")
    assert [(p.name, p.price) for p in catalog.products] == [("A", None), ("B", 5.0)]


@pytest.mark.parametrize(
    ("header", "message"),
    [
        # Две колонки на одну роль: какая из них цена — не выбираем.
        ("name;url;price;Qiymət", "на одну роль (price)"),
        ("name;url;price;sale price;Yeni qiymət", "на одну роль (sale_price)"),
        # Незнакомая колонка с ценой молча не пропускается.
        ("name;url;price;Kredit qiyməti", "колонка с ценой не узнана"),
        ("name;url;price;Цена в рассрочку", "колонка с ценой не узнана"),
    ],
)
def test_ambiguous_or_unknown_price_column_refuses_the_file(tmp_path, header, message):
    row = ";".join(["A", "https://kontakt.az/a"] + ["10"] * (header.count(";") - 1))
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(_write(tmp_path, header + "\n" + row + "\n"), "kontakt")
    assert message in str(error.value)


def test_unknown_column_that_is_not_a_price_is_reported_not_refused(tmp_path):
    text = "name;url;price;Zəmanət;Rəng\nA;https://kontakt.az/a;10;2 il;Qara\n"
    catalog = read_catalog_file(_write(tmp_path, text), "kontakt")
    assert catalog.ignored_columns == ["Zəmanət", "Rəng"]


def test_values_the_database_cannot_hold_refuse_the_file(tmp_path):
    for row in (
        "A,10,https://kontakt.az/" + "x" * 250,
        "N" * 501 + ",10,https://kontakt.az/a",
    ):
        with pytest.raises(CatalogFileError) as error:
            read_catalog_file(_write(tmp_path, "name,price,url\n" + row + "\n"), "kontakt")
        assert "длиннее допустимого" in error.value.problems[0]


def test_sku_does_not_excuse_an_unreadable_address(tmp_path):
    for url in ("n/a", "kontakt.az/a"):
        text = f"name,price,url,sku\nA,10,{url},TM-1\n"
        with pytest.raises(CatalogFileError) as error:
            read_catalog_file(_write(tmp_path, text), "kontakt")
        assert "адрес страницы не читается" in error.value.problems[0]


def test_query_string_is_part_of_the_identity(tmp_path):
    text = "name,price,url\nA,10,https://kontakt.az/p?id=1\nA,10,https://kontakt.az/p?id=2\n"
    catalog = read_catalog_file(_write(tmp_path, text), "kontakt")
    assert [p.external_id for p in catalog.products] == ["p?id=1", "p?id=2"]
    assert catalog.duplicate_rows == 0


def test_file_without_a_single_price_is_not_a_price_catalog(tmp_path):
    text = "name,price,url\nA,,https://kontakt.az/a\nB,,https://kontakt.az/b\n"
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(_write(tmp_path, text), "kontakt")
    assert "нет цены" in str(error.value)


def test_unreadable_files_are_refused_with_a_reason_not_a_traceback(tmp_path):
    cp1251 = tmp_path / "excel.csv"
    cp1251.write_bytes("Название;Цена;Ссылка\nТелефон;10;https://kontakt.az/a\n".encode("cp1251"))
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(cp1251, "kontakt")
    assert "UTF-8" in str(error.value)

    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"this is not a spreadsheet")
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(broken, "kontakt")
    assert "не читается" in str(error.value)

    # Каталог на двух листах: первый лист — не весь каталог.
    book = Workbook()
    for sheet in (book.active, book.create_sheet("TV")):
        sheet.append(["name", "price", "url"])
        sheet.append([f"A {sheet.title}", 10, f"https://kontakt.az/{sheet.title}"])
    path = tmp_path / "two.xlsx"
    book.save(path)
    with pytest.raises(CatalogFileError) as error:
        read_catalog_file(path, "kontakt")
    assert "листов с данными 2" in str(error.value)


def test_file_scraper_reports_the_file_as_one_complete_route(tmp_path):
    catalog = read_catalog_file(_write(tmp_path, GOOD_CSV), "kontakt")
    scraper_class = catalog_file.file_scraper("kontakt", catalog)

    async def collect(route, limit=None):
        async with scraper_class() as scraper:
            products = [p async for p in scraper.scrape_category(route, limit)]
            return products, scraper._route_statuses[route]

    products, status = asyncio.run(collect(FILE_ROUTE))
    assert len(products) == 3
    assert (status.complete, status.expected_items, status.raw_items, status.parsed_items) == (
        True,
        3,
        3,
        3,
    )
    # Оборванное чтение и чужой раздел полным каталогом не считаются.
    _, status = asyncio.run(collect(FILE_ROUTE, limit=2))
    assert status.complete is False and status.expected_items == 3
    products, status = asyncio.run(collect("telefonlar"))
    assert products == [] and status.complete is False


def _cli(tmp_path: Path, profile: str | None, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != site_profile.PROFILE_ENV}
    if profile is not None:
        env[site_profile.PROFILE_ENV] = profile
    for name in ("SENTRY_DSN", "REDIS_URL", "SMTP_HOST", "TELEGRAM_BOT_TOKEN"):
        env.pop(name, None)
    env.update(
        DATABASE_URL=f"sqlite:///{tmp_path / 'instance.db'}",
        SCRAPE_REPORT_EMAIL="0",
        PYTHONPATH=str(ROOT),
    )
    return subprocess.run(
        [sys.executable, "-m", "src.main", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _db(tmp_path: Path, query: str) -> list[tuple]:
    with sqlite3.connect(tmp_path / "instance.db") as connection:
        return connection.execute(query).fetchall()


def test_electronics_instance_takes_the_client_catalog_from_a_file(tmp_path):
    path = _write(tmp_path, GOOD_CSV)

    preview = _cli(
        tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(path), "--dry-run"
    )
    assert preview.returncode == 0, preview.stderr[-1500:]
    assert "товаров 3" in preview.stdout and "ничего не записано" in preview.stdout
    assert not (tmp_path / "instance.db").exists()

    done = _cli(tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(path))
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    assert _db(
        tmp_path, "select status, catalog_scope, catalog_verified, products_scraped from runs"
    ) == [("ok", "full", 1, 3)]
    assert _db(tmp_path, "select site, external_id from products order by external_id") == [
        ("kontakt", "TM-DG-1"),
        ("kontakt", "TM-DG-2"),
        ("kontakt", "TM-TV-3"),
    ]
    assert _db(tmp_path, "select site, slug, is_active from site_routes") == [
        ("kontakt", FILE_ROUTE, 1)
    ]
    quality = json.loads(_db(tmp_path, "select run_quality from runs")[0][0])
    assert quality["financially_eligible"] is True

    # Новая выгрузка с изменённой ценой: тот же товар, новая запись о цене.
    snapshots_before = _db(tmp_path, "select count(*) from price_snapshots")[0][0]
    changed = _write(tmp_path, GOOD_CSV.replace("849,99", "829,99"), name="next.csv")
    done = _cli(tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(changed))
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    assert _db(tmp_path, "select count(*) from products")[0][0] == 3
    assert _db(tmp_path, "select count(*) from price_snapshots")[0][0] == snapshots_before + 1


def test_file_much_smaller_than_the_last_accepted_one_is_refused(tmp_path):
    # У файла нет внешнего знаменателя: мерка — прошлая принятая загрузка.
    rows = "".join(f"P{n};{n + 10};;https://kontakt.az/p{n};K{n};;\n" for n in range(20))
    header = GOOD_CSV.splitlines()[0] + "\n"
    full = _write(tmp_path, header + rows, name="full.csv")
    assert (
        _cli(
            tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(full)
        ).returncode
        == 0
    )

    # 17 из 20 — меньше девяти десятых: отказ до записи, прогон не заводится.
    part = _write(tmp_path, header + "".join(rows.splitlines(keepends=True)[:17]), name="part.csv")
    done = _cli(tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(part))
    assert done.returncode != 0
    assert "в прошлой принятой загрузке было 20" in done.stdout + done.stderr
    assert _db(tmp_path, "select count(*) from runs")[0][0] == 1

    # Настоящее сокращение — 12 из 20 — проходит только по явному слову и тогда
    # принимается целиком: прогон «ok», системе можно верить его ценам.
    small = _write(
        tmp_path, header + "".join(rows.splitlines(keepends=True)[:12]), name="small.csv"
    )
    args = ("catalog", "import", "--site", "kontakt", str(small))
    assert _cli(tmp_path, "electronics", *args).returncode != 0
    assert _db(tmp_path, "select count(*) from runs")[0][0] == 1
    done = _cli(tmp_path, "electronics", *args, "--allow-shrink", "--no-alerts")
    assert done.returncode == 0, (done.stdout + done.stderr)[-2000:]
    status, products, quality = _db(
        tmp_path, "select status, products_scraped, run_quality from runs order by id desc limit 1"
    )[0]
    assert (status, products) == ("ok", 12)
    assert json.loads(quality)["financially_eligible"] is True

    # Мерка теперь 12: файл в 11 товаров (больше девяти десятых) проходит сам.
    eleven = _write(tmp_path, header + "".join(rows.splitlines(keepends=True)[:11]), name="11.csv")
    assert (
        _cli(
            tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(eleven)
        ).returncode
        == 0
    )


def test_load_after_an_accepted_shrink_is_trusted(tmp_path):
    # Каталог сократился втрое по слову человека. Следующая обычная загрузка
    # того же размера обязана быть доверенной: мерка качества начинается с
    # сокращённого каталога, а не с медианы прежних, больших.
    header = GOOD_CSV.splitlines()[0] + "\n"
    rows = [f"P{n};{n + 10};;https://kontakt.az/p{n};K{n};;\n" for n in range(20)]
    base = ("electronics", "catalog", "import", "--site", "kontakt")
    full = _write(tmp_path, header + "".join(rows), name="full.csv")
    assert _cli(tmp_path, *base, str(full), "--no-alerts").returncode == 0
    small = _write(tmp_path, header + "".join(rows[:6]), name="small.csv")
    assert _cli(tmp_path, *base, str(small), "--allow-shrink", "--no-alerts").returncode == 0
    again = _write(tmp_path, header + "".join(rows[:6]).replace(";10;", ";11;"), name="again.csv")
    done = _cli(tmp_path, *base, str(again), "--no-alerts")
    assert done.returncode == 0, (done.stdout + done.stderr)[-2000:]

    runs = _db(tmp_path, "select status, products_scraped, run_quality from runs order by id")
    assert [(status, products) for status, products, _ in runs] == [
        ("ok", 20),
        ("ok", 6),
        ("ok", 6),
    ]
    assert [json.loads(quality)["financially_eligible"] for *_, quality in runs] == [
        True,
        True,
        True,
    ]


def test_refused_file_writes_nothing(tmp_path):
    bad = _write(tmp_path, GOOD_CSV + "Broken;abc;;https://kontakt.az/x;TM-X;;\n")
    done = _cli(tmp_path, "electronics", "catalog", "import", "--site", "kontakt", str(bad))
    assert done.returncode != 0
    assert "файл не принят" in done.stdout + done.stderr
    assert "строка 5" in done.stdout
    assert not (tmp_path / "instance.db").exists()


def test_pharmacy_instance_has_no_site_to_import(tmp_path):
    path = _write(tmp_path, GOOD_CSV)
    done = _cli(tmp_path, None, "catalog", "import", "--site", "kontakt", str(path))
    assert done.returncode != 0
    assert not (tmp_path / "instance.db").exists()
