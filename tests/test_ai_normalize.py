"""Тесты для src/ai_normalize — hash cache, batching, budget, fail-soft."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src import ai_normalize
from src.storage import Product


def _add_product(s, *, site="aloe", name="Aspirin", brand="Bayer", ext_id="x"):
    p = Product(
        site=site,
        external_id=ext_id,
        url=f"https://{site}.az/p/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        brand=brand,
        dosage="100 mg",
        pack_size="30 tab",
    )
    s.add(p)
    s.flush()
    return p


def test_compute_normalize_hash_stable(db_session):
    h1 = ai_normalize.compute_normalize_hash("aloe", "Aspirin", "Bayer", "100 mg", "30 tab")
    h2 = ai_normalize.compute_normalize_hash("aloe", "ASPIRIN", "bayer", "100 mg", "30 tab")
    # case-insensitive
    assert h1 == h2
    # different site → different hash
    h3 = ai_normalize.compute_normalize_hash("pharmonline", "Aspirin", "Bayer", "100 mg", "30 tab")
    assert h1 != h3


def test_estimate_cost_usd_haiku():
    # Haiku: $0.25/$1.25 per 1M tokens
    c = ai_normalize.estimate_cost_usd(
        tokens_in=1_000_000, tokens_out=0,
        provider="anthropic", model="claude-haiku-4-5",
    )
    assert c == pytest.approx(0.25, rel=1e-3)


def test_pending_products_skips_unchanged(db_session):
    """Если normalize_hash совпадает с текущим — продукт не pending."""
    p = _add_product(db_session)
    h = ai_normalize.compute_normalize_hash(p.site, p.name, p.brand, p.dosage, p.pack_size)
    p.normalize_hash = h
    p.normalized_attrs = {"active_ingredient": "acetylsalicylic acid", "confidence": 0.95}
    db_session.commit()

    pending = ai_normalize._pending_products(db_session, site=None, limit=None, force=False)
    assert p not in pending


def test_pending_products_picks_changed_hash(db_session):
    """Если name изменилось — hash другой → pending."""
    p = _add_product(db_session)
    p.normalize_hash = "stale-hash"
    p.normalized_attrs = {"active_ingredient": "x"}
    db_session.commit()

    pending = ai_normalize._pending_products(db_session, site=None, limit=None, force=False)
    assert p in pending


def test_normalize_uses_cache_for_same_hash(db_session):
    """Два продукта с одинаковыми полями → второй берёт attrs из кэша, без LLM."""
    p1 = _add_product(db_session, ext_id="1", site="aloe")
    p2 = _add_product(db_session, ext_id="2", site="aloe")  # same fields, different site? no
    # make them identical (same hash):
    db_session.commit()

    # seed p1 как уже нормализованного
    h = ai_normalize.compute_normalize_hash(p1.site, p1.name, p1.brand, p1.dosage, p1.pack_size)
    p1.normalize_hash = h
    p1.normalized_attrs = {
        "active_ingredient": "acetylsalicylic acid",
        "dosage_mg": 100.0,
        "pack_count": 30,
        "form": "tablet",
        "brand_canonical": "Bayer",
        "is_pharma": True,
        "confidence": 0.95,
    }
    db_session.commit()

    # call_llm_batch must NOT be called when everything cached
    with patch.object(ai_normalize, "call_llm_batch") as mock_llm:
        stats = ai_normalize.normalize_run(db_session, force=False)

    mock_llm.assert_not_called()
    assert stats.products_cached >= 1
    assert stats.products_called == 0

    # p2 should now have normalized_attrs copied from cache
    db_session.refresh(p2)
    assert p2.normalized_attrs is not None
    assert p2.normalized_attrs["active_ingredient"] == "acetylsalicylic acid"


def test_normalize_calls_llm_for_new_product(db_session):
    """Без кэша → батч уходит в LLM."""
    _add_product(db_session, ext_id="new")
    db_session.commit()

    mock_response = [
        {
            "idx": 0,
            "active_ingredient": "acetylsalicylic acid",
            "dosage_mg": 100.0,
            "pack_count": 30,
            "pack_unit": "tab",
            "form": "tablet",
            "brand_canonical": "Bayer",
            "is_pharma": True,
            "confidence": 0.92,
            "needs_review": False,
        }
    ]
    with patch.object(
        ai_normalize, "call_llm_batch", return_value=(mock_response, 200, 100)
    ) as mock_llm:
        stats = ai_normalize.normalize_run(db_session, force=False)

    mock_llm.assert_called_once()
    assert stats.products_called == 1
    assert stats.products_failed == 0
    assert stats.tokens_in == 200
    assert stats.tokens_out == 100
    assert stats.cost_usd > 0

    # Verify product was updated
    p = db_session.scalars(select_product()).first()
    assert p.normalized_attrs["active_ingredient"] == "acetylsalicylic acid"
    assert p.normalized_attrs["confidence"] == 0.92
    assert p.normalized_attrs["needs_review"] is False
    assert p.normalized_at is not None
    assert p.normalize_hash is not None


def test_normalize_needs_review_when_low_confidence(db_session):
    """confidence < 0.7 → needs_review=True даже если модель не выставила."""
    _add_product(db_session, ext_id="unclear")
    db_session.commit()

    mock_response = [
        {
            "idx": 0,
            "active_ingredient": None,
            "dosage_mg": None,
            "pack_count": None,
            "form": None,
            "brand_canonical": None,
            "is_pharma": False,
            "confidence": 0.4,
            "needs_review": False,  # LLM не выставил, но мы должны переписать
        }
    ]
    with patch.object(
        ai_normalize, "call_llm_batch", return_value=(mock_response, 100, 50)
    ):
        ai_normalize.normalize_run(db_session, force=False)

    p = db_session.scalars(select_product()).first()
    assert p.normalized_attrs["needs_review"] is True


def test_normalize_budget_stops_early(db_session):
    """Бюджет исчерпан → следующие батчи пропускаются."""
    for i in range(150):
        _add_product(db_session, ext_id=f"e{i}")
    db_session.commit()

    # Stub LLM с большим cost per batch (300K input, 100K output → $0.2 per batch)
    def fake_llm(batch, *, provider, model):
        return ([{"idx": i, "active_ingredient": "x", "confidence": 0.9} for i in range(len(batch))], 300_000, 100_000)

    with patch.object(ai_normalize, "call_llm_batch", side_effect=fake_llm):
        stats = ai_normalize.normalize_run(
            db_session, batch_size=50, budget_usd=0.20, force=False
        )

    # 150 продуктов / 50 batch = 3 батча
    # Каждый батч ~$0.20 → после первого мы вылетим на проверке pre-flight
    assert stats.budget_exceeded is True
    assert stats.products_called < 150


def test_normalize_fail_soft_on_llm_error(db_session):
    """Если LLM кидает исключение — продукты остаются с null attrs, не падаем."""
    _add_product(db_session, ext_id="bad")
    db_session.commit()

    with patch.object(
        ai_normalize, "call_llm_batch", side_effect=RuntimeError("API down")
    ):
        stats = ai_normalize.normalize_run(db_session, force=False)

    assert stats.products_failed == 1
    assert stats.products_called == 0
    assert any("API down" in f for f in stats.failures)
    p = db_session.scalars(select_product()).first()
    # Атрибутов нет, hash тоже не записан (продукт остался pending для retry)
    assert p.normalized_attrs is None


def test_normalize_force_bypasses_cache(db_session):
    """force=True пересчитывает даже cached продукты."""
    p = _add_product(db_session, ext_id="cached")
    h = ai_normalize.compute_normalize_hash(p.site, p.name, p.brand, p.dosage, p.pack_size)
    p.normalize_hash = h
    p.normalized_attrs = {"active_ingredient": "stale", "confidence": 0.5}
    db_session.commit()

    mock_response = [
        {
            "idx": 0,
            "active_ingredient": "fresh",
            "confidence": 0.95,
            "needs_review": False,
        }
    ]
    with patch.object(
        ai_normalize, "call_llm_batch", return_value=(mock_response, 100, 50)
    ) as mock_llm:
        stats = ai_normalize.normalize_run(db_session, force=True)

    mock_llm.assert_called_once()
    assert stats.products_called == 1
    db_session.refresh(p)
    assert p.normalized_attrs["active_ingredient"] == "fresh"


def test_normalize_limit_caps_pending(db_session):
    """`limit=N` ограничивает количество продуктов в этом прогоне."""
    for i in range(10):
        _add_product(db_session, ext_id=f"l{i}")
    db_session.commit()

    pending = ai_normalize._pending_products(db_session, site=None, limit=3, force=False)
    assert len(pending) == 3


def test_normalize_site_filter(db_session):
    """site='aloe' трогает только aloe-продукты, pharmonline остаются нетронутыми."""
    _add_product(db_session, site="aloe", ext_id="a1")
    _add_product(db_session, site="pharmonline", ext_id="p1")
    db_session.commit()

    pending = ai_normalize._pending_products(
        db_session, site="aloe", limit=None, force=False
    )
    assert len(pending) == 1
    assert pending[0].site == "aloe"


def test_parse_llm_response_strips_markdown_fence():
    """LLM иногда оборачивает JSON в ```json fence — должен распарситься."""
    txt = '```json\n[{"idx": 0, "confidence": 0.9}]\n```'
    out = ai_normalize._parse_llm_response(txt)
    assert out == [{"idx": 0, "confidence": 0.9}]


def test_parse_llm_response_raises_on_garbage():
    with pytest.raises(ValueError):
        ai_normalize._parse_llm_response("no JSON here at all")


def select_product():
    """Helper — общий SELECT для tests, чтобы не повторять import везде."""
    from sqlalchemy import select
    return select(Product).limit(1)
