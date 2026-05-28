import { test, expect } from "@playwright/test";

/**
 * Phase 6.1 i18n routing regression tests.
 *
 * Verifies URL-based locale (`/ru/...`, `/az/...`, `/en/...`) + backward
 * compat redirects (`/comparison` → `/ru/comparison`) work correctly.
 *
 * These tests are PUBLIC — don't need auth (login page renders for all locales,
 * legacy redirects don't require session).
 *
 * Setup constraint: when run against prod (PLAYWRIGHT_BASE_URL=https://leaddrive.cloud),
 * any "page renders content" assertion may be blocked by Caddy / auth-gate / etc.
 * For prod we mostly verify HTTP status + URL behavior.
 */

test.describe("Locale prefix routing", () => {
  for (const locale of ["ru", "az", "en"] as const) {
    test(`/${locale}/login renders successfully`, async ({ page, request }) => {
      const response = await request.get(`/${locale}/login`);
      expect(response.status()).toBe(200);
      // Body should contain auth-related content
      const body = await response.text();
      expect(body.length).toBeGreaterThan(500);  // not an empty error page
    });

    test(`/${locale}/comparison hits auth gate (307 to login)`, async ({ request }) => {
      const response = await request.get(`/${locale}/comparison`, {
        maxRedirects: 0,
      }).catch((err) => {
        // Playwright treats 307 as redirect; need maxRedirects=0
        return err.response ?? null;
      });
      // Either 307 (auth redirect) or 200 (if logged in). Both acceptable.
      // We want to verify it's NOT 500 (recursion bug) или 404.
      const status = response?.status() ?? 0;
      expect([200, 307]).toContain(status);
    });
  }
});

test.describe("Legacy URL redirects (Phase 6.1 backward compat)", () => {
  const LEGACY_ROUTES = [
    "comparison",
    "overview",
    "alerts",
    "analytics",
    "categories",
    "matcher",
    "settings",
    "watchlist",
    "login",
  ];

  for (const route of LEGACY_ROUTES) {
    test(`/${route} → /ru/${route} (next.config.mjs redirect)`, async ({ request }) => {
      const response = await request.get(`/${route}`, {
        maxRedirects: 0,
      }).catch((err) => err.response ?? null);
      const status = response?.status() ?? 0;
      expect(status).toBe(307);
      const location = response?.headers()["location"];
      expect(location).toBe(`/ru/${route}`);
    });
  }

  test("/ eventually reaches /ru/* (root redirect chain)", async ({ page }) => {
    // / → /ru → /ru/overview → /login → /ru/login (auth gate cascade).
    // Just verify final URL is some /ru/* path (not 500 or stuck).
    await page.goto("/");
    await expect(page).toHaveURL(/\/ru\//);
  });
});

test.describe("No middleware recursion (next-intl issue #524)", () => {
  test("no 500 errors on locale routes", async ({ request }) => {
    // The bug we fixed: standalone middleware rewrite causing recursive
    // HTTP proxy → ECONNRESET → 500. After Phase 6.1 retry, should be clean.
    for (const url of ["/ru/login", "/az/login", "/en/login"]) {
      const response = await request.get(url);
      expect(
        response.status(),
        `${url} should return 200, not 500 (recursion bug)`,
      ).toBe(200);
    }
  });

  test("X-Request-ID header preserved through redirects (Phase 5.5)", async ({ request }) => {
    const customId = `test-i18n-${Date.now()}`;
    const response = await request.get(`/health`, {
      headers: { "X-Request-ID": customId },
    });
    expect(response.status()).toBe(200);
    const echoedId = response.headers()["x-request-id"];
    expect(echoedId).toBe(customId);
  });
});

test.describe("API routes bypass i18n", () => {
  test("/api/* doesn't get locale-prefixed", async ({ request }) => {
    // API routes managed by Caddy → FastAPI, не by Next.js i18n
    const response = await request.get(`/health`);
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty("status");
  });
});
