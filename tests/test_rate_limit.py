"""Unit tests для src.rate_limit (Phase 5.2).

Covers:
  - In-memory fallback sliding window (когда REDIS_URL не задан или Redis down)
  - Per-tier limits (admin > viewer > anon)
  - tier_for_user mapping
  - Explicit limit override (для auth endpoints)
  - Retry-After header при 429
  - Redis backend через mock client (без реальной зависимости)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src import rate_limit as rl


@pytest.fixture(autouse=True)
def _reset_state():
    """Свежее состояние between тестами — clear memory buckets + redis cache."""
    rl._reset_memory_for_tests()
    rl._reset_redis_probe_for_tests()
    yield
    rl._reset_memory_for_tests()
    rl._reset_redis_probe_for_tests()


@pytest.fixture
def no_redis(monkeypatch):
    """Принудительно отключаем Redis — все тесты идут через memory backend."""
    monkeypatch.delenv("REDIS_URL", raising=False)


# ─── In-memory backend ───────────────────────────────────────────────────────


def test_memory_check_under_limit_returns_count(no_redis):
    """3 запроса при лимите 10 → не raise, count растёт 1→2→3."""
    for expected_count in (1, 2, 3):
        c = rl.check_rate_limit("test-key", limit=10, window_sec=60)
        assert c == expected_count


def test_memory_check_at_limit_raises_429(no_redis):
    """11-й запрос при лимите 10 → 429 + Retry-After."""
    for _ in range(10):
        rl.check_rate_limit("burst", limit=10, window_sec=60)
    with pytest.raises(HTTPException) as exc:
        rl.check_rate_limit("burst", limit=10, window_sec=60)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_memory_keys_isolated(no_redis):
    """Один key исчерпан, другой ещё свободен."""
    for _ in range(5):
        rl.check_rate_limit("alice", limit=5, window_sec=60)
    with pytest.raises(HTTPException):
        rl.check_rate_limit("alice", limit=5, window_sec=60)
    # Боб не пострадал
    assert rl.check_rate_limit("bob", limit=5, window_sec=60) == 1


def test_memory_window_expires(no_redis, monkeypatch):
    """После window_sec старые записи вываливаются — счётчик сбрасывается."""
    # Используем короткое окно + sleep
    rl.check_rate_limit("expire-key", limit=2, window_sec=1)
    rl.check_rate_limit("expire-key", limit=2, window_sec=1)
    with pytest.raises(HTTPException):
        rl.check_rate_limit("expire-key", limit=2, window_sec=1)
    time.sleep(1.05)
    # Окно прошло → можно снова
    assert rl.check_rate_limit("expire-key", limit=2, window_sec=1) == 1


# ─── Tier configuration ─────────────────────────────────────────────────────


def test_admin_tier_has_higher_limit_than_viewer(no_redis, monkeypatch):
    """Admin limit > viewer limit > anon limit (with defaults)."""
    monkeypatch.delenv("PHARMACY_API_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("PHARMACY_API_RATE_LIMIT_ADMIN_RPM", raising=False)
    monkeypatch.delenv("PHARMACY_API_RATE_LIMIT_VIEWER_RPM", raising=False)
    monkeypatch.delenv("PHARMACY_API_RATE_LIMIT_ANON_RPM", raising=False)
    # Force re-load of tier configs
    rl._TIER_CONFIGS = rl._load_tier_configs()
    assert rl._TIER_CONFIGS[rl.Tier.ADMIN].limit > rl._TIER_CONFIGS[rl.Tier.VIEWER].limit
    assert rl._TIER_CONFIGS[rl.Tier.VIEWER].limit > rl._TIER_CONFIGS[rl.Tier.ANON].limit


def test_tier_for_user_mapping():
    assert rl.tier_for_user("admin") == rl.Tier.ADMIN
    assert rl.tier_for_user("viewer") == rl.Tier.VIEWER
    assert rl.tier_for_user(None) == rl.Tier.ANON
    assert rl.tier_for_user("unknown_role") == rl.Tier.ANON


def test_tier_picks_different_limit(no_redis, monkeypatch):
    """Anon tier hits 429 быстрее чем admin при одинаковом потоке."""
    monkeypatch.setenv("PHARMACY_API_RATE_LIMIT_ADMIN_RPM", "100")
    monkeypatch.setenv("PHARMACY_API_RATE_LIMIT_ANON_RPM", "3")
    rl._TIER_CONFIGS = rl._load_tier_configs()

    # Anon — 4-й запрос упадёт
    for _ in range(3):
        rl.check_rate_limit("anon:1.2.3.4", tier=rl.Tier.ANON)
    with pytest.raises(HTTPException):
        rl.check_rate_limit("anon:1.2.3.4", tier=rl.Tier.ANON)

    # Admin — 4 запроса проходят свободно
    for _ in range(4):
        rl.check_rate_limit("user:42", tier=rl.Tier.ADMIN)


def test_legacy_rpm_env_used_for_viewer(no_redis, monkeypatch):
    """`PHARMACY_API_RATE_LIMIT_RPM` (legacy) → viewer tier default."""
    monkeypatch.setenv("PHARMACY_API_RATE_LIMIT_RPM", "777")
    monkeypatch.delenv("PHARMACY_API_RATE_LIMIT_VIEWER_RPM", raising=False)
    rl._TIER_CONFIGS = rl._load_tier_configs()
    assert rl._TIER_CONFIGS[rl.Tier.VIEWER].limit == 777


# ─── Explicit limit override ────────────────────────────────────────────────


def test_explicit_limit_overrides_tier(no_redis):
    """`check_rate_limit(key, limit=2)` игнорирует tier."""
    rl.check_rate_limit("auth_req:1.1.1.1", limit=2, window_sec=60)
    rl.check_rate_limit("auth_req:1.1.1.1", limit=2, window_sec=60)
    with pytest.raises(HTTPException):
        rl.check_rate_limit("auth_req:1.1.1.1", limit=2, window_sec=60)


# ─── Redis backend (mocked) ─────────────────────────────────────────────────


def _mock_redis_pipeline(card_after_zadd: int):
    """Helper: возвращает mock client чей pipeline().execute() даёт ZCARD=card."""
    client = MagicMock()
    pipe = MagicMock()
    pipe.execute.return_value = [0, 1, card_after_zadd, True]  # zrem, zadd, zcard, expire
    client.pipeline.return_value = pipe
    client.zrange.return_value = []
    return client


def test_redis_backend_used_when_client_available(monkeypatch):
    """Если _redis_client() возвращает client — идём в Redis backend."""
    fake = _mock_redis_pipeline(card_after_zadd=1)
    monkeypatch.setattr(rl, "_redis_client", lambda: fake)
    count = rl.check_rate_limit("key", limit=10, window_sec=60)
    assert count == 1
    # ZADD/ZCARD были вызваны
    fake.pipeline.assert_called_once()


def test_redis_backend_429_rollbacks_zadd(monkeypatch):
    """Если ZCARD > limit → ZREM откатывается, raise 429."""
    fake = _mock_redis_pipeline(card_after_zadd=11)
    fake.zrange.return_value = [(b"old-member", float(int(time.time() * 1000)) - 30000)]
    monkeypatch.setattr(rl, "_redis_client", lambda: fake)
    with pytest.raises(HTTPException) as exc:
        rl.check_rate_limit("key", limit=10, window_sec=60)
    assert exc.value.status_code == 429
    fake.zrem.assert_called_once()


def test_redis_op_failure_falls_back_to_memory(monkeypatch):
    """Если pipeline.execute падает → используем memory backend, не 5xx."""
    broken: Any = MagicMock()
    broken.pipeline.side_effect = RuntimeError("connection refused mid-flight")
    monkeypatch.setattr(rl, "_redis_client", lambda: broken)
    # Не должен raise — должен молча перейти на memory backend
    count = rl.check_rate_limit("key-fail", limit=10, window_sec=60)
    assert count == 1


# ─── Integration: ensure _redis_client respects REDIS_URL ─────────────────


def test_redis_client_returns_none_without_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert rl._redis_client() is None


def test_redis_reprobes_after_backoff(monkeypatch):
    """Codex review fix #1: после неудачной попытки connect — re-probe через backoff."""
    monkeypatch.setenv("REDIS_URL", "redis://nope:6379")

    # Mock первого probe — fail
    call_count = {"n": 0}

    def fake_from_url(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("first probe fails")
        # Второй вызов — success
        client = MagicMock()
        client.ping.return_value = True
        return client

    import redis as _redis_module

    monkeypatch.setattr(_redis_module, "from_url", fake_from_url)

    # Первый probe → fail, должен set _REDIS_LAST_FAILURE_TS
    assert rl._redis_client() is None
    assert rl._REDIS_LAST_FAILURE_TS > 0

    # Сразу повторный вызов — НЕ должен пытаться re-connect (внутри backoff window)
    assert rl._redis_client() is None
    assert call_count["n"] == 1  # ещё не пробовали снова

    # Симулируем что backoff прошёл
    rl._REDIS_LAST_FAILURE_TS = time.time() - rl._REDIS_REPROBE_SEC - 1
    # Теперь должен попытаться снова — и success
    client = rl._redis_client()
    assert client is not None
    assert call_count["n"] == 2


# ─── Self-review fix: memory leak GC ────────────────────────────────────────


def test_memory_gc_drops_stale_buckets(no_redis, monkeypatch):
    """Codex review fix #2 (2026-05-28): GC evicts keys через `2*window_sec`
    после последнего обращения.

    Раньше threshold был жёстко 3600s. Теперь GC использует per-key window —
    rate-limit window=60s → eviction после 120s бездействия.

    Сценарий cardinality bomb: атакующий шлёт N уникальных IP-keys.
    """
    # 50 keys с коротким window
    for i in range(50):
        rl.check_rate_limit(f"unique-ip-{i}", limit=100, window_sec=60)
    assert len(rl._MEMORY_BUCKETS) == 50

    # Симулируем будущее > 2 * window_sec
    rl._GC_LAST_RUN[0] = 0.0
    future_now = time.time() + 200  # > 2*60s
    rl._gc_memory_buckets(future_now)

    # Все 50 stale keys должны быть удалены
    assert rl._MEMORY_BUCKETS == {}, (
        f"Expected all stale keys removed, got {len(rl._MEMORY_BUCKETS)}"
    )
    # И tracker-dicts тоже почистились
    assert rl._MEMORY_LAST_SEEN == {}
    assert rl._MEMORY_LAST_WINDOW == {}


def test_memory_gc_keeps_active_buckets(no_redis):
    """GC не трогает ключи которые видели запрос недавно."""
    rl.check_rate_limit("active-key", limit=10, window_sec=60)
    rl._GC_LAST_RUN[0] = 0.0
    rl._gc_memory_buckets(time.time())  # сейчас — недавно был запрос
    # Запись свежая → ключ сохраняется
    assert "active-key" in rl._MEMORY_BUCKETS


def test_memory_gc_uses_per_key_window(no_redis):
    """Keys с разными window'ами эvict'ятся по своему window'у."""
    rl.check_rate_limit("short-window", limit=10, window_sec=10)
    rl.check_rate_limit("long-window", limit=10, window_sec=3600)
    rl._GC_LAST_RUN[0] = 0.0
    # Симулируем 30s в будущем (>2*10=20s но <<2*3600=7200s)
    future_now = time.time() + 30
    rl._gc_memory_buckets(future_now)
    # short-window evicted, long-window survives
    assert "short-window" not in rl._MEMORY_BUCKETS
    assert "long-window" in rl._MEMORY_BUCKETS


def test_memory_gc_throttled_to_interval(no_redis, monkeypatch):
    """GC не выполняется на каждом вызове — только раз в _GC_INTERVAL_SEC."""
    rl._GC_LAST_RUN[0] = time.time()  # GC только что был
    rl.check_rate_limit("k1", limit=10, window_sec=60)
    # Создадим stale ключ напрямую
    rl._MEMORY_BUCKETS["stale"] = type(rl._MEMORY_BUCKETS["k1"])()  # empty deque
    # Ещё один check — GC не должен сработать (интервал не прошёл)
    rl.check_rate_limit("k2", limit=10, window_sec=60)
    assert "stale" in rl._MEMORY_BUCKETS, "GC fired too eagerly (should throttle)"
