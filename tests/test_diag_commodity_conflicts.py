"""Guards for the destructive commodity audit (`--apply` mutates production).

The script had no coverage at all. These pin the two properties that made its
readout contradict its own `--apply`.
"""

import scripts.diag_commodity_conflicts as diag
from src import matcher


class _P:
    def __init__(self, site, name, mfr=None, code=None, status="unknown", brand_verified=None):
        self.site = site
        self.name = name
        self.name_normalized = (name or "").lower()
        self.manufacturer = mfr
        self.manufacturer_country_code = code
        self.country_resolution_status = status
        self.brand_verified = brand_verified
        self.url = ""
        self.brand = None
        self.id = id(self)


def test_grade_tokens_match_the_guard_they_audit():
    """Az dotted `İ` broke this: `KOSMETİK YAĞ` read as no-grade here, grade there."""
    for name in ["KOSMETİK YAĞ", "Kosmetik yag", "kosmetik", "Zeytun yagi", "NARUZHNOE"]:
        assert diag._grade_tokens(name) == matcher._grade_tokens(name), name


def test_audit_sees_country_known_only_from_legacy_manufacturer():
    """The whole point: verified-only would report this cluster as clean."""
    a = _P("aptekonline", "Zeytun yagi 100 ml", mfr="TÜRKİYƏ")
    b = _P("pharmonline", "Zeytun yagi 100 ml", mfr="Azərbaycan")
    # No verified codes at all...
    assert matcher._has_conflicting_country(a, b) is False
    # ...but the audit must still see the conflict.
    assert matcher._has_conflicting_legacy_country(a, b) is True


def test_strict_targets_a_grade_only_cluster_so_it_must_not_be_labelled_country():
    """`_strict_not_identical` is (country OR grade). A grade-only target exists,
    so `--apply` must not hardcode reason_type='system_country' for it."""

    class _M:
        products = [
            _P("aptekonline", "Zeytun yagi kosmetik 100 ml", code="az", status="resolved"),
            _P("pharmonline", "Zeytun yagi 100 ml", code="az", status="resolved"),
        ]

    m = _M()
    assert diag._strict_not_identical(m) is True
    a, b = m.products
    # Same country -> the rejection this pair produces must be spec, not country.
    assert matcher._has_conflicting_legacy_country(a, b) is False
    assert diag._grade_tokens(a.name) != diag._grade_tokens(b.name)


def test_same_brand_same_country_commodity_is_not_targeted():
    """Guard against over-flagging on the --apply path (Medoil az↔az)."""

    class _M:
        products = [
            _P(
                "aptekonline",
                "Zeytun yagi 100 ml",
                code="az",
                status="resolved",
                brand_verified="Medoil",
            ),
            _P(
                "pharmonline",
                "Zeytun yagi 100 ml",
                code="az",
                status="resolved",
                brand_verified="Medoil",
            ),
        ]

    assert diag._strict_not_identical(_M()) is False
    assert diag._cosmetic_origin_false(_M()) is False
