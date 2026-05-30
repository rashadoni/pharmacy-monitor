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
    results = asyncio.run(link_validator.check_urls(items, client=client))
    asyncio.run(client.aclose())
    assert results == {1: "alive", 2: "dead", 3: "error"}


def test_check_urls_network_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = asyncio.run(link_validator.check_urls([(1, "http://x/a")], client=client))
    asyncio.run(client.aclose())
    assert results == {1: "error"}


def test_check_urls_empty():
    assert asyncio.run(link_validator.check_urls([])) == {}


def _mk(s, ext, url_dead_at=None):
    p = storage.Product(
        site="aptekonline", external_id=ext, url=f"http://x/{ext}",
        name=ext, name_normalized=ext.lower(), url_dead_at=url_dead_at,
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
