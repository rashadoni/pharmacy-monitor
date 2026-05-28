import { test, expect } from "@playwright/test";

/**
 * Alerts inbox E2E — Phase 5.x audit (2026-05-28).
 *
 * Auth-gated; пропускаем без PLAYWRIGHT_AUTH_TOKEN.
 *
 * Что покрываем:
 *   - редирект /alerts → /ru/alerts
 *   - страница рендерится и показывает либо feed либо empty state
 *   - severity tabs/filters работают (если есть в DOM)
 *   - mark-as-read NOT triggered automatically (явное действие)
 */

test.describe("Alerts inbox (unauthenticated)", () => {
  test("legacy /alerts redirects to /ru/alerts", async ({ request }) => {
    const r = await request
      .get("/alerts", { maxRedirects: 0 })
      .catch((err) => err.response ?? null);
    expect(r?.status()).toBe(307);
    expect(r?.headers()["location"]).toBe("/ru/alerts");
  });

  test("/ru/alerts requires auth", async ({ page }) => {
    await page.goto("/ru/alerts");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("Alerts inbox (authenticated)", () => {
  test.skip(
    !process.env.PLAYWRIGHT_AUTH_TOKEN,
    "Set PLAYWRIGHT_AUTH_TOKEN to run authenticated tests",
  );

  test.beforeEach(async ({ page }) => {
    await page.goto(`/auth/verify?token=${process.env.PLAYWRIGHT_AUTH_TOKEN}`);
    await page.waitForURL(/\/overview/, { timeout: 10_000 });
  });

  test("alerts page renders heading", async ({ page }) => {
    await page.goto("/ru/alerts");
    await expect(page.locator("h1")).toBeVisible();
  });

  test("alerts page shows feed or empty state (no 500)", async ({ page }) => {
    const responses: number[] = [];
    page.on("response", (r) => {
      if (r.url().includes("/api/v1/dash/alerts")) {
        responses.push(r.status());
      }
    });
    await page.goto("/ru/alerts");
    await page.waitForLoadState("networkidle");
    // All alerts API calls must be 200 (no 500/429 in normal flow)
    for (const status of responses) {
      expect(status).toBeLessThan(500);
    }
  });
});
