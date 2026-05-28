"""Anti-detection patches for Playwright.

Reduces the chance of being flagged as a bot by:
  - Patching `navigator.webdriver` to undefined
  - Faking realistic `navigator.plugins`, `navigator.languages`, `navigator.platform`
  - Removing `chrome.runtime.connect` automation marker
  - Setting realistic viewport + device pixel ratio
  - Random subtle viewport variations per session

This is NOT a guaranteed bypass — sites with serious bot protection (Cloudflare,
Datadome, Akamai) need residential proxies + CAPTCHA solving. But it removes
the obvious flags that make trivial detection (`navigator.webdriver === true`)
trip immediately.

Usage:
    context = await browser.new_context(...)
    await apply_stealth(context)
"""

from __future__ import annotations

import random
from playwright.async_api import BrowserContext

# Snippet runs in every page before any site script. Source-of-truth for tweaks.
# Don't go overboard — too many fake values trigger fingerprint anomaly detection.
_STEALTH_JS = """
() => {
    // 1. Hide webdriver flag (most common detection)
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });

    // 2. Fake plugins array (real browsers have 3-5)
    if (navigator.plugins.length === 0) {
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'internal-chrome-pdf-viewer' },
                { name: 'Chromium PDF Viewer', filename: 'internal-chromium-pdf-viewer' },
            ],
            configurable: true,
        });
    }

    // 3. Languages (real browsers have at least 1, often 2)
    Object.defineProperty(navigator, 'languages', {
        get: () => ['az-AZ', 'az', 'ru-RU', 'ru', 'en-US', 'en'],
        configurable: true,
    });

    // 4. Permissions API — bots often return inconsistent values
    if (navigator.permissions && navigator.permissions.query) {
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return originalQuery(params);
        };
    }

    // 5. WebGL vendor/renderer (bots often have generic SwiftShader)
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
        return getParameter.call(this, param);
    };

    // 6. Suppress chrome.runtime detection
    if (window.chrome) {
        window.chrome.runtime = window.chrome.runtime || {};
    }
};
"""


async def apply_stealth(context: BrowserContext) -> None:
    """Inject stealth patches into every page in this context.

    Must be called BEFORE any page.goto(). Idempotent.
    """
    await context.add_init_script(_STEALTH_JS)


# Realistic viewport sizes (real users don't have exactly 1366×900 every time)
COMMON_VIEWPORTS = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1920, 1080),
    (1600, 900),
    (1280, 800),
]


def random_viewport() -> dict[str, int]:
    """Return a random realistic desktop viewport with subtle ±10px jitter."""
    w, h = random.choice(COMMON_VIEWPORTS)
    return {
        "width": w + random.randint(-10, 10),
        "height": h + random.randint(-10, 10),
    }


# Larger UA pool than the original 3 in base.py.
# Sourced from real Chrome / Firefox / Safari version strings (Q1 2026 era).
USER_AGENTS_EXTENDED = [
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS_EXTENDED)
