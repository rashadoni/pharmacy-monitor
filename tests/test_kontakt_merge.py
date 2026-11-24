"""Склейка выгрузок Kontakt Home (`src/kontakt_merge.py`) и отправка с Мака
(`infra/local/kontakt/upload.sh`).

Выгрузки строятся здесь же в двух видах, которые дал Instant Data Scraper
13 ноября 2026. Главное, что проверяется: файл принимает сервер
(`catalog_file`) и узнаёт товар по адресу страницы, как в первой загрузке.
На настоящих выгрузках 13 ноября (628 файлов, вне репозитория) склейка дала
те же 15 674 товара с теми же идентификаторами, ценами и разделами, что
прежний сценарий `merge_kontakt.py`.
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook

from src import catalog_file, kontakt_merge

ROOT = Path(__file__).resolve().parents[1]


def _xlsx(path: Path, rows: list[list[object]]) -> None:
    book = Workbook()
    for row in rows:
        book.active.append(row)
    book.save(path)


def test_both_export_kinds_merge_into_a_file_the_server_reads(tmp_path):
    folder = tmp_path / "Kontakt"
    folder.mkdir()
    _xlsx(
        folder / "kontakt (1).xlsx",
        [
            ["Название", "Цена", "Ссылка", "Прежняя цена"],
            ["Notbuk Acer Nitro", "1899.99 ₼", "https://kontakt.az/notbuk-acer-nitro", "2899.99 ₼"],
            ["Mikrofon Proel", "299.99 ₼", "https://kontakt.az/mikrofon-proel?x=1", None],
            ["Kartinka", "10 ₼", "https://api.kontakt.az/media/x.webp", None],
            ["Bez ceny", None, "https://kontakt.az/bez-ceny", None],
        ],
    )
    _xlsx(
        folder / "Chrome 04.03.xlsx",
        [
            [
                "product-card__image-link href",
                "product-card__title",
                "product-card__old-price",
                "product-card__price",
            ],
            [
                "https://kontakt.az/notbuk-acer-nitro",
                "Notbuk Acer Nitro",
                "2 899.99 ₼",
                "1 899.99 ₼",
            ],
            ["https://kontakt.az/disk-ps4/", "Disk PlayStation 4", None, "14,99 ₼"],
        ],
    )
    (folder / "catalog-status.csv").write_text(
        "Раздел;Подраздел;Строк в XLSX;Файл;Статус\n"
        "Gamer zona;Konsollar;2;Chrome 04.03.xlsx;Chrome\n"
        "Notbuklar;Acer;4;kontakt (1).xlsx;Instant Data Scraper\n",
        encoding="utf-8-sig",
    )
    _xlsx(folder / "~$open.xlsx", [["junk"]])  # служебный файл Excel

    result = kontakt_merge.merge(folder)
    assert result.files == 2
    assert result.stats["no_url"] == 1 and result.stats["no_price"] == 1
    assert result.stats["duplicate"] == 1 and result.price_conflicts == []
    out = tmp_path / "kontakt.csv"
    kontakt_merge.write_catalog(result.rows, out)

    with out.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == list(kontakt_merge.COLUMNS)
    catalog = catalog_file.read_catalog_file(out, "kontakt")
    by_id = {product.external_id: product for product in catalog.products}
    # Идентификатор — адрес страницы без строки запроса и косой черты в конце.
    assert set(by_id) == {"notbuk-acer-nitro", "mikrofon-proel", "disk-ps4"}
    acer = by_id["notbuk-acer-nitro"]
    assert (acer.price, acer.discount_price, acer.is_on_sale) == (2899.99, 1899.99, True)
    assert by_id["mikrofon-proel"].price == 299.99 and not by_id["mikrofon-proel"].is_on_sale
    assert by_id["disk-ps4"].price == 14.99
    # Товар из двух файлов берётся из первого по имени файла.
    assert acer.category == "Gamer zona"
    assert by_id["disk-ps4"].category == "Gamer zona"


def test_archived_exports_are_not_merged_again(tmp_path):
    folder = tmp_path / "Kontakt"
    sent = folder / "отправлено" / "2026-11-24_1000"
    sent.mkdir(parents=True)
    _xlsx(
        sent / "телефоны.xlsx",
        [["Название", "Цена", "Ссылка"], ["Old", "1 ₼", "https://kontakt.az/old-phone"]],
    )
    _xlsx(
        folder / "new.xlsx",
        [["Название", "Цена", "Ссылка"], ["New", "2 ₼", "https://kontakt.az/new-phone"]],
    )
    (sent / "catalog-status.csv").write_text(
        "Раздел;Файл\nСтарый раздел;new.xlsx\n", encoding="utf-8-sig"
    )
    rows = kontakt_merge.merge(folder).rows
    assert [row["url"] for row in rows] == ["https://kontakt.az/new-phone"]
    assert rows[0]["category"] == ""  # раздел из архива не подхвачен


def test_a_folder_without_products_writes_nothing(tmp_path):
    folder = tmp_path / "Kontakt"
    folder.mkdir()
    _xlsx(folder / "x.xlsx", [["что-то", "другое"], [1, 2]])
    out = tmp_path / "k.csv"
    assert kontakt_merge.main([str(folder), "--out", str(out)]) == 3
    assert not out.exists()


def test_money_reads_the_forms_the_exports_use():
    assert kontakt_merge.money("1 899.99 ₼") == "1899.99"
    assert kontakt_merge.money("14,99 ₼") == "14.99"
    assert kontakt_merge.money(5049.9) == "5049.90"
    assert kontakt_merge.money("2,199.00") == "2199.00"
    assert kontakt_merge.money("1.899,99") == "1899.99"
    assert kontakt_merge.money("0 ₼") is None and kontakt_merge.money("—") is None


def test_merge_needs_nothing_but_openpyxl():
    """На Маке окружение — только openpyxl: склейка не тянет код проекта."""
    source = (ROOT / "src" / "kontakt_merge.py").read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert all(not line.startswith("from src") for line in imports), imports


def test_upload_checks_the_server_and_keeps_files_on_failure():
    upload = (ROOT / "infra" / "local" / "kontakt" / "upload.sh").read_text()
    # ssh делит UserKnownHostsFile по пробелам — ключ сервера не в «Application Support».
    assert 'KNOWN="$HOME/.ssh/kontakt_known_hosts"' in upload
    assert '-o UserKnownHostsFile="$KNOWN"' in upload
    assert "server ping" in upload and "src.kontakt_merge import COLUMNS" in upload
    # Выгрузки уезжают в «отправлено» только после того, как сервер принял файл.
    assert upload.index("server import") < upload.index('mv "${files[@]}"')


def test_install_removes_the_old_automatic_schedule():
    install = (ROOT / "infra" / "local" / "kontakt" / "install-mac.sh").read_text()
    assert "az.leaddrive.kontakt-collect" in install and "launchctl bootout" in install
    assert "launchctl bootstrap" not in install
