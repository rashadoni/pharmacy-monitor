"""Matcher brand-conflict guard — wired into _hard_conflict + _pairwise_spec_conflict.

The client's bug: commodity oils with a generic name ("Alaqanqal yağı 100 ml")
sold by different firms (Biola vs Herba Flora) were clustered together. The guard
uses brand_verified (authoritative) and only fires on different CONSUMER brands —
manufacturer companies stay non-discriminating so trade-name drugs keep matching.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.matcher import (
    _has_conflicting_brand,
    _has_conflicting_ingredient_codes,
    _has_conflicting_pack_volume,
    _has_conflicting_variant_words,
    _hard_conflict,
    _pairwise_spec_conflict,
)


def _p(name, brand_verified):
    return SimpleNamespace(
        name=name,
        name_normalized=name.lower(),
        brand_verified=brand_verified,
    )


class TestBrandGuard:
    def test_different_consumer_brands_conflict(self):
        # exactly the client's cluster 100014: Herba Flora oil vs Biola oil
        a = _p("Alaqanqal yağı 100 ml", "Herba Flora")
        b = _p("Alaqanqal yağı 100 ml", "Biola")
        assert _has_conflicting_brand(a, b) is True
        assert _hard_conflict(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True

    def test_same_consumer_brand_no_conflict(self):
        # cluster 100013: Biola ↔ Biola (correct match, real 14.20 vs 14.00 gap)
        a = _p("Alaqanqal yağı 100 ml", "Biola")
        b = _p("Alaqanqal yağı 100 ml", "biola")
        assert _has_conflicting_brand(a, b) is False
        assert _hard_conflict(a, b) is False
        assert _pairwise_spec_conflict(a, b) is False

    def test_trade_name_drug_different_manufacturer_no_conflict(self):
        # Konkor by Merck (aptek) vs Konkor by Nycomed (pharmonline) — same drug.
        # Blanket brand block would break this; manufacturers are non-discriminating.
        a = _p("Konkor 5 mg N30", "Merck KGaA")
        b = _p("Konkor 5 mg N30", "Nycomed")
        assert _has_conflicting_brand(a, b) is False
        assert _hard_conflict(a, b) is False
        assert _pairwise_spec_conflict(a, b) is False

    def test_missing_brand_no_conflict(self):
        # unknown brand on either side → recall preserved
        a = _p("Alaqanqal yağı 100 ml", None)
        b = _p("Alaqanqal yağı 100 ml", "Biola")
        assert _has_conflicting_brand(a, b) is False
        assert _hard_conflict(a, b) is False

    def test_ingredient_code_combo_conflicts(self):
        # client's case: D3 mono vs D3+K2 combo — same brand/volume, different composition
        d3 = _p("Venatura Vitamin D3 20 ml", None)
        d3k2 = _p("Venatura Vitamin D3 K2 20 ml", None)
        assert _has_conflicting_ingredient_codes(d3, d3k2) is True
        assert _hard_conflict(d3, d3k2) is True
        assert _pairwise_spec_conflict(d3, d3k2) is True

    def test_ingredient_code_same_no_conflict(self):
        a = _p("Venatura Vitamin D3 20 ml", None)
        b = _p("Venatura Vitamin D3 damci 20 ml", None)
        assert _has_conflicting_ingredient_codes(a, b) is False

    def test_ingredient_code_omission_no_conflict(self):
        # one side omits the code (DetriBus == DetriBus D3, P-Devit B6-spacing) → NOT a conflict
        assert (
            _has_conflicting_ingredient_codes(
                _p("DetriBus 15 ml", None), _p("DetriBus D3 15 ml", None)
            )
            is False
        )
        assert (
            _has_conflicting_ingredient_codes(_p("Proqram B 6", None), _p("Proqram B6 N20", None))
            is False
        )

    def test_variant_word_conflicts(self):
        # unambiguous product-line modifiers → different product
        assert (
            _has_conflicting_variant_words(_p("Linkas № 16", None), _p("Linkas Plyus 120 ml", None))
            is True
        )
        assert (
            _has_conflicting_variant_words(_p("Kodelak N10", None), _p("Kodelak Bronxo N10", None))
            is True
        )
        assert (
            _has_conflicting_variant_words(_p("Snip № 20", None), _p("Snip pain N20", None)) is True
        )
        assert _hard_conflict(_p("Linkas № 16", None), _p("Linkas Plyus 120 ml", None)) is True
        assert (
            _pairwise_spec_conflict(_p("Kodelak N10", None), _p("Kodelak Bronxo N10", None)) is True
        )

    def test_variant_word_same_no_conflict(self):
        assert (
            _has_conflicting_variant_words(
                _p("Linkas Plyus 120", None), _p("Linkas Plyus № 16", None)
            )
            is False
        )

    def test_size_and_translation_words_do_not_fire(self):
        # diaper size-name == size-number (same product) and men==kişilər (translation)
        # are deliberately NOT in the whitelist → must NOT split (dry-run showed ~15 such)
        assert (
            _has_conflicting_variant_words(
                _p("Sleepy Natural-3 4-9kq", None), _p("Sleepy Natural Midi 4-9kq", None)
            )
            is False
        )
        assert (
            _has_conflicting_variant_words(
                _p("Nivea Men duş geli", None), _p("Nivea kişilər duş geli", None)
            )
            is False
        )

    def test_form_word_omission_no_conflict(self):
        # form/descriptor words are NOT in the whitelist → omission stays a match
        assert (
            _has_conflicting_variant_words(_p("Yarpız 40 q", None), _p("Yarpız otu 40 qr", None))
            is False
        )
        assert (
            _has_conflicting_variant_words(
                _p("Levomekol 40 q", None), _p("Levomekol məlhəm 40 q", None)
            )
            is False
        )
        assert (
            _has_conflicting_variant_words(
                _p("Kəpənək N21", None), _p("Kəpənək venoz kateter N21", None)
            )
            is False
        )

    def test_pack_volume_conflict(self):
        # workflow audit: Azoksin 200mq/5ml 15ml vs 30ml — the "5ml" concentration
        # base shadowed the bottle volume → 15ml flacon merged with 30ml
        a = _p("Azoksin 200mq/5ml 15ml", None)
        b = _p("Azoksin 200mq/5ml 30ml", None)
        assert _has_conflicting_pack_volume(a, b) is True
        assert _hard_conflict(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True

    def test_pack_volume_same_or_missing_no_conflict(self):
        # same total volume despite concentration token → no conflict
        assert (
            _has_conflicting_pack_volume(_p("X 200mq/5ml 15ml", None), _p("X 15 ml", None)) is False
        )
        # one side has no total volume (concentration only) → omission, not a conflict
        assert (
            _has_conflicting_pack_volume(_p("X 200mq/5ml", None), _p("X 200mq/5ml 30ml", None))
            is False
        )

    def test_non_commodity_different_brands_no_conflict(self):
        # commodity gate: a trade-name drug with different brand strings must NOT
        # fire — aloe/pharmonline spell the same manufacturer differently
        # (Biofarm vs Biofarm Spzoo, Merk vs Merck Sante) and blanket-blocking
        # split ~115 correct matches. Identical name isolates the brand gate.
        a = _p("Densip 30", "Biofarm")
        b = _p("Densip 30", "Pharmaco")
        assert _has_conflicting_brand(a, b) is False
        assert _hard_conflict(a, b) is False
        assert _pairwise_spec_conflict(a, b) is False
