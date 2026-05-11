"""Captcha / bot-wall detection.

We don't try to solve captchas — we detect them, log a warning, and skip the
page. If a site introduces captcha for our scraper, that's a signal to:
  1. Switch IP / proxy
  2. Add anti-detection patches if not already
  3. Reduce scrape frequency
  4. Consider that site as 'blocked' until manual intervention

The goal: never silently skip pages thinking they're empty when actually a
captcha is rendered.
"""
from __future__ import annotations

import structlog
from playwright.async_api import Page

log = structlog.get_logger()


# Selectors / text that indicate captcha / bot-wall.
# Order matters: most specific first.
CAPTCHA_INDICATORS = [
    # Cloudflare (most common)
    "iframe[src*='challenges.cloudflare.com']",
    "div[class*='cf-browser-verification']",
    "#cf-please-wait",
    # Google reCAPTCHA
    "iframe[src*='google.com/recaptcha']",
    "iframe[src*='recaptcha.net']",
    "div.g-recaptcha",
    # hCaptcha
    "iframe[src*='hcaptcha.com']",
    "div.h-captcha",
    # Datadome
    "iframe[src*='datadome.co']",
    "[id*='datadome']",
    # Generic
    "div.captcha",
    "img[src*='captcha']",
]

CAPTCHA_TEXT_PATTERNS = [
    "Checking your browser",
    "Please verify you are human",
    "Подтвердите, что вы человек",
    "Robot olduğunuzu",  # AZ
    "complete the security check",
    "ddg-challenge",
]


async def detect_captcha(page: Page) -> str | None:
    """Return captcha indicator name if detected, else None.

    Fast: checks DOM selectors first, falls back to text scan only if needed.
    Should be called after `page.goto()` completes (after domcontentloaded).
    """
    # Fast: DOM selector check
    for selector in CAPTCHA_INDICATORS:
        try:
            handle = await page.query_selector(selector)
            if handle:
                log.warning(
                    "captcha_detected",
                    type="dom",
                    selector=selector,
                    url=page.url,
                )
                return f"dom:{selector}"
        except Exception:
            continue

    # Slower: text content scan
    try:
        text = await page.text_content("body", timeout=2000)
        if text:
            text_lower = text.lower()
            for pattern in CAPTCHA_TEXT_PATTERNS:
                if pattern.lower() in text_lower:
                    log.warning(
                        "captcha_detected",
                        type="text",
                        pattern=pattern,
                        url=page.url,
                    )
                    return f"text:{pattern}"
    except Exception:
        pass

    return None


async def is_blocked_by_bot_wall(page: Page) -> bool:
    """Convenience boolean check."""
    return await detect_captcha(page) is not None
