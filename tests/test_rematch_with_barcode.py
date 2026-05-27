"""Tests для scripts/rematch_with_barcode.py (Phase 2.4).

Покрываем decision logic `_cluster_barcodes_disagree`:
- Distinct barcodes ≥ 2 → disagree (break cluster)
- All None / all same → no disagree (keep cluster)
- Mix null + one barcode → no disagree (not enough signal)
- "0" / short barcode → ignored as noise
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rematch_with_barcode import _cluster_barcodes_disagree  # noqa: E402
from src import storage  # noqa: E402


def _mk(site: str, barcode: str | None) -> storage.Product:
    """Light unattached Product for decision-logic tests."""
    return storage.Product(
        tenant_id=1, site=site, external_id=f"{site}-x", url="http://x",
        name="X", name_normalized="x", barcode=barcode,
    )


def test_disagree_when_two_distinct_barcodes():
    cluster = [
        _mk("pharmonline", "4607017950021"),
        _mk("aptekonline", "5712345678901"),
    ]
    assert _cluster_barcodes_disagree(cluster) is True


def test_no_disagree_when_all_same():
    cluster = [
        _mk("pharmonline", "4607017950021"),
        _mk("aptekonline", "4607017950021"),
        _mk("aloe", "4607017950021"),
    ]
    assert _cluster_barcodes_disagree(cluster) is False


def test_no_disagree_when_all_null():
    cluster = [_mk("pharmonline", None), _mk("aptekonline", None)]
    assert _cluster_barcodes_disagree(cluster) is False


def test_no_disagree_when_only_one_has_barcode():
    """Insufficient signal — keep fuzzy cluster as-is."""
    cluster = [_mk("pharmonline", "4607017950021"), _mk("aptekonline", None)]
    assert _cluster_barcodes_disagree(cluster) is False


def test_ignores_noise_zero():
    cluster = [_mk("pharmonline", "0"), _mk("aptekonline", "4607017950021")]
    # Only one VALID barcode → not enough disagreement signal
    assert _cluster_barcodes_disagree(cluster) is False


def test_ignores_short_barcode():
    cluster = [
        _mk("pharmonline", "123"),
        _mk("aptekonline", "5712345678901"),
    ]
    assert _cluster_barcodes_disagree(cluster) is False


def test_ignores_non_numeric():
    cluster = [
        _mk("pharmonline", "ABCDEFGH"),
        _mk("aptekonline", "5712345678901"),
    ]
    assert _cluster_barcodes_disagree(cluster) is False


def test_three_way_disagree():
    """3+ distinct barcodes — definitely a broken cluster."""
    cluster = [
        _mk("pharmonline", "11111111"),
        _mk("aptekonline", "22222222"),
        _mk("aloe", "33333333"),
    ]
    assert _cluster_barcodes_disagree(cluster) is True
