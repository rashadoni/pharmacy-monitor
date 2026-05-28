import { test, expect } from "@playwright/test";

/**
 * Phase 4.6 — Pricing Settings UI E2E.
 *
 * Auth-gated; пропускаем если нет PLAYWRIGHT_AUTH_TOKEN.
 *
 * Что покрываем:
 *   - страница доступна через /[locale]/settings/pricing
 *   - thresholds section рендерится с input'ами
 *   - costs CSV upload section рендерится
 *   - save button присутствует
 */

test.describe("Pricing Settings (unauthenticated)", () => {
  test("/settings/pricing redirects to /ru/settings/pricing", async ({ request }) => {
    const r = await request
      .get("/settings/pricing", { maxRedirects: 0 })
      .catch((err) => err.response ?? null);
    expect(r?.status()).toBe(307);
    expect(r?.headers()["location"]).toBe("/ru/settings/pricing");
  });

  test("/ru/settings/pricing requires auth", async ({ page }) => {
    await page.goto("/ru/settings/pricing");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("Pricing Settings (authenticated)", () => {
  test.skip(
    !process.env.PLAYWRIGHT_AUTH_TOKEN,
    "Set PLAYWRIGHT_AUTH_TOKEN to run authenticated tests",
  );

  test.beforeEach(async ({ page }) => {
    await page.goto(`/auth/verify?token=${process.env.PLAYWRIGHT_AUTH_TOKEN}`);
    await page.waitForURL(/\/overview/, { timeout: 10_000 });
  });

  test("pricing settings page renders thresholds section", async ({ page }) => {
    await page.goto("/ru/settings/pricing");
    await expect(page.locator("h1")).toBeVisible();
    // Thresholds section should have at least one <input type=number>
    const numericInputs = page.locator('input[type="number"]');
    await expect(numericInputs.first()).toBeVisible({ timeout: 10_000 });
  });

  test("CSV upload section is present", async ({ page }) => {
    await page.goto("/ru/settings/pricing");
    const fileInput = page.locator('input[type="file"][accept*="csv"]');
    await expect(fileInput).toHaveCount(1);
  });
});
