import { test, expect, type Page } from "@playwright/test";
import { authenticate, hasE2EAuth } from "./helpers/auth";

async function createWatchlistItem(page: Page, canonicalName: string) {
  const response = await page.context().request.post("/api/v1/dash/watchlist", {
    data: {
      canonical_name: canonicalName,
      brand: "E2E",
      notes: "created by Playwright e2e",
    },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  return body.id as number;
}

async function deleteWatchlistItem(
  page: Page,
  id: number,
  { allowMissing = false }: { allowMissing?: boolean } = {},
) {
  const response = await page.context().request.delete(`/api/v1/dash/watchlist/${id}`);
  if (allowMissing && response.status() === 404) return;
  expect(response.ok(), await response.text()).toBe(true);
}

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
  test("legacy /watchlist redirects to /az/watchlist", async ({ request }) => {
    const r = await request
      .get("/watchlist", { maxRedirects: 0 })
      .catch((err) => err.response ?? null);
    expect(r?.status()).toBe(307);
    expect(r?.headers()["location"]).toBe("/az/watchlist");
  });

  test("/ru/watchlist requires auth (redirects to login)", async ({ page }) => {
    await page.goto("/ru/watchlist");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("Watchlist page (authenticated)", () => {
  test.describe.configure({ mode: "serial" });

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
    const itemName = `E2E Delete ${Date.now()}`;
    const itemId = await createWatchlistItem(page, itemName);
    try {
      await page.goto("/ru/watchlist");
      await expect(page.getByText(itemName)).toBeVisible();

      await page.locator(`[data-testid="watchlist-delete-${itemId}"]`).click();
      await expect(
        page.locator(`[data-testid="watchlist-delete-confirm-${itemId}"]`),
      ).toHaveCount(1);
      await page.locator(`[data-testid="watchlist-delete-confirm-${itemId}"]`).click();
      await expect(page.getByText(itemName)).toHaveCount(0);
    } finally {
      await deleteWatchlistItem(page, itemId, { allowMissing: true });
    }
  });

  test("search input filters visible items", async ({ page }) => {
    const itemId = await createWatchlistItem(page, `E2E Search ${Date.now()}`);
    try {
      await page.goto("/ru/watchlist");
      const search = page.locator('[data-testid="watchlist-search"]');
      await expect(search).toBeVisible();
      await search.fill("definitely-not-in-list-zzzz");
      await expect(page.getByText(/Ничего не найдено|no match/i)).toBeVisible();
    } finally {
      await deleteWatchlistItem(page, itemId);
    }
  });

  test("category products link preserves locale and does not open comparison", async ({ page }) => {
    await page.goto("/az/watchlist");
    const link = page.locator('[data-testid^="watchlist-category-products-"]').first();
    await expect(link).toBeVisible({ timeout: 10_000 });

    const href = await link.getAttribute("href");
    expect(href).toContain("/az/category-products?");
    expect(href).toContain("aloe=tibbi-vasit");
    expect(href).not.toContain("/comparison");

    await link.click();
    await expect(page).toHaveURL(/\/az\/category-products\?/);
    await expect(page.locator("h1")).toContainText("Kateqoriya məhsulları");
    await expect(page.locator('[data-testid="category-products-missing-sites"]')).toHaveCount(0);
  });

  test("category attach link opens matcher in current locale", async ({ page }) => {
    await page.goto("/az/watchlist");
    const link = page.locator('[data-testid^="watchlist-category-attach-aloe-"]').first();
    await expect(link).toBeVisible({ timeout: 10_000 });
    await expect(link).toContainText("uyğunluq əlavə et");
    await expect(link).toHaveAttribute("title", /uyğunlaşdırma seçimini/i);

    const href = await link.getAttribute("href");
    expect(href).toContain("/az/matcher?");
    expect(href).toContain("mode=attach");
    expect(href).toContain("site=aloe");
    expect(href).toContain("category=");

    await link.click();
    await expect(page).toHaveURL(/\/az\/matcher\?.*mode=attach/);
    await expect(page).toHaveURL(/site=aloe/);
    const selectedCategory = page.locator('[data-testid="matcher-category-filter"] option:checked');
    await expect(selectedCategory).toContainText("Digər tibbi vasitələr");
    await expect(selectedCategory).not.toContainText(/[А-Яа-яЁё]/);
  });
});
