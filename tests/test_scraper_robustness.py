"""Tests for W10 scraper robustness primitives.

These don't actually launch a browser — they verify the helpers and the
configuration plumbing that BaseScraper relies on (UA rotation, viewport,
proxy URL redaction, captcha pattern matching).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scrapers import anti_detection, base, captcha


# ─── User-Agent pool ────────────────────────────────────────────────────────


def test_user_agents_pool_size():
    """At least 5 UAs to ensure good rotation."""
    assert len(anti_detection.USER_AGENTS_EXTENDED) >= 5


def test_random_user_agent_returns_string():
    ua = anti_detection.random_user_agent()
    assert isinstance(ua, str)
    assert "Mozilla/5.0" in ua


def test_random_user_agent_eventually_varies():
    """1000 calls should produce >1 distinct UA (probabilistic, but ~certain)."""
    seen = {anti_detection.random_user_agent() for _ in range(1000)}
    assert len(seen) > 1


# ─── Viewport randomization ─────────────────────────────────────────────────


def test_random_viewport_returns_realistic_size():
    vp = anti_detection.random_viewport()
    assert "width" in vp and "height" in vp
    # Common desktop range
    assert 1200 <= vp["width"] <= 2000
    assert 700 <= vp["height"] <= 1200


def test_random_viewport_jitter_changes_value():
    """Multiple calls should produce different exact sizes (due to ±10 jitter)."""
    sizes = {(vp := anti_detection.random_viewport()) and (vp["width"], vp["height"]) for _ in range(100)}
    assert len(sizes) > 5


# ─── Proxy URL redaction ────────────────────────────────────────────────────


def test_redact_proxy_with_credentials():
    assert base._redact_proxy("http://user:pass@proxy.host:8080") == "http://***@proxy.host:8080"


def test_redact_proxy_without_credentials():
    """No '@' → no redaction needed, return as-is."""
    assert base._redact_proxy("http://proxy.host:8080") == "http://proxy.host:8080"


def test_redact_proxy_no_scheme():
    assert base._redact_proxy("user:pass@host:80") == "***@host:80"


# ─── ScraperAPI per-site proxy config ───────────────────────────────────────


def test_scraperapi_proxy_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("SCRAPER_API_KEY", raising=False)
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline")
    assert base._scraperapi_proxy_for("pharmonline") is None


def test_scraperapi_proxy_returns_none_when_site_not_listed(monkeypatch):
    monkeypatch.setenv("SCRAPER_API_KEY", "test_key_123")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    assert base._scraperapi_proxy_for("aloe") is None


def test_scraperapi_proxy_default_country(monkeypatch):
    monkeypatch.setenv("SCRAPER_API_KEY", "test_key_123")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    monkeypatch.delenv("SCRAPER_API_COUNTRY", raising=False)

    cfg = base._scraperapi_proxy_for("pharmonline")
    assert cfg == {
        "server": "http://proxy-server.scraperapi.com:8001",
        "username": "scraperapi",
        "password": "test_key_123",
    }


def test_scraperapi_proxy_with_country(monkeypatch):
    monkeypatch.setenv("SCRAPER_API_KEY", "test_key_123")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "tr")

    cfg = base._scraperapi_proxy_for("aptekonline")
    assert cfg["username"] == "scraperapi.country_code=tr"
    assert cfg["password"] == "test_key_123"


def test_scraperapi_proxy_handles_whitespace_in_csv(monkeypatch):
    monkeypatch.setenv("SCRAPER_API_KEY", "test_key_123")
    monkeypatch.setenv("SCRAPER_API_SITES", " pharmonline ,  aptekonline ")
    assert base._scraperapi_proxy_for("pharmonline") is not None
    assert base._scraperapi_proxy_for("aptekonline") is not None
    assert base._scraperapi_proxy_for("aloe") is None


# ─── IPRoyal residential (Phase 1.2b) ───────────────────────────────────────


def _clear_iproyal_env(monkeypatch):
    for k in ("IPROYAL_USERNAME", "IPROYAL_PASSWORD", "IPROYAL_SITES",
              "IPROYAL_HOST", "IPROYAL_COUNTRY"):
        monkeypatch.delenv(k, raising=False)


def test_iproyal_proxy_returns_none_without_username(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_PASSWORD", "secret")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    assert base._iproyal_proxy_for("pharmonline") is None


def test_iproyal_proxy_returns_none_without_password(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "user1")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    assert base._iproyal_proxy_for("pharmonline") is None


def test_iproyal_proxy_skips_site_not_in_list(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "user1")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    assert base._iproyal_proxy_for("aloe") is None


def test_iproyal_proxy_default_endpoint(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "user1")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    cfg = base._iproyal_proxy_for("pharmonline")
    assert cfg == {
        "server": "http://geo.iproyal.com:12321",
        "username": "user1",
        "password": "p",
    }


def test_iproyal_proxy_appends_country(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "user1")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    monkeypatch.setenv("IPROYAL_COUNTRY", "tr")
    cfg = base._iproyal_proxy_for("pharmonline")
    # IPRoyal uses underscore syntax, not dash
    assert cfg["username"] == "user1_country-tr"


def test_iproyal_proxy_does_not_double_country(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "user1_country-az")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    monkeypatch.setenv("IPROYAL_COUNTRY", "tr")
    cfg = base._iproyal_proxy_for("pharmonline")
    assert cfg["username"] == "user1_country-az"


def test_iproyal_proxy_custom_host(monkeypatch):
    _clear_iproyal_env(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "u")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    monkeypatch.setenv("IPROYAL_HOST", "premium.iproyal.com:6000")
    cfg = base._iproyal_proxy_for("pharmonline")
    assert cfg["server"] == "http://premium.iproyal.com:6000"


# ─── Bright Data residential (Phase 1.2) ────────────────────────────────────


def _clear_brightdata_env(monkeypatch):
    for k in ("BRIGHTDATA_USERNAME", "BRIGHTDATA_PASSWORD", "BRIGHTDATA_SITES",
              "BRIGHTDATA_HOST", "BRIGHTDATA_COUNTRY"):
        monkeypatch.delenv(k, raising=False)


def test_brightdata_proxy_returns_none_without_username(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "secret")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    assert base._brightdata_proxy_for("pharmonline") is None


def test_brightdata_proxy_returns_none_without_password(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    assert base._brightdata_proxy_for("pharmonline") is None


def test_brightdata_proxy_returns_none_when_site_not_listed(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "secret")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline,aptekonline")
    # aloe is NOT in the site list → no proxy (works direct from Hetzner)
    assert base._brightdata_proxy_for("aloe") is None


def test_brightdata_proxy_default_endpoint(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "secret")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    cfg = base._brightdata_proxy_for("pharmonline")
    assert cfg == {
        "server": "http://brd.superproxy.io:33335",
        "username": "brd-customer-hl_X-zone-Y",
        "password": "secret",
    }


def test_brightdata_proxy_appends_country_when_not_in_username(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "secret")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    monkeypatch.setenv("BRIGHTDATA_COUNTRY", "tr")
    cfg = base._brightdata_proxy_for("pharmonline")
    assert cfg["username"] == "brd-customer-hl_X-zone-Y-country-tr"


def test_brightdata_proxy_does_not_double_country(monkeypatch):
    """User pre-encoded country in username → don't append again."""
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y-country-az")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "secret")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    monkeypatch.setenv("BRIGHTDATA_COUNTRY", "tr")  # different, should NOT clobber
    cfg = base._brightdata_proxy_for("pharmonline")
    assert cfg["username"] == "brd-customer-hl_X-zone-Y-country-az"


def test_brightdata_proxy_custom_host(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "u")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    monkeypatch.setenv("BRIGHTDATA_HOST", "custom.proxy.example:9999")
    cfg = base._brightdata_proxy_for("pharmonline")
    assert cfg["server"] == "http://custom.proxy.example:9999"


def test_brightdata_proxy_handles_whitespace_in_csv(monkeypatch):
    _clear_brightdata_env(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "u")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_SITES", " pharmonline ,  aptekonline ")
    assert base._brightdata_proxy_for("pharmonline") is not None
    assert base._brightdata_proxy_for("aptekonline") is not None
    assert base._brightdata_proxy_for("aloe") is None


# ─── Captcha indicator constants ────────────────────────────────────────────


def test_captcha_indicators_non_empty():
    assert len(captcha.CAPTCHA_INDICATORS) >= 5
    assert any("cloudflare" in s for s in captcha.CAPTCHA_INDICATORS)
    assert any("recaptcha" in s for s in captcha.CAPTCHA_INDICATORS)


def test_captcha_text_patterns_non_empty():
    assert len(captcha.CAPTCHA_TEXT_PATTERNS) >= 3
    # Should cover EN + RU + AZ
    joined = " ".join(captcha.CAPTCHA_TEXT_PATTERNS).lower()
    assert "checking" in joined or "verify" in joined
    assert any("Подтвердите" in p for p in captcha.CAPTCHA_TEXT_PATTERNS)


# ─── detect_captcha — DOM path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_captcha_finds_cloudflare():
    """Mock Page where query_selector returns a handle for cloudflare iframe."""
    page = MagicMock()
    handle = MagicMock()

    async def query_selector(sel: str):
        return handle if "cloudflare" in sel else None

    page.query_selector = AsyncMock(side_effect=query_selector)
    page.url = "https://example.com/blocked"
    page.text_content = AsyncMock(return_value="")

    result = await captcha.detect_captcha(page)
    assert result is not None
    assert "cloudflare" in result


@pytest.mark.asyncio
async def test_detect_captcha_returns_none_when_clean():
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.text_content = AsyncMock(return_value="Just normal page content")
    page.url = "https://example.com/normal"

    result = await captcha.detect_captcha(page)
    assert result is None


@pytest.mark.asyncio
async def test_detect_captcha_text_fallback():
    """No DOM hits, but body text contains 'Checking your browser'."""
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.text_content = AsyncMock(return_value="Checking your browser before continuing")
    page.url = "https://example.com/cf"

    result = await captcha.detect_captcha(page)
    assert result is not None
    assert "text:" in result


@pytest.mark.asyncio
async def test_detect_captcha_handles_text_extraction_failure():
    """If text_content() throws (e.g. timeout), don't crash — return None."""
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.text_content = AsyncMock(side_effect=Exception("timeout"))
    page.url = "https://example.com/x"

    result = await captcha.detect_captcha(page)
    assert result is None


# ─── CaptchaDetected exception ──────────────────────────────────────────────


def test_captcha_detected_is_exception():
    """Should be catchable as Exception."""
    e = base.CaptchaDetected("test")
    assert isinstance(e, Exception)
    assert str(e) == "test"


# ─── BaseScraper config from env ────────────────────────────────────────────


def test_base_scraper_reads_rate_limit_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPE_RATE_LIMIT_SEC", "5.5")

    class DummyScraper(base.BaseScraper):
        site_name = "dummy"
        base_url = "https://dummy.test"

        async def scrape_category(self, category_slug, limit=None):
            yield  # type: ignore

    s = DummyScraper()
    assert s.rate_limit_sec == 5.5


def test_base_scraper_reads_max_retries_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPE_MAX_RETRIES", "7")

    class DummyScraper(base.BaseScraper):
        site_name = "dummy"
        base_url = "https://dummy.test"

        async def scrape_category(self, category_slug, limit=None):
            yield  # type: ignore

    s = DummyScraper()
    assert s.max_retries == 7


# ─── Backwards-compat: USER_AGENTS still exported ───────────────────────────


def test_legacy_user_agents_export_still_works():
    """Some external scripts may import USER_AGENTS from base.py."""
    assert isinstance(base.USER_AGENTS, tuple)
    assert len(base.USER_AGENTS) >= 5
