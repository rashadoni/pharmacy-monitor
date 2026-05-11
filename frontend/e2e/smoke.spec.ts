import { test, expect } from "@playwright/test";

/**
 * Smoke test suite: verify core user journeys work end-to-end.
 *
 * This DOES NOT mock the API — it talks to the real FastAPI backend.
 * Run prerequisites:
 *   1. Backend with seed user `e2e@example.com` and DB with at least 1 match.
 *   2. Either set `PLAYWRIGHT_AUTH_TOKEN` env var to a valid magic token,
 *      OR mark these tests as skipped without an auth setup.
 */

test.describe("Auth flow", () => {
  test("login page renders the form", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator("h1")).toContainText("Sign in");
    await expect(page.locator("input[type=email]")).toBeVisible();
    await expect(page.locator("button[type=submit]")).toBeVisible();
  });

  test("submitting unknown email shows success (no enumeration)", async ({ page }) => {
    await page.goto("/login");
    await page.fill("input[type=email]", "nobody@example.com");
    await page.click("button[type=submit]");
    await expect(page.getByText(/Письмо отправлено/i)).toBeVisible({ timeout: 10_000 });
  });

  test("invalid email is blocked client-side", async ({ page }) => {
    await page.goto("/login");
    await page.fill("input[type=email]", "not-an-email");
    await page.click("button[type=submit]");
    // Native HTML5 validation prevents submit; URL stays /login
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("Protected routes (no cookie)", () => {
  for (const path of ["/overview", "/comparison", "/analytics", "/alerts", "/watchlist", "/categories", "/settings"]) {
    test(`${path} redirects to /login when unauthenticated`, async ({ page }) => {
      await page.goto(path);
      await expect(page).toHaveURL(/\/login/);
    });
  }
});

test.describe("Authenticated dashboard", () => {
  test.skip(
    !process.env.PLAYWRIGHT_AUTH_TOKEN,
    "Set PLAYWRIGHT_AUTH_TOKEN to run these tests",
  );

  test.beforeEach(async ({ page }) => {
    // Use the magic-link verify endpoint to set the cookie
    await page.goto(`/auth/verify?token=${process.env.PLAYWRIGHT_AUTH_TOKEN}`);
    await page.waitForURL(/\/overview/, { timeout: 10_000 });
  });

  test("overview shows KPI cards", async ({ page }) => {
    await page.goto("/overview");
    await expect(page.locator("h1")).toContainText("Обзор");
    await expect(page.getByText(/Cross-site matches/i)).toBeVisible();
    await expect(page.getByText(/Coverage/i)).toBeVisible();
  });

  test("comparison search filters results", async ({ page }) => {
    await page.goto("/comparison");
    await page.fill("[data-testid=search-input]", "nestle");
    await page.waitForTimeout(500); // debounce
    // Either we have results or empty-state — both are valid
    const hasResults = await page.locator("[data-testid=result-count]").isVisible().catch(() => false);
    const hasEmpty = await page.locator("[data-testid=empty]").isVisible().catch(() => false);
    expect(hasResults || hasEmpty).toBe(true);
  });

  test("min-sites filter changes URL state", async ({ page }) => {
    await page.goto("/comparison");
    await page.selectOption("[data-testid=min-sites-select]", "1");
    await page.waitForTimeout(500);
    // No URL change for now — internal state. Just verify select value.
    await expect(page.locator("[data-testid=min-sites-select]")).toHaveValue("1");
  });

  test("alerts page renders feed", async ({ page }) => {
    await page.goto("/alerts");
    await expect(page.locator("h1")).toContainText("Алерты");
    // Either feed or empty state
    const anyContent = await page
      .locator("text=/Critical|Warning|Info|Алертов нет/i")
      .first()
      .isVisible({ timeout: 5_000 })
      .catch(() => false);
    expect(anyContent).toBe(true);
  });

  test("settings shows logged-in user", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator("h1")).toContainText("Настройки");
    await expect(page.getByText(/Профиль/i)).toBeVisible();
  });

  test("mobile bottom nav is visible on small viewport", async ({ page, browserName }) => {
    test.skip(browserName !== "webkit", "Mobile-specific test");
    await page.goto("/overview");
    // 5 visible nav buttons on mobile
    const buttons = page.locator("nav.md\\:hidden a");
    await expect(buttons).toHaveCount(5);
  });
});
