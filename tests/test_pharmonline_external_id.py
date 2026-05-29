"""external_id из pharmonline product-href — locale-агностичность (2026-05-29).

Регрессия: `?lng=en`/`?lng=ru` в href раньше попадали в external_id → один товар
дублировался по локалям (~9.5K EN-дублей на проде). Теперь query/fragment
обрезаются → EN/AZ/RU мапятся на один external_id.
"""

from src.scrapers.pharmonline import _external_id_from_href


def test_strips_locale_query():
    assert _external_id_from_href("/product/ringer-400-ml?lng=en") == "ringer-400-ml"
    assert _external_id_from_href("/product/ringer-400-ml?lng=ru") == "ringer-400-ml"
    assert _external_id_from_href("/product/ringer-400-ml") == "ringer-400-ml"


def test_az_and_en_collapse_to_same_id():
    az = _external_id_from_href("/product/normoqlip-m-30")
    en = _external_id_from_href("/product/normoqlip-m-30?lng=en")
    assert az == en == "normoqlip-m-30"


def test_strips_trailing_slash_and_fragment():
    assert _external_id_from_href("/product/aspirin-c-10/") == "aspirin-c-10"
    assert _external_id_from_href("/product/aspirin-c-10#reviews") == "aspirin-c-10"
    assert _external_id_from_href("https://pharmonline.az/product/aspirin-c-10?lng=en") == (
        "aspirin-c-10"
    )


def test_multi_param_query():
    assert _external_id_from_href("/product/ringer-400-ml?lng=en&ref=cat") == "ringer-400-ml"
