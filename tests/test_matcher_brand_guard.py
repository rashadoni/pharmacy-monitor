"""Matcher brand-conflict guard — wired into _hard_conflict + _pairwise_spec_conflict.

The client's bug: commodity oils with a generic name ("Alaqanqal yağı 100 ml")
sold by different firms (Biola vs Herba Flora) were clustered together. The guard
uses brand_verified (authoritative) and only fires on different CONSUMER brands —
manufacturer companies stay non-discriminating so trade-name drugs keep matching.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.matcher import (
    _doses_mg_from_url,
    _has_conflicting_brand,
    _has_conflicting_dose,
    _has_conflicting_ingredient_codes,
    _has_conflicting_origin_or_grade,
    _has_conflicting_pack_volume,
    _has_conflicting_variant_words,
    _hard_conflict,
    _pairwise_spec_conflict,
    ultra_equal,
)


def _p(name, brand_verified, url=""):
    return SimpleNamespace(
        name=name,
        name_normalized=name.lower(),
        brand_verified=brand_verified,
        url=url,
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

    def test_saline_tonicity_variant_conflicts(self):
        # Marimer/Aqua Maris izotonik ≠ hipertonik — different salt concentration,
        # different product. Recall dry-run 2026-05-31 surfaced "Marimer 100ml" ✗
        # "Marimer hipertonik 100ml" as a near-twin that must NOT auto-link.
        a = _p("Marimer 100 ml burun spreyi", None)
        b = _p("Marimer hipertonik 100 ml burun spreyi", None)
        assert _has_conflicting_variant_words(a, b) is True
        assert _hard_conflict(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True
        # same tonicity on both sides → still a match
        assert (
            _has_conflicting_variant_words(
                _p("Aqua Maris izotonik 30 ml", None), _p("Aqua Maris izotonik № 1 30 ml", None)
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

    def test_pack_volume_unit_mismatch_and_kg_do_not_fire(self):
        # different unit family (toothpaste 100ml vs 100g = same product) → no conflict
        assert (
            _has_conflicting_pack_volume(_p("Splat 100 ml", None), _p("Splat 100 qr", None))
            is False
        )
        # kg is a diaper BABY-weight range, not package volume → never fires
        assert (
            _has_conflicting_pack_volume(
                _p("Paddlers 11-25kg N52", None), _p("Paddlers 11-18kg N52", None)
            )
            is False
        )

    def test_dose_conflict_from_names(self):
        # different single dose, and different multi-component combos (Tripliksam 10mg vs 5mg)
        assert (
            _has_conflicting_dose(
                _p("Risek Insta 40 mq № 10", None), _p("Risek Insta 20 mq № 10", None)
            )
            is True
        )
        a = _p("Tripliksam 5 mq/1.25 mq/10 mq N30", None)
        b = _p("Tripliksam 5 mq/1.25 mq/5 mq N30", None)
        assert _has_conflicting_dose(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True

    def test_dose_multicomponent_same_no_conflict(self):
        # decimal comma/dot + spaced thousands must NOT create spurious doses
        assert (
            _has_conflicting_dose(
                _p("Tripliksam 5 mq/1.25 mq/10 mq", None), _p("Tripliksam 5mq/1,25mq/10mq", None)
            )
            is False
        )
        assert (
            _has_conflicting_dose(
                _p("Terapin Bid 1 000 mq № 14", None), _p("Terapin Bid 1000 mq N14", None)
            )
            is False
        )

    def test_dose_same_or_missing_no_conflict(self):
        # same dose (mq == mg) → no conflict
        assert _has_conflicting_dose(_p("X 40 mq", None), _p("X 40 mg N10", None)) is False
        # one side has no dose unit (bare pack number) → not a conflict
        assert _has_conflicting_dose(_p("X 500 mg", None), _p("X № 20", None)) is False
        # mcg vs mg equivalence (1000 mcg == 1 mg) → no conflict
        assert _has_conflicting_dose(_p("X 1000 mcg", None), _p("X 1 mg", None)) is False

    def test_dose_uses_safe_url_fallback_when_title_omits_strength(self):
        title_poor = _p(
            "Risek İnsta N10 (toz)",
            None,
            "https://www.aptekonline.az/product/risek-40mg-n10",
        )
        assert _doses_mg_from_url(title_poor.url) == frozenset({40.0})
        assert _has_conflicting_dose(title_poor, _p("Risek Insta 20 mq № 10", None)) is True
        assert _has_conflicting_dose(title_poor, _p("Risek Insta 40 mq № 10", None)) is False

    def test_url_dose_fallback_rejects_ambiguous_slug_encodings(self):
        assert _doses_mg_from_url("https://example.test/product/foo-7-5mg-n10") == frozenset()
        assert _doses_mg_from_url("https://example.test/product/foo-5mg125mg10mg") == frozenset()

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


class TestStrictCommodityOrigin:
    """Country is strict for every SKU; grade remains commodity-specific."""

    @staticmethod
    def _pc(name, brand_verified=None, manufacturer=None, url="", country_code=None):
        return SimpleNamespace(
            name=name,
            name_normalized=name.lower(),
            brand_verified=brand_verified,
            manufacturer=manufacturer,
            url=url,
            manufacturer_country_code=country_code,
            country_resolution_status="resolved" if country_code else "unknown",
        )

    def test_diff_country_no_brand_conflicts(self):
        a = self._pc("Qara zirə yağı 100 ml", manufacturer="AZERBAYCAN")
        b = self._pc("Qara zirə yağı 100 ml", manufacturer="RUSİYA")
        assert _has_conflicting_origin_or_grade(a, b) is True
        assert _hard_conflict(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True

    def test_same_brand_diff_country_conflicts(self):
        # client policy 2026-05-31: country must match too — Medoil Türkiyə ≠ Medoil
        # Azərbaycan even though brand matches (client explicitly required this).
        a = self._pc("Naftalan yağı 60 ml", "Medoil", manufacturer="TÜRKİYƏ")
        b = self._pc("Naftalan yağı 60 ml", "Medoil", manufacturer="AZERBAYCAN")
        assert _has_conflicting_origin_or_grade(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True

    def test_grade_asymmetry_conflicts(self):
        # cosmetic-grade ≠ regular (the client's Çaytikanı kosmetik case)
        a = self._pc("Çaytikanı yağı 100 ml (kosmetik)", manufacturer="RUSİYA")
        b = self._pc("Çaytikanı yağı 100 ml", manufacturer="AZERBAYCAN")
        assert _has_conflicting_origin_or_grade(a, b) is True

    def test_same_country_same_grade_no_conflict(self):
        a = self._pc("Gənəgərçək yağı 30 ml", manufacturer="AZERBAYCAN")
        b = self._pc("Gənəgərçək yağı 30 ml", manufacturer="AZERBAYCAN")
        assert _has_conflicting_origin_or_grade(a, b) is False

    def test_non_commodity_drug_diff_country_conflicts_under_new_policy(self):
        # Client policy 2026-07-13 supersedes the old trade-name exception.
        a = self._pc(
            "Konkor 5 mg N30", "Merck", manufacturer="ALMANİYA", country_code="de"
        )
        b = self._pc(
            "Konkor 5 mg N30", "Merck", manufacturer="FRANSA", country_code="fr"
        )
        assert _has_conflicting_origin_or_grade(a, b) is False
        assert _hard_conflict(a, b) is True
        assert _pairwise_spec_conflict(a, b) is True


class TestUltraEqual:
    """ultra_equal — high-confidence equality powering recall_candidates --ultra
    AND the UI auto_safe badge. EQUAL (not subset) on every spec axis."""

    def test_formatting_only_difference_is_equal(self):
        # punctuation/spacing/case only → same product (these are in the applied set)
        assert ultra_equal(_p("Tioktas N30", None), _p("TioktAs № 30", None)) is True
        assert ultra_equal(_p("Estova 2 mq N28", None), _p("Estova 2 mq № 28", None)) is True
        assert (
            ultra_equal(
                _p("Qara zirə yağı 100 ml", None), _p("Qara zirə yağı  100 ml", None)
            )
            is True
        )

    def test_noise_form_words_collapse_equal(self):
        # form words (şərbət/sirop) drop out under real normalize_name → both reduce
        # to the product token. Uses normalize_name (not the naive _p lowercase) since
        # this asserts the prod normalize+ultra_equal interaction.
        from src.normalize import normalize_name

        def _np(name):
            return SimpleNamespace(name=name, name_normalized=normalize_name(name), brand_verified=None, url="")

        assert ultra_equal(_np("Litosit şərbət 100 ml"), _np("Litosit 100 ml (Sirop)")) is True

    def test_size_letter_difference_not_equal(self):
        assert ultra_equal(_p("Pufies M 30", None), _p("Pufies L 30", None)) is False

    def test_ingredient_code_difference_not_equal(self):
        # the D3 vs D3+K2 class the client flagged — must NOT be auto_safe
        assert ultra_equal(_p("Venatura D3 20 ml", None), _p("Venatura D3 K2 20 ml", None)) is False

    def test_dose_difference_not_equal(self):
        assert ultra_equal(_p("Risek 40 mq N10", None), _p("Risek 20 mq N10", None)) is False

    def test_extra_significant_token_not_equal(self):
        # aloe-vera vs aloe: "vera" is a significant token on one side only
        assert ultra_equal(_p("Aloe vera gel 100 ml", None), _p("Aloe gel 100 ml", None)) is False

    def test_pack_size_difference_not_equal(self):
        assert (
            ultra_equal(_p("Qara zirə yağı 100 ml", None), _p("Qara zirə yağı 250 ml", None))
            is False
        )

    # ── brand-aware ultra_equal (workflow wmol35wiv 2026-05-31) ──────────────
    def test_ultra_equal_demotes_cross_brand_commodity(self):
        # commodity oil, different CONSUMER brands → not a safe exact match
        # (recall-apply gate + UI auto_safe badge both go through ultra_equal)
        assert (
            ultra_equal(
                _p("Alaqanqal yağı 100 ml", "Herba Flora"),
                _p("Alaqanqal yağı 100 ml", "Biola"),
            )
            is False
        )

    def test_ultra_equal_keeps_same_firm_translit_commodity(self):
        # same firm spelled differently → fuzzy bridge keeps it auto_safe
        assert (
            ultra_equal(
                _p("Valerian yağı 50 ml", "Borisov"),
                _p("Valerian yağı 50 ml", "Borisovsky Zmp"),
            )
            is True
        )

    def test_ultra_equal_keeps_null_brand_commodity_twin(self):
        # NULL brand = missing metadata, correct twin → stays auto_safe (recall)
        assert (
            ultra_equal(
                _p("Exinasea ekstraktı 50 ml", None),
                _p("Exinasea ekstraktı 50 ml", None),
            )
            is True
        )

    def test_ultra_equal_inert_for_trade_name_drug(self):
        # non-commodity name → brand gate cannot fire even w/ different makers
        assert (
            ultra_equal(_p("Konkor 5 mg N30", "Merck KGaA"), _p("Konkor 5 mg N30", "Nycomed"))
            is True
        )

    def test_ultra_equal_kosmetika_pad_word_still_collapses(self):
        # regression lock: pharmonline category pad "(Kosmetika)" must NOT split a
        # NULL-brand twin (grade-word guard was REJECTED — pads aren't grades).
        from src.normalize import normalize_name

        def _np(name):
            return SimpleNamespace(
                name=name, name_normalized=normalize_name(name), brand_verified=None, url=""
            )

        assert (
            ultra_equal(_np("Çaytıkanı yağı 100 ml"), _np("Çaytıkanı yağı 100 ml (Kosmetika)"))
            is True
        )
