"""Phase 5.2 — Redis-backed sliding-window rate limiter with per-user tiers.

Previously rate-limiting жил in-process `deque` (см. src/api.py старый
`_check_rate_limit`). Это работало для single-uvicorn-worker эпохи, но
сейчас в проде 2 worker'а — каждый держит свой bucket → effective rate
limit вдвое выше указанного. Плюс при restart api все буфера сбрасываются.

Этот модуль:
1. Использует Redis sorted-set `ratelimit:<key>` для sliding window —
   shared across workers и restart-safe.
2. ZADD timestamp + ZREMRANGEBYSCORE old + ZCARD → атомарно через pipeline.
3. EXPIRE key (window*2) — auto-cleanup ключей которые перестали использоваться.
4. Per-tier limits через `Tier` enum + env config:
       admin: 600 rpm   (full UI workflows + интенсивный browse)
       viewer: 200 rpm  (read-only пользователи, дефолт)
       anon: 30 rpm     (pre-login traffic, не привязано к user_id)
5. Fallback на in-memory deque если Redis unreachable — degradation
   gracefully, API не падает.

API:
    from src.rate_limit import check_rate_limit, Tier
    check_rate_limit("user:42", tier=Tier.ADMIN)
    # raises HTTPException(429) если превышено

См. tests/test_rate_limit.py — unit-тесты на оба бэкенда (Redis mocked +
in-memory fallback).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog
from fastapi import HTTPException

log = structlog.get_logger(__name__)


# ─── Tier configuration ──────────────────────────────────────────────────────


class Tier(str, Enum):
    """Per-user rate-limit tier — выбирается из user.role на момент запроса."""

    ADMIN = "admin"
    VIEWER = "viewer"
    ANON = "anon"  # pre-login или unauthenticated (login, auth/request)


@dataclass(frozen=True)
class TierConfig:
    """Sliding-window limit для одного tier."""

    limit: int  # requests
    window_sec: int  # per window


def _load_tier_configs() -> dict[Tier, TierConfig]:
    """Загружает per-tier лимиты из env с дефолтами.

    Env vars (все int rpm):
        PHARMACY_API_RATE_LIMIT_ADMIN_RPM  (default 600)
        PHARMACY_API_RATE_LIMIT_VIEWER_RPM (default 200)
        PHARMACY_API_RATE_LIMIT_ANON_RPM   (default 30)

    Также читает legacy `PHARMACY_API_RATE_LIMIT_RPM` как fallback для viewer
    (backwards-compat с предыдущей версией кода).
    """
    legacy = os.environ.get("PHARMACY_API_RATE_LIMIT_RPM")
    viewer_default = int(legacy) if legacy else 200
    return {
        Tier.ADMIN: TierConfig(int(os.environ.get("PHARMACY_API_RATE_LIMIT_ADMIN_RPM", "600")), 60),
        Tier.VIEWER: TierConfig(
            int(os.environ.get("PHARMACY_API_RATE_LIMIT_VIEWER_RPM", str(viewer_default))),
            60,
        ),
        Tier.ANON: TierConfig(int(os.environ.get("PHARMACY_API_RATE_LIMIT_ANON_RPM", "30")), 60),
    }


_TIER_CONFIGS = _load_tier_configs()


# ─── Redis backend ────────────────────────────────────────────────────────────


def _redis_client() -> Any | None:
    """Lazy Redis client. None если REDIS_URL не задан или Redis недоступен.

    Codex review fix (2026-05-28): прежняя версия делала single-shot probe —
    если первый connect упал, worker НАВСЕГДА оставался на memory fallback.
    Теперь после failure пробуем переподключиться раз в `_REDIS_REPROBE_SEC`
    (30s по умолчанию). Это устраняет "fail once, fail forever" поведение,
    но не делает hot retry на каждый запрос (защита от storms).
    """
    global _REDIS_CACHED, _REDIS_LAST_FAILURE_TS
    now = time.time()
    if _REDIS_CACHED is not None:
        return _REDIS_CACHED
    # Recent failure → не пробуем снова до окончания backoff'а
    if _REDIS_LAST_FAILURE_TS and now - _REDIS_LAST_FAILURE_TS < _REDIS_REPROBE_SEC:
        return None
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis as _redis

        client = _redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        _REDIS_CACHED = client
        _REDIS_LAST_FAILURE_TS = 0.0  # success — reset backoff
        return client
    except Exception as e:  # noqa: BLE001
        log.warning("rate_limit_redis_unreachable", error=str(e))
        _REDIS_LAST_FAILURE_TS = now
        return None


_REDIS_CACHED: Any | None = None
_REDIS_LAST_FAILURE_TS: float = 0.0
_REDIS_REPROBE_SEC = 30  # ждать N секунд между неудачными probe'ами


def _reset_redis_probe_for_tests() -> None:
    """Test-only helper — сбрасывает cached client между тестами."""
    global _REDIS_CACHED, _REDIS_LAST_FAILURE_TS
    _REDIS_CACHED = None
    _REDIS_LAST_FAILURE_TS = 0.0


# ─── In-memory fallback ──────────────────────────────────────────────────────
# Self-review fix (2026-05-28): чтобы избежать cardinality-leak от malicious
# clients (100k уникальных IP-keys → 100k пустых deque), периодически чистим
# `_MEMORY_BUCKETS` от ключей с пустой deque (т.е. с window-prune прошло
# всё содержимое). Чистка происходит лениво раз в `_GC_INTERVAL_SEC` секунд.

_MEMORY_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
# Codex review fix #2 (2026-05-28): отдельный last-seen tracker per key.
# Раньше GC ждал `default_window_sec=3600` даже когда реальный window=60s →
# атакер-keys линджились час. Теперь evict через `2 * actual_window_sec`.
_MEMORY_LAST_SEEN: dict[str, float] = {}
_MEMORY_LAST_WINDOW: dict[str, int] = {}
_GC_INTERVAL_SEC = 60
_GC_LAST_RUN: list[float] = [0.0]  # mutable container для thread-safe-ish access


def _gc_memory_buckets(now: float) -> None:
    """Чистит stale ключи из `_MEMORY_BUCKETS`.

    Стратегия: для каждого ключа удаляем если `now - last_seen > 2*window`.
    Множитель 2× — safety margin против spurious evict'а активной key между
    запросами. Это устраняет cardinality leak от unique IPs / user_ids
    которые отстрелялись и больше не возвращаются.

    Идемпотентно. Throttle'ится через `_GC_LAST_RUN`.
    """
    if now - _GC_LAST_RUN[0] < _GC_INTERVAL_SEC:
        return
    _GC_LAST_RUN[0] = now
    # Iterate over copy of keys чтобы dict mutation во время iteration не падал
    for k in list(_MEMORY_BUCKETS.keys()):
        last_seen = _MEMORY_LAST_SEEN.get(k, 0.0)
        window = _MEMORY_LAST_WINDOW.get(k, 60)
        if now - last_seen > 2 * window:
            _MEMORY_BUCKETS.pop(k, None)
            _MEMORY_LAST_SEEN.pop(k, None)
            _MEMORY_LAST_WINDOW.pop(k, None)


def _memory_check(key: str, limit: int, window_sec: int) -> int:
    """Sliding-window через deque. Возвращает текущий count (для логов)."""
    now = time.time()
    bucket = _MEMORY_BUCKETS[key]
    while bucket and bucket[0] < now - window_sec:
        bucket.popleft()
    if len(bucket) >= limit:
        retry_after = max(1, int(window_sec - (now - bucket[0])))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} req per {window_sec}s)",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)
    # Track per-key window для accurate GC
    _MEMORY_LAST_SEEN[key] = now
    _MEMORY_LAST_WINDOW[key] = window_sec
    # Lazy GC старых ключей (раз в минуту максимум — не на каждый запрос)
    _gc_memory_buckets(now)
    return len(bucket)


def _reset_memory_for_tests() -> None:
    """Test-only — сбрасывает in-memory buckets между тестами."""
    _MEMORY_BUCKETS.clear()
    _MEMORY_LAST_SEEN.clear()
    _MEMORY_LAST_WINDOW.clear()
    _GC_LAST_RUN[0] = 0.0


# ─── Redis sliding-window ─────────────────────────────────────────────────────


def _redis_check(client: Any, key: str, limit: int, window_sec: int) -> int:
    """ZSET-based sliding window. Atomic через MULTI/EXEC pipeline.

    Реализация:
      1. ZREMRANGEBYSCORE   key  0  (now - window) → удаляет старое
      2. ZCARD              key                    → текущий count
      3. ZADD               key  now → member=uuid (используем now+inc)
      4. EXPIRE             key  window*2          → auto-cleanup

    Решение проверять count ДО ZADD: иначе race может пропустить limit на 1.
    Поскольку проверка count в pipeline вместе с ZADD не атомарна (pipeline
    EXEC'ит всё, а потом мы читаем результат), мы используем post-check —
    если ZCARD после ZADD > limit, откатываем ZADD через ZREM и raise 429.
    """
    redis_key = f"ratelimit:{key}"
    now_ms = int(time.time() * 1000)
    window_ms = window_sec * 1000
    member = f"{now_ms}-{os.urandom(4).hex()}"  # unique per request

    pipe = client.pipeline(transaction=True)
    pipe.zremrangebyscore(redis_key, 0, now_ms - window_ms)
    pipe.zadd(redis_key, {member: now_ms})
    pipe.zcard(redis_key)
    pipe.expire(redis_key, window_sec * 2)
    results = pipe.execute()
    count = int(results[2])

    if count > limit:
        # Откатываем — мы попали поверх лимита
        try:
            client.zrem(redis_key, member)
        except Exception:  # noqa: BLE001
            pass
        # Узнаём oldest для Retry-After
        try:
            oldest_score = client.zrange(redis_key, 0, 0, withscores=True)
            if oldest_score:
                oldest_ms = int(oldest_score[0][1])
                retry_after = max(1, int((oldest_ms + window_ms - now_ms) / 1000))
            else:
                retry_after = 1
        except Exception:  # noqa: BLE001
            retry_after = window_sec
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} req per {window_sec}s)",
            headers={"Retry-After": str(retry_after)},
        )
    return count


# ─── Public API ───────────────────────────────────────────────────────────────


def check_rate_limit(
    key: str,
    *,
    tier: Tier = Tier.VIEWER,
    limit: int | None = None,
    window_sec: int | None = None,
) -> int:
    """Проверяет rate-limit для данного key + tier (или explicit limit).

    Args:
        key: уникальный идентификатор клиента (user_id, ip, api_key prefix).
        tier: Tier из enum. Игнорируется если limit явно передан.
        limit: явный override (обходит tier config). Пара с window_sec.
        window_sec: окно для явного override.

    Returns:
        Current count после успешной регистрации (для логов).

    Raises:
        HTTPException(429) если лимит превышен. Включает Retry-After header.
    """
    if limit is None:
        cfg = _TIER_CONFIGS[tier]
        limit, window_sec = cfg.limit, cfg.window_sec
    elif window_sec is None:
        window_sec = 60

    client = _redis_client()
    if client is None:
        return _memory_check(key, limit, window_sec)
    try:
        return _redis_check(client, key, limit, window_sec)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # Redis упал в середине запроса — degrade на memory, не валим API
        log.warning("rate_limit_redis_op_failed", key=key, error=str(e))
        return _memory_check(key, limit, window_sec)


def tier_for_user(role: str | None) -> Tier:
    """Маппинг role → Tier.

    Принимает None / unknown → ANON (защита от пропавших ролей в БД).
    """
    if role == "admin":
        return Tier.ADMIN
    if role == "viewer":
        return Tier.VIEWER
    return Tier.ANON
