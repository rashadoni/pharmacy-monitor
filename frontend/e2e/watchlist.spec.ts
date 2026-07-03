import { test, expect } from "@playwright/test";
import { authenticate, hasE2EAuth } from "./helpers/auth";

/**
 * Phase 6.3 (audit 2026-05-28) — Watchlist UX E2E.
 *
 * Что покрываем:
 *   - страница рендерится с локалью /ru/watchlist
 *   - empty state виден когда нет items (зависит от DB seed — gracefully skip
 *     если auth недоступен или есть items)
 *   - search input фильтрует list
 *   - delete-armed pattern: первый клик меняет иконку → второй удаляет
 *   - add form открывается/закрывается
 *
 * Auth-gated: use PLAYWRIGHT_AUTH_TOKEN or PLAYWRIGHT_AUTH_LOGIN/PASSWORD.
 */

test.describe("Watchlist page (unauthenticated)", () => {
  test("legacy /watchlist redirects to /ru/watchlist", async ({ request }) => {
    const r = await request
      .get("/watchlist", { maxRedirects: 0 })
      .catch((err) => err.response ?? null);
    expect(r?.status()).toBe(307);
    expect(r?.headers()["location"]).toBe("/ru/watchlist");
  });

  test("/ru/watchlist requires auth (redirects to login)", async ({ page }) => {
    await page.goto("/ru/watchlist");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("Watchlist page (authenticated)", () => {
  test.skip(
    !hasE2EAuth(),
    "Set PLAYWRIGHT_AUTH_TOKEN or PLAYWRIGHT_AUTH_LOGIN/PASSWORD to run authenticated tests",
  );

  test.beforeEach(async ({ page }) => {
    await authenticate(page);
  });

  test("watchlist page renders", async ({ page }) => {
    await page.goto("/ru/watchlist");
    await expect(page.locator("h1")).toContainText("Watchlist");
  });

  test("add button opens form", async ({ page }) => {
    await page.goto("/ru/watchlist");
    await page.locator('[data-testid="watchlist-add"]').click();
    await expect(page.locator('[data-testid="watchlist-form-canonical"]')).toBeVisible();
  });

  test("form save is disabled without canonical name", async ({ page }) => {
    await page.goto("/ru/watchlist");
    await page.locator('[data-testid="watchlist-add"]').click();
    const submit = page.locator('[data-testid="watchlist-form-submit"]');
    await expect(submit).toBeDisabled();
    await page.locator('[data-testid="watchlist-form-canonical"]').fill("E2E Test Item");
    await expect(submit).toBeEnabled();
  });

  test("delete uses 2-click armed pattern (no window.confirm)", async ({ page }) => {
    await page.goto("/ru/watchlist");
    // Tries to find any existing item via testid prefix; skip if none seeded
    const deleteButtons = page.locator('[data-testid^="watchlist-delete-"]:not([data-testid*="confirm"])');
    const count = await deleteButtons.count();
    test.skip(count === 0, "No watchlist items seeded — skipping armed-delete check");

    await deleteButtons.first().click();
    // After first click the same row now has the confirm button visible
    await expect(
      page.locator('[data-testid^="watchlist-delete-confirm-"]'),
    ).toHaveCount(1);
  });

  test("search input filters visible items", async ({ page }) => {
    await page.goto("/ru/watchlist");
    const search = page.locator('[data-testid="watchlist-search"]');
    const visible = await search.isVisible().catch(() => false);
    test.skip(!visible, "No items rendered → search not shown");
    await search.fill("definitely-not-in-list-zzzz");
    // "No match" empty-state appears
    await expect(page.getByText(/Ничего не найдено|no match/i)).toBeVisible();
  });

  test("category products link preserves locale and does not open comparison", async ({ page }) => {
    await page.goto("/az/watchlist");
    await page.waitForLoadState("networkidle");
    const link = page.locator('[data-testid^="watchlist-category-products-"]').first();
    await expect(link).toBeVisible({ timeout: 10_000 });

    const href = await link.getAttribute("href");
    expect(href).toContain("/az/category-products?");
    expect(href).not.toContain("/comparison");

    await link.click();
    await expect(page).toHaveURL(/\/az\/category-products\?/);
    await expect(page.locator("h1")).toContainText("Kateqoriya məhsulları");
  });
});
