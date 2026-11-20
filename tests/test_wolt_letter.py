"""Аптеки на Wolt в утреннем письме: раздел под ценами сайтов и второй файл.

Решение владельца 2026-11-20: сравнение аптек Wolt приходит тем же письмом,
что и разница цен сайтов. Числа раздела пересчитываются из приложенного файла
— как у раздела сайтов.
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import notifications, product_policy, site_profile, storage, wolt, wolt_export
from src._time import utcnow
from tests.test_daily_price_letter import (
    _event,
    _mixed_catalog,
    _number,
    _pair,
    _reader,
    _text,
    _trusted_run,
)

BUTA, ALOE, KANON = list(wolt.VENUES)


@pytest.fixture(autouse=True)
def _fresh_wolt_cache():
    wolt._rows_cache.clear()
    wolt._last_rows = None
    yield
    wolt._rows_cache.clear()
    wolt._last_rows = None


@pytest.fixture
def letter_db(monkeypatch, db_session):
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.test")
    monkeypatch.delenv("COUNTRY_IDENTITY_POLICY", raising=False)
    monkeypatch.delenv("OFFER_AVAILABILITY_POLICY", raising=False)
    _reader(db_session)
    return db_session


def _wolt(venue, item_id, name, price, *, in_stock=True, original=None):
    return storage.WoltItem(
        venue=venue,
        item_id=item_id,
        name=name,
        price=price,
        original_price=original,
        in_stock=in_stock,
        in_catalog=True,
    )


def _wolt_catalog(s, *, collected=None) -> storage.Run:
    """По товару на каждый исход раздела Wolt; цены владельца — из сбора его сайта."""
    run = _trusted_run(s)
    _mixed_catalog(s, run)
    _pair(s, run, "Betadin 10% 30 ml", {"pharmonline": 8.0})
    _pair(s, run, "Duspatalin 200 mq N30", {"pharmonline": 17.0})
    _pair(s, run, "Enterol 250 mq N10", {"pharmonline": 12.0})
    # У владельца товар есть, но не в наличии: в сравнение он не входит.
    _pair(s, run, "Nimesil 100 mq N30", {"pharmonline": 4.0})
    s.query(storage.Product).filter_by(name="Nimesil 100 mq N30").update(
        {"offer_availability_status": product_policy.OFFER_OUT_OF_STOCK}
    )
    s.add_all(
        [
            # Дешевле в одной из аптек Wolt.
            _wolt(BUTA, "1", "Betadin 10% 30 ml", 6.6),
            _wolt(ALOE, "2", "Betadin 10% 30 ml", 9.3),
            # Цена одинаковая.
            _wolt(KANON, "3", "Duspatalin 200 mq N30", 17.0),
            # У владельца самая низкая.
            _wolt(BUTA, "4", "Enterol 250 mq N10", 13.0, original=15.0),
            _wolt(KANON, "5", "Enterol 250 mq N10", 14.0),
            # Владельца нет — различаются цены аптек Wolt.
            _wolt(ALOE, "6", "Linex N16", 10.0),
            _wolt(KANON, "7", "Linex N16", 11.0),
            _wolt(BUTA, "10", "Nimesil 100 mq N30", 5.0),
            _wolt(ALOE, "11", "Nimesil 100 mq N30", 6.0),
            # В наличии только в одном месте — сравнить не с чем.
            _wolt(BUTA, "8", "Smecta N10", 5.0, in_stock=False),
            _wolt(ALOE, "9", "Smecta N10", 6.0),
        ]
    )
    collected = collected or {}
    for venue in wolt.VENUES:
        s.add(
            storage.WoltScrape(
                venue=venue,
                status="ok",
                items=3,
                finished_at=collected.get(venue, utcnow() - timedelta(hours=23)),
            )
        )
    s.commit()
    return run


def _send(s):
    with patch("src.notifier.send_email", return_value=True) as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=1)
    assert sent == 1
    (call,) = mock_email.call_args_list
    return call


def _wolt_part(call) -> str:
    letter = _text(call.kwargs["html_body"])
    assert "Аптеки на Wolt" in letter
    return letter.split("Аптеки на Wolt", 1)[1]


def _sheet_rows(ws) -> list[tuple]:
    return list(ws.iter_rows(min_row=2, values_only=True))


def test_one_letter_carries_both_comparisons_and_both_files(letter_db):
    s = letter_db
    _wolt_catalog(s)

    call = _send(s)

    letter = _text(call.kwargs["html_body"])
    # Сначала цены сайтов, под ними аптеки Wolt — одним письмом.
    assert letter.index("Цена на сайтах различается") < letter.index("Аптеки на Wolt")
    sites_file, wolt_file = call.kwargs["attachments"]
    assert sites_file[0].startswith("raznye-ceny-")
    assert wolt_file[0].startswith("wolt-apteki-") and wolt_file[0].endswith(".xlsx")
    assert wolt_file[2] == wolt_export.comparison_export.XLSX_MEDIA_TYPE
    # Тема — прежняя: про сайты.
    assert ": различаются у 6 товаров" in call.kwargs["subject"]


def test_wolt_numbers_can_be_recounted_from_the_attached_file(letter_db):
    s = letter_db
    _wolt_catalog(s)

    call = _send(s)

    part = _wolt_part(call)
    book = load_workbook(io.BytesIO(call.kwargs["attachments"][1][1]))
    differing = _sheet_rows(book.worksheets[0])
    everything = _sheet_rows(book.worksheets[1])
    position = 1 + len(wolt.COLUMNS) + 3  # колонка «Позиция Pharmonline», с нуля
    positions = [row[position] for row in differing]

    assert _number(part, "Товаров в сравнении") == len(everything) == 6
    assert _number(part, "Цена различается") == len(differing) == 4
    assert _number(part, "в одной из аптек Wolt дешевле, чем на Pharmonline") == (
        positions.count("Самая высокая") + positions.count("Посередине")
    )
    assert _number(part, "на Pharmonline цена самая низкая") == positions.count("Самая низкая")
    assert _number(part, "Pharmonline в сравнение не вошёл — различаются цены аптек Wolt") == (
        positions.count(None)
    )
    assert _number(part, "Цена одинаковая") == 1
    assert _number(part, "Сравнить не с чем: товар в наличии меньше чем в двух местах") == 1
    assert sorted(positions, key=str) == sorted(["Посередине", "Самая низкая", None, None], key=str)
    assert {row[0] for row in differing} == {
        "Betadin 10% 30 ml",
        "Enterol 250 mq N10",
        "Linex N16",
        "Nimesil 100 mq N30",
    }


def test_owner_against_each_wolt_pharmacy(letter_db):
    s = letter_db
    _wolt_catalog(s)

    part = _wolt_part(_send(s))

    line = "{} общих {} · дешевле на Pharmonline {} · дешевле в аптеке {} · поровну {}"
    assert line.format("Buta Aptek — Kral", 2, 1, 1, 0) in part
    assert line.format("Aloe Aptek — Ağ Şəhər", 1, 1, 0, 0) in part
    assert line.format("Kanon Aptek — Torgovaya", 2, 1, 0, 1) in part


def test_file_shows_stock_and_discount_and_links(letter_db):
    s = letter_db
    _wolt_catalog(s)

    call = _send(s)

    book = load_workbook(io.BytesIO(call.kwargs["attachments"][1][1]))
    ws = book.worksheets[1]
    header = [cell.value for cell in ws[1]]
    assert header[:5] == [
        "Товар",
        "Pharmonline, AZN",
        "Buta Aptek — Kral, AZN",
        "Aloe Aptek — Ağ Şəhər, AZN",
        "Kanon Aptek — Torgovaya, AZN",
    ]
    rows = {row[0].value: row for row in ws.iter_rows(min_row=2)}
    note = header.index("Примечание")
    assert rows["Smecta N10"][note].value == "Buta Aptek — Kral: нет в наличии, в разницу не входит"
    assert rows["Nimesil 100 mq N30"][note].value == (
        "Pharmonline: нет в наличии, в разницу не входит"
    )
    assert rows["Enterol 250 mq N10"][note].value == (
        "Buta Aptek — Kral: цена со скидкой, без скидки 15.00"
    )
    link = rows["Betadin 10% 30 ml"][header.index("Ссылка Buta Aptek — Kral")]
    assert link.hyperlink.target.endswith(f"/venue/{BUTA}/itemid-1")
    info = [row[0] for row in book.worksheets[2].iter_rows(values_only=True)]
    assert "Kanon Aptek — Torgovaya: каталог собран" in info


def test_wolt_catalog_not_collected_lately_is_flagged(letter_db):
    s = letter_db
    _wolt_catalog(s, collected={KANON: utcnow() - timedelta(days=2)})

    part = _wolt_part(_send(s))

    assert "Каталог Kanon Aptek — Torgovaya — от" in part
    assert "с тех пор сбор на Wolt не прошёл" in part
    assert "Каталог Buta Aptek — Kral — от" not in part


def test_without_a_wolt_catalog_the_letter_is_as_before(letter_db):
    s = letter_db
    _mixed_catalog(s, _trusted_run(s))

    with capture_logs() as logs:
        call = _send(s)

    assert "Аптеки на Wolt" not in call.kwargs["html_body"]
    assert len(call.kwargs["attachments"]) == 1
    skipped = [e for e in logs if e["event"] == "digest_wolt_report_skipped"]
    assert [e["reason"] for e in skipped] == ["no_wolt_catalog"]


def test_broken_wolt_part_does_not_stop_the_letter(letter_db, monkeypatch):
    s = letter_db
    _wolt_catalog(s)

    def broken(*args, **kwargs):
        raise KeyError("venue")

    monkeypatch.setattr(wolt_export, "build_workbook", broken)
    with capture_logs() as logs:
        call = _send(s)

    letter = _text(call.kwargs["html_body"])
    assert "Цена на сайтах различается" in letter
    assert "Аптеки на Wolt" not in letter
    assert len(call.kwargs["attachments"]) == 1
    failed = [e for e in logs if e["event"] == "digest_wolt_report_failed"]
    assert len(failed) == 1 and failed[0]["error_type"] == "KeyError"
    assert "@" not in repr(logs)


def test_another_instance_or_tenant_has_no_wolt_part(letter_db, monkeypatch):
    s = letter_db
    _wolt_catalog(s)

    assert notifications._wolt_report(s, 2, locale="ru") is None
    monkeypatch.setattr(site_profile, "is_pharmacy", lambda: False)
    assert notifications._wolt_report(s, 1, locale="ru") is None


def test_weekly_digest_has_no_wolt_part(letter_db):
    s = letter_db
    run = _wolt_catalog(s)
    s.query(storage.TenantUser).update({"weekly_digest": True})
    s.commit()
    _event(s, run)

    with patch("src.notifier.send_email", return_value=True) as mock_email:
        sent = notifications.send_weekly_digest(s, tenant_id=1)

    assert sent == 1
    (call,) = mock_email.call_args_list
    assert "Kreon 25000" in call.kwargs["html_body"]
    assert "Аптеки на Wolt" not in call.kwargs["html_body"]
    assert "attachments" not in call.kwargs
