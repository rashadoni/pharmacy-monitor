from types import SimpleNamespace

import scripts.reconcile_pharmonline_public_api_identities as reconciliation
from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE


async def test_reconciliation_cools_down_after_empty_decodo_sitemap(monkeypatch):
    rejected = SimpleNamespace(
        site_fatal=False,
        errors=(),
        products=(),
        route_statuses={
            PUBLIC_CATALOG_ROUTE: SimpleNamespace(
                complete=False,
                abort_reason="product_sitemap_empty",
            )
        },
    )
    accepted = SimpleNamespace(
        site_fatal=False,
        errors=(),
        products=[object()],
        route_statuses={
            PUBLIC_CATALOG_ROUTE: SimpleNamespace(
                complete=True,
                expected_items=1,
                abort_reason=None,
            )
        },
    )
    results = iter((rejected, accepted))

    class FakeScraper:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def scrape(self, routes):
            assert routes == [PUBLIC_CATALOG_ROUTE]
            return next(results)

    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(reconciliation, "PharmonlinePublicAPIScraper", FakeScraper)
    monkeypatch.setattr(reconciliation.asyncio, "sleep", fake_sleep)

    result = await reconciliation.read_catalog_pass("first pass")

    assert result is accepted
    assert delays == [300]
