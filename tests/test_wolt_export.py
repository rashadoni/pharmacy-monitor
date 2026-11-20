"""Excel раздела «Аптеки на Wolt»: листы, колонки, цвета, сводка, эндпоинт."""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from openpyxl import load_workbook

from src import storage, tenants, wolt, wolt_export
from src._time import utcnow
from tests.test_wolt import ALOE, BUTA, KANON, _item, _owner_product, _seed

# Фикстуры test_wolt (свежий кэш, клиент API) нужны и здесь.
from tests.test_wolt import _fresh_cache, client  # noqa: F401


def _workbook(db_session, locale="ru", search=None):
    data = wolt.comparison(db_session, search=search)
    content = wolt_export.build_workbook(
        data,
        locale=locale,
        generated_at=datetime(2026, 11, 20, 6, 30),
        search=search,
    )
    return load_workbook(io.BytesIO(content))


def _seed_catalog(db_session):
    _owner_product(db_session, "Betadin 10% 30 ml", 6.0)
    _seed(
        db_session,
        _item(ALOE, "2", "Betadin 10% 30 ml", 9.3),
        _item(KANON, "3", "Betadin 10% 30 ml", 9.5),
        _item(BUTA, "4", "Duspatalin 200 mq N30", 17.0),
        _item(ALOE, "5", "Duspatalin 200 mq N30", 17.0),
        _item(BUTA, "6", "=HYPERLINK(1) 10 ml", 3.0),
        _item(KANON, "7", "=HYPERLINK(1) 10 ml", 3.5),
    )


def test_sheets_owner_first_and_only_different_prices_on_the_first(db_session):
    _seed_catalog(db_session)

    wb = _workbook(db_session)

    assert wb.sheetnames == ["Разная цена", "Все товары", "Сводка"]
    diff, every = wb["Разная цена"], wb["Все товары"]
    assert diff["B1"].value == "Цена: Pharmonline"
    names = [c.value for c in diff["A"][1:]]
    assert "Duspatalin 200 mq N30" not in names
    assert "Betadin 10% 30 ml" in names
    assert every.max_row == 4  # заголовок + три товара


def test_prices_colours_and_links(db_session):
    _seed_catalog(db_session)

    ws = _workbook(db_session)["Разная цена"]
    row = next(r for r in ws.iter_rows(min_row=2) if r[0].value == "Betadin 10% 30 ml")
    owner, buta, aloe, kanon = row[1], row[2], row[3], row[4]

    assert (owner.value, aloe.value, kanon.value, buta.value) == (6.0, 9.3, 9.5, None)
    assert owner.font.color.rgb.endswith("15803D")  # дешевле всех — зелёный
    assert kanon.font.color.rgb.endswith("B91C1C")  # дороже всех — красный
    assert row[5].value == "Pharmonline"
    assert row[6].value == pytest.approx(3.5)
    assert row[7].number_format == "0.0%"
    link = row[8]  # ссылки идут после цен, «дешевле всех» и двух разниц
    assert link.hyperlink.target.startswith("https://pharmonline.az/")


def test_name_that_looks_like_a_formula_stays_text(db_session):
    _seed_catalog(db_session)

    ws = _workbook(db_session)["Разная цена"]
    cell = next(r[0] for r in ws.iter_rows(min_row=2) if str(r[0].value).startswith("="))

    assert cell.data_type == "s"


def test_summary_has_totals_venues_and_pairs(db_session):
    _seed_catalog(db_session)

    ws = _workbook(db_session)["Сводка"]
    values = {row[0].value: row[1].value for row in ws.iter_rows() if row[0].value}

    assert values["Выгружено"] == "20.11.2026 10:30 (Bakı)"
    assert values["Товаров минимум в двух местах"] == 3
    assert values["Из них с разной ценой"] == 2
    assert values["Pharmonline"] == 1
    assert values["Pharmonline — Aloe Aptek — Ağ Şəhər"] == 1


def test_search_narrows_the_file_and_its_totals(db_session):
    _seed_catalog(db_session)

    wb = _workbook(db_session, search="duspatal")

    assert [c.value for c in wb["Все товары"]["A"][1:]] == ["Duspatalin 200 mq N30"]
    assert wb["Разная цена"].max_row == 1
    summary = {row[0].value: row[1].value for row in wb["Сводка"].iter_rows() if row[0].value}
    assert summary["Поиск"] == "duspatal"
    assert summary["Товаров минимум в двух местах"] == 1
    assert summary["Из них с разной ценой"] == 0
    assert "По парам, по всем товарам без поиска (в наличии у обеих)" in summary


def test_out_of_stock_is_grey_in_price_and_link(db_session):
    gone = _item(KANON, "3", "Betadin 10% 30 ml", 5.0)
    gone.in_stock = False
    _seed(
        db_session,
        _item(ALOE, "2", "Betadin 10% 30 ml", 9.3),
        _item(BUTA, "1", "Betadin 10% 30 ml", 9.5),
        gone,
    )

    ws = _workbook(db_session)["Все товары"]
    row = ws[2]
    kanon_price, kanon_link = row[4], row[11]

    assert kanon_price.value == 5.0
    assert kanon_price.font.color.rgb.endswith("9CA3AF")
    assert kanon_link.value == "нет в наличии"
    assert kanon_link.font.color.rgb.endswith("9CA3AF")
    # Дешевле всех — из тех, что в наличии.
    assert row[5].value == "Aloe Aptek — Ağ Şəhər"


def test_venue_update_time_is_in_baku_time(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Keratodrop 10 ml", 17.0),
        _item(ALOE, "2", "Keratodrop 10 ml", 18.0),
    )
    for scrape in db_session.query(storage.WoltScrape):
        scrape.finished_at = datetime(2026, 11, 20, 20, 15)
    db_session.commit()

    ws = _workbook(db_session)["Сводка"]
    buta = next(row for row in ws.iter_rows() if row[0].value == "Buta Aptek — Kral")

    assert buta[2].value == "21.11.2026 00:15"


@pytest.mark.parametrize(("locale", "first"), [("az", "Fərqli qiymət"), ("en", "Different price")])
def test_other_languages(db_session, locale, first):
    _seed_catalog(db_session)

    assert _workbook(db_session, locale=locale).sheetnames[0] == first


def test_filename_uses_baku_date():
    assert (
        wolt_export.filename("ru", datetime(2026, 11, 20, 21, 0))
        == "wolt-sravnenie-2026-11-21.xlsx"
    )


def test_api_needs_a_login(client):  # noqa: F811
    assert client.get("/api/v1/dash/wolt/export.xlsx").status_code == 401


def test_api_returns_an_excel_file(client, db_session):  # noqa: F811
    from src import api as api_module

    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    tenant = tenants.get_or_create_default(db_session)
    db_session.add(
        storage.TenantUser(
            tenant_id=tenant.id,
            email="viewer@example.test",
            role="viewer",
            is_active=True,
            created_at=utcnow(),
        )
    )
    db_session.commit()
    _seed_catalog(db_session)
    token = tenants.issue_magic_token(db_session, "viewer@example.test")
    assert client.get(f"/auth/verify?token={token}").status_code == 200

    response = client.get("/api/v1/dash/wolt/export.xlsx", params={"locale": "az"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == wolt_export.XLSX_MEDIA_TYPE
    assert 'filename="wolt-muqayise-' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert load_workbook(io.BytesIO(response.content)).sheetnames[0] == "Fərqli qiymət"


def test_api_refuses_a_too_long_search(client):  # noqa: F811
    from src import api as api_module

    api_module.app.dependency_overrides[api_module.require_user] = lambda: object()
    try:
        response = client.get("/api/v1/dash/wolt/export.xlsx", params={"search": "x" * 201})
    finally:
        api_module.app.dependency_overrides.pop(api_module.require_user, None)

    assert response.status_code == 422


def test_api_is_absent_in_another_profile(client, monkeypatch):  # noqa: F811
    from src import api as api_module
    from src import site_profile

    monkeypatch.setattr(site_profile, "is_pharmacy", lambda: False)
    monkeypatch.setitem(
        api_module.app.dependency_overrides, api_module.require_user, lambda: object()
    )

    assert client.get("/api/v1/dash/wolt/export.xlsx").status_code == 404
