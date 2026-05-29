"""Тесты brand-blacklist (accent-insensitive) — coverage deep-dive 2026-05-29.

Контекст: generic AZ-слова (Günəş, Sabun, Baby, Qoruyucu…) массово извлекались
как «бренды», засоряя /analytics и matcher bucket_key. is_brand_blacklisted
теперь нормализует accents (ş→s, ə→e, ı→i) перед сравнением + расширенный список.
"""

from __future__ import annotations

import pytest

from src.brand_catalog import is_brand_blacklisted


@pytest.mark.parametrize(
    "fake",
    [
        "Günəş",  # ə+ş вариант — ловится несмотря на то что в списке gunes
        "GÜNƏŞ",
        "Şampun",
        "şampun",
        "Sabun",
        "Baby",
        "Body",
        "Leykoplastr",
        "Böyüklər",
        "Elastik",
        "Əmzik",
        "Qoruyucu",
        "Kalqotka",
        "Əlcək",
        "Prezervativ",
        "Bədən",
        "Maye",
        "Varikoz",
        "Ağız",
        "Toothpaste",
    ],
)
def test_generic_words_blocked(fake):
    assert is_brand_blacklisted(fake) is True


@pytest.mark.parametrize(
    "real",
    [
        "Bioderma",
        "Nivea",
        "Vichy",
        "Avent",
        "Huggies",
        "Nestle",
        "Pampers",  # generic «pamper» НЕ блокирует реальный бренд Pampers
        "Venatura",
        "Solgar",
        "Sebamed",
        "CeraVe",
        "La Roche-Posay",
    ],
)
def test_real_brands_preserved(real):
    assert is_brand_blacklisted(real) is False


def test_none_and_empty():
    assert is_brand_blacklisted(None) is False
    assert is_brand_blacklisted("") is False
    assert is_brand_blacklisted("   ") is False


def test_accent_insensitivity_explicit():
    """Günəş (ş/ə) и Gunes (ASCII) трактуются одинаково."""
    assert is_brand_blacklisted("Günəş") == is_brand_blacklisted("gunes")
    assert is_brand_blacklisted("Şampun") == is_brand_blacklisted("sampun")


def test_matcher_bucket_key_nulls_blacklisted_brand(db_session, monkeypatch):
    """Integration: matcher bucket_key для продукта с fake-брендом падает на
    name-fallback (не группирует по 'baby')."""
    from sqlalchemy import select

    from src import matcher, storage

    # Два продукта на разных сайтах, fake brand 'Baby', но одинаковое имя-продукт
    for site, ext in [("pharmonline", "p1"), ("aptekonline", "a1")]:
        db_session.add(
            storage.Product(
                tenant_id=1,
                site=site,
                external_id=ext,
                url=f"http://{site}/{ext}",
                name="Mustela bebe gentle şampun 200 ml",
                name_normalized="mustela bebe gentle sampun",
                brand="Baby",  # fake
                dosage="200ml",
                pack_size="200ml",
            )
        )
    db_session.commit()
    # monkeypatch авто-восстанавливает после теста (иначе протекает в др. тесты)
    monkeypatch.setattr(matcher, "latest_snapshots_per_product", lambda *a, **kw: {})
    matcher.match_products(db_session)
    # Должны сматчиться (через name-fallback bucket, не через fake 'baby')
    prods = db_session.scalars(
        select(storage.Product).where(storage.Product.canonical_id.is_not(None))
    ).all()
    assert len(prods) == 2, "products with fake brand should still match via name fallback"
    assert prods[0].canonical_id == prods[1].canonical_id
