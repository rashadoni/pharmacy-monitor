"""Matcher brand-conflict guard — wired into _hard_conflict + _pairwise_spec_conflict.

The client's bug: commodity oils with a generic name ("Alaqanqal yağı 100 ml")
sold by different firms (Biola vs Herba Flora) were clustered together. The guard
uses brand_verified (authoritative) and only fires on different CONSUMER brands —
manufacturer companies stay non-discriminating so trade-name drugs keep matching.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.matcher import _has_conflicting_brand, _hard_conflict, _pairwise_spec_conflict


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
