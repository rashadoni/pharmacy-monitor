import { test, expect } from "@playwright/test";

/**
 * Task #33 regression test: aloe Product.url должен быть detail page URL,
 * не category listing URL.
 *
 * Bug 2026-05-28: все 1809 aloe products в DB имели url типа
 *   https://aloe.az/catalog/filters/?category_slug=dermanlar&page=7
 * вместо
 *   https://aloe.az/{product-slug}/
 *
 * Fix: synth URL through aloe_slug() in src/scrapers/aloe.py based on
 * product name.
 *
 * This test verifies the FRONTEND consumes URL correctly: после клика на
 * aloe-продукт в дашборде user должен перейти на product detail page,
 * не на category listing.
 *
 * Requires PLAYWRIGHT_AUTH_TOKEN to access protected /comparison page.
 */

test.describe("Aloe URL regression (Task #33)", () => {
  test.skip(
    !process.env.PLAYWRIGHT_AUTH_TOKEN,
    "Set PLAYWRIGHT_AUTH_TOKEN to run authenticated tests",
  );

  test.beforeEach(async ({ page }) => {
    await page.goto(`/auth/verify?token=${process.env.PLAYWRIGHT_AUTH_TOKEN}`);
    await page.waitForURL(/\/(ru|az|en)?\/?overview/, { timeout: 10_000 });
  });

  test("aloe product links go to detail page, NOT category listing", async ({ page }) => {
    await page.goto("/ru/comparison");
    await page.waitForSelector("[data-testid=result-count], [data-testid=empty]", {
      timeout: 15_000,
    });

    // Find first row with aloe price + link
    const aloeLinks = page.locator('a[href*="aloe.az"]');
    const count = await aloeLinks.count();
    test.skip(count === 0, "No aloe products visible in comparison view");

    const firstHref = await aloeLinks.first().getAttribute("href");
    expect(firstHref, "Aloe link must NOT be category listing").not.toMatch(
      /catalog\/filters/,
    );
    // Should be format https://aloe.az/<slug>/ where slug = url-safe chars
    expect(firstHref, "Aloe link should match detail page pattern").toMatch(
      /^https:\/\/aloe\.az\/[a-z0-9-]+\/?$/,
    );
  });
});
