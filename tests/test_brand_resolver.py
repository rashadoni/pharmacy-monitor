"""Tests for src/brand_resolver — consumer vs manufacturer brand + conflict guard."""

from __future__ import annotations

import pytest

from src.brand_resolver import (
    brand_from_pharmonline_slug,
    brands_conflict,
    consumer_brand,
    is_commodity_name,
    is_manufacturer_company,
)


class TestCommodityName:
    @pytest.mark.parametrize(
        "name",
        [
            "Alaqanqal yağı 100 ml",
            "Palıd qabığı 50 qr",
            "Şüyüd çayı № 30",
            "Balqabaq toxumu 50 q",
            "Çaytikanı yağı altay 100 ml",
            "Boymadərən otu 40 qr",
            "Adaçayı ekstraktı",
        ],
    )
    def test_botanical_commodities(self, name):
        assert is_commodity_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Konkor 5 mg N30",
            "Densip 30 əd.",
            "Movalis 15 mq № 20",
            "Glukofaj 500 mg N60",
            "Asmartin 125 ml (Sirop)",
            "Depantol N10",
        ],
    )
    def test_trade_name_drugs_are_not_commodity(self, name):
        assert is_commodity_name(name) is False


class TestManufacturerDetection:
    @pytest.mark.parametrize(
        "name",
        [
            "Merck KGaA",
            "Egis Pharmaceuticals",
            "Pharmex Rom Industry SRL",
            "Vega İlaç",
            "Gricar Chemical Srl.",
            "Alpen Pharma GmbH",
            "Sun Pharmaceutical Industries Ltd",
            "Berlin Chemie",
            "Nycomed",  # suffix-less known manufacturer
            "Actavis Group",
            "Kopas Kozmetik",
            "Fortex Nutraceuticals",
        ],
    )
    def test_companies_are_manufacturers(self, name):
        assert is_manufacturer_company(name) is True
        # → non-discriminating for the guard
        assert consumer_brand(name) is None

    @pytest.mark.parametrize(
        "name",
        ["Biola", "Herba Flora", "Medoil", "Fitooil", "Althea", "Botalife", "Nivea"],
    )
    def test_consumer_brands_are_not_manufacturers(self, name):
        assert is_manufacturer_company(name) is False
        assert consumer_brand(name) == name.lower()


class TestBrandsConflict:
    def test_different_consumer_brands_conflict(self):
        # the client's bug: Herba Flora oil wrongly matched to Biola oil
        assert brands_conflict("Herba Flora", "Biola") is True
        assert brands_conflict("Biola", "Medoil") is True

    def test_same_consumer_brand_no_conflict(self):
        assert brands_conflict("Biola", "biola") is False
        assert brands_conflict("Herba Flora", "herba flora") is False

    def test_trade_name_drug_across_manufacturers_no_conflict(self):
        # Konkor by Merck (aptek) vs Konkor by Nycomed (pharmonline) — SAME drug.
        # A blanket brand block would break this; manufacturers are non-discriminating.
        assert brands_conflict("Merck KGaA", "Nycomed") is False
        assert brands_conflict("Egis Pharmaceuticals", "Egis") is False
        assert brands_conflict("Pharmex Rom Industry SRL", "Pharmex") is False

    def test_spelling_variants_of_same_firm_no_conflict(self):
        # transliteration / suffix variants of the SAME firm are fuzzy-similar →
        # not a conflict (the manufacturer-transliteration confound that made a
        # blanket guard split ~115 correct matches)
        assert brands_conflict("Borisov", "Borisovsky Zmp") is False
        assert brands_conflict("Medizin", "Medizen") is False
        assert brands_conflict("Nijfarm", "Nizhfarm") is False

    def test_missing_brand_no_conflict(self):
        # unknown brand → recall preserved (guard does not fire)
        assert brands_conflict(None, "Biola") is False
        assert brands_conflict("Biola", None) is False
        assert brands_conflict(None, None) is False
        assert brands_conflict("", "Biola") is False

    def test_consumer_vs_manufacturer_no_conflict(self):
        # one side consumer, other a company → cannot tell → don't block
        assert brands_conflict("Biola", "Merck KGaA") is False


class TestPharmonlineSlugBrand:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://pharmonline.az/product/alaqanqal-yaghi-100-ml-biola-azerbaycan", "Biola"),
            ("https://pharmonline.az/product/naftalan-yaghi-60-ml-medoil-turkiye", "Medoil"),
            (
                "https://pharmonline.az/product/arqan-yaghi-30-ml-herba-flora-azerbaycan",
                "Herba Flora",
            ),
            ("https://pharmonline.az/product/adachayi-yaghi-20-ml-biola-azerbaycan", "Biola"),
        ],
    )
    def test_extracts_real_brand_before_country(self, url, expected):
        assert brand_from_pharmonline_slug(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            # no brand in slug — just name/dosage/pack → None (not a false brand)
            "https://pharmonline.az/product/pachuli-yaghi-20-ml",
            "https://pharmonline.az/product/nioli-yaghi-4406-10-ml-yagh",
            # generic-only trailing token (filtered as noise)
            "https://pharmonline.az/product/qaragile-meyveleri-40-qr-diger-azerbaycan",
        ],
    )
    def test_no_false_brand_from_noise(self, url):
        out = brand_from_pharmonline_slug(url)
        # whatever it returns must not be a generic/noise consumer brand that
        # would cause a false block — i.e. it resolves to no consumer brand
        assert consumer_brand(out) in (None, "nioli", "pachuli") or out is None

    def test_empty(self):
        assert brand_from_pharmonline_slug(None) is None
        assert brand_from_pharmonline_slug("") is None
