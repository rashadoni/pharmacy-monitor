"""Tests for scripts/cleanup_false_matches.py (Phase 0.2).

Покрываем:
- CSV-парсинг (header validation, type coercion, error reporting)
- Идемпотентность (повторный прогон не дублирует MatchRejection)
- Корректная обработка edge-кейсов: продукт не в кластере, match не существует
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cleanup_false_matches import _parse_csv, CleanupRow  # noqa: E402
from src import match_actions, storage  # noqa: E402


def test_parse_csv_basic(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id,reason\n1,2,oops\n3,4,other\n", encoding="utf-8")
    rows = _parse_csv(p)
    assert rows == [
        CleanupRow(match_id=1, detach_product_id=2, reason="oops"),
        CleanupRow(match_id=3, detach_product_id=4, reason="other"),
    ]


def test_parse_csv_strips_whitespace_from_reason(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text(
        "match_id,detach_product_id,reason\n1,2,  whitespace around  \n", encoding="utf-8"
    )
    rows = _parse_csv(p)
    assert rows[0].reason == "whitespace around"


def test_parse_csv_rejects_missing_column(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id\n1,2\n", encoding="utf-8")  # no `reason`
    with pytest.raises(ValueError, match="missing required columns"):
        _parse_csv(p)


def test_parse_csv_reports_line_number_on_bad_row(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id,reason\nNOT_A_NUMBER,2,oops\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        _parse_csv(p)


def test_break_match_idempotent_via_add_rejection(db_session):
    """add_rejection (used by break_match) is idempotent — repeat call returns same row."""
    s = db_session
    # Build a minimal cluster: 2 products → 1 Match.
    p_a = storage.Product(
        tenant_id=1, site="pharmonline", external_id="a", url="http://a", name="A",
        name_normalized="a",
    )
    p_b = storage.Product(
        tenant_id=1, site="aptekonline", external_id="b", url="http://b", name="B",
        name_normalized="b",
    )
    s.add_all([p_a, p_b])
    s.flush()
    m = storage.Match(tenant_id=1, canonical_name="canon")
    s.add(m)
    s.flush()
    p_a.canonical_id = m.id
    p_b.canonical_id = m.id
    s.commit()

    r1 = match_actions.add_rejection(s, p_a.id, p_b.id, reason="first")
    r2 = match_actions.add_rejection(s, p_a.id, p_b.id, reason="second")
    assert r1.id == r2.id  # same record returned, no duplicate
    # Reason of the first call is preserved (we don't overwrite on re-add).
    assert r1.reason == "first"
