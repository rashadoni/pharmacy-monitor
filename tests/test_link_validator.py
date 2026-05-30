"""Тесты валидатора мёртвых ссылок (Product.url_dead_at)."""

import asyncio

import httpx

from src import link_validator, storage
from src._time import utcnow


def test_classify():
    assert link_validator.classify(404) == "dead"
    assert link_validator.classify(410) == "dead"
    assert link_validator.classify(451) == "dead"
    assert link_validator.classify(200) == "alive"
    assert link_validator.classify(301) == "alive"  # <400 = alive
    # транзиент — НЕ помечаем мёртвым/живым
    assert link_validator.classify(403) == "error"
    assert link_validator.classify(429) == "error"
    assert link_validator.classify(500) == "error"
    assert link_validator.classify(None) == "error"


def _client(routes):
    def handler(request):
        return httpx.Response(routes.get(request.url.path, 200))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_check_urls_mixed():
    client = _client({"/a": 200, "/b": 404, "/c": 500})
    items = [(1, "http://x/a"), (2, "http://x/b"), (3, "http://x/c")]
    results, meta = asyncio.run(link_validator.check_urls(items, client=client, rate_per_sec=0))
    asyncio.run(client.aclose())
    assert results == {1: "alive", 2: "dead", 3: "error"}
    assert meta == {"checked": 3, "aborted": False}


def test_check_urls_network_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results, _ = asyncio.run(
        link_validator.check_urls([(1, "http://x/a")], client=client, rate_per_sec=0)
    )
    asyncio.run(client.aclose())
    assert results == {1: "error"}


def test_check_urls_empty():
    results, meta = asyncio.run(link_validator.check_urls([]))
    assert results == {} and meta["checked"] == 0


def test_check_urls_circuit_breaker_aborts():
    """Серия 'error' (403=бан-сигнал) → circuit-breaker прерывает прогон."""

    def handler(request):
        return httpx.Response(403)  # все error

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items = [(i, f"http://x/{i}") for i in range(30)]
    results, meta = asyncio.run(
        link_validator.check_urls(
            items, client=client, concurrency=1, rate_per_sec=0, circuit_breaker=5
        )
    )
    asyncio.run(client.aclose())
    assert meta["aborted"] is True
    assert len(results) < 30  # оставшиеся НЕ дёрнуты (не углубляем бан)


def test_check_urls_head_fallback_to_get():
    """HEAD 405 (не поддержан) → fallback GET → честный статус."""

    def handler(request):
        return httpx.Response(405 if request.method == "HEAD" else 404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results, _ = asyncio.run(
        link_validator.check_urls([(1, "http://x/a")], client=client, rate_per_sec=0)
    )
    asyncio.run(client.aclose())
    assert results == {1: "dead"}  # HEAD 405 → GET 404 → dead


def test_check_urls_no_get_on_ban_code():
    """HEAD 403 (бан) НЕ добивается GET'ом — только error."""
    seen = {"methods": []}

    def handler(request):
        seen["methods"].append(request.method)
        return httpx.Response(403)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results, _ = asyncio.run(
        link_validator.check_urls([(1, "http://x/a")], client=client, rate_per_sec=0)
    )
    asyncio.run(client.aclose())
    assert results == {1: "error"}
    assert seen["methods"] == ["HEAD"]  # GET НЕ делался


def test_dead_fraction():
    assert link_validator.dead_fraction({}) == 0.0
    assert link_validator.dead_fraction({1: "dead", 2: "alive", 3: "alive", 4: "dead"}) == 0.5
    assert link_validator.dead_fraction({1: "alive"}) == 0.0


def _mk(s, ext, url_dead_at=None):
    p = storage.Product(
        site="aptekonline",
        external_id=ext,
        url=f"http://x/{ext}",
        name=ext,
        name_normalized=ext.lower(),
        url_dead_at=url_dead_at,
    )
    s.add(p)
    s.flush()
    return p


def test_apply_results(db_session):
    s = db_session
    now = utcnow()
    p_alive = _mk(s, "alive")
    p_to_die = _mk(s, "todie")
    p_err = _mk(s, "err")
    p_revive = _mk(s, "revive", url_dead_at=now)  # уже мёртв → станет жив
    s.commit()

    results = {
        p_alive.id: "alive",
        p_to_die.id: "dead",
        p_err.id: "error",
        p_revive.id: "alive",
    }
    counts = link_validator.apply_results(s, results, now)
    s.commit()

    assert p_alive.url_dead_at is None
    assert p_to_die.url_dead_at == now  # newly dead
    assert p_err.url_dead_at is None  # error → не трогаем
    assert p_revive.url_dead_at is None  # revived
    assert counts["newly_dead"] == 1
    assert counts["revived"] == 1
    assert counts["error"] == 1
    assert counts["still_dead"] == 0


def test_apply_results_still_dead(db_session):
    s = db_session
    now = utcnow()
    p = _mk(s, "stilldead", url_dead_at=now)
    s.commit()
    counts = link_validator.apply_results(s, {p.id: "dead"}, now)
    assert counts["still_dead"] == 1
    assert counts["newly_dead"] == 0
    assert p.url_dead_at == now
