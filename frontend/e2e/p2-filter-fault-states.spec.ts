import { expect, test, type Page, type Route } from "@playwright/test";

const BASE_URL =
  process.env.PLAYWRIGHT_BASE_URL ||
  `http://localhost:${process.env.PLAYWRIGHT_PORT || "3000"}`;

async function enterDashboard(page: Page) {
  await page.context().addCookies([
    { name: "pm_session", value: "fault-injection-test", url: BASE_URL },
  ]);
}

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function expectMobileShellFits(page: Page) {
  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll("body *"))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          text: element.textContent?.trim().slice(0, 80),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          width: Math.round(rect.width),
          className: typeof element.className === "string" ? element.className : "",
        };
      })
      .filter((rect) => rect.left < -1 || rect.right > document.documentElement.clientWidth + 1)
      .slice(0, 10),
  }));
  expect(overflow, JSON.stringify(overflow.offenders, null, 2)).toMatchObject({
    scrollWidth: overflow.clientWidth,
  });

  const navTargets = page.locator("nav.fixed.bottom-0").locator(":scope > a, :scope > button");
  await expect(navTargets).toHaveCount(5);
  const heights = await navTargets.evaluateAll((elements) =>
    elements.map((element) => element.getBoundingClientRect().height),
  );
  expect(heights.every((height) => height >= 44)).toBe(true);
}

test.describe("P2 shareable filters and fault states", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page }) => {
    await enterDashboard(page);
  });

  test("alerts preserves URL filters and retries an injected API failure", async ({ page }) => {
    let attempts = 0;
    let allowSuccess = false;
    await page.route("**/api/v1/dash/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/dash/alerts/counts") {
        return json(route, 200, { unread: 0, snoozed: 0, read: 0, total: 0 });
      }
      if (path === "/api/v1/dash/alerts") {
        attempts += 1;
        if (!allowSuccess) return json(route, 503, { detail: "fault injection" });
        return json(route, 200, [
          {
            id: 101,
            rule_type: "retired_rule",
            severity: "warning",
            title: "Regression alert",
            detail: null,
            payload: null,
            created_at: new Date().toISOString(),
            is_read: true,
            read_at: null,
            snoozed_until: null,
          },
        ]);
      }
      if (path === "/api/v1/dash/me/notifications") {
        return json(route, 200, { email_severity_min: "warning", telegram_chat_id: null });
      }
      return json(route, 200, {});
    });

    await page.goto("/ru/alerts?view=read&severity=critical&hours=24&type=retired_rule");
    await expect(page).toHaveURL(/view=read.*severity=critical.*hours=24.*type=retired_rule/);
    await expect(page.getByLabel("Тип уведомления")).toHaveValue("retired_rule");
    const retryButton = page.getByRole("button", { name: "Повторить" });
    await expect(retryButton).toBeVisible();
    allowSuccess = true;
    const attemptsBeforeRetry = attempts;
    await retryButton.click();
    await expect.poll(() => attempts).toBeGreaterThan(attemptsBeforeRetry);
    await page.getByRole("button", { name: "🟡 Предупреждение" }).click();
    await expect(page).toHaveURL(/severity=warning.*hours=24.*type=retired_rule/);
    await page.getByLabel("Период уведомлений").selectOption("72");
    await expect(page).toHaveURL(/severity=warning.*hours=72.*type=retired_rule/);
    await page.goBack();
    await expect(page).toHaveURL(/severity=warning.*hours=24.*type=retired_rule/);
    await page.goBack();
    await expect(page).toHaveURL(/severity=critical.*hours=24.*type=retired_rule/);
    await page.goForward();
    await expect(page).toHaveURL(/severity=warning.*hours=24.*type=retired_rule/);
    const bulkCheckbox = page.getByRole("checkbox", { name: "Выбрать для массового действия" });
    await bulkCheckbox.check();
    await expect(page.getByText("1 выбрано:")).toBeVisible();
    await page.getByLabel("Период уведомлений").selectOption("720");
    await expect(page).toHaveURL(/hours=720/);
    await expect(page.getByText("1 выбрано:")).toHaveCount(0);
    await bulkCheckbox.check();
    await page.goBack();
    await expect(page).toHaveURL(/hours=24/);
    await expect(page.getByText("1 выбрано:")).toHaveCount(0);

    await page.getByRole("button", { name: "Ещё" }).click();
    await expect(page.getByRole("menuitem", { name: "Настройки" })).toBeVisible();
    await expectMobileShellFits(page);
  });

  test("Azerbaijani categories keeps administration context across reload", async ({ page }) => {
    let attempts = 0;
    await page.route("**/api/v1/dash/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/dash/categories") {
        attempts += 1;
        return json(route, 503, { detail: "fault injection" });
      }
      if (path === "/api/v1/dash/categories/suggestions") return json(route, 200, []);
      if (path === "/api/v1/dash/scrape/requests") return json(route, 200, []);
      if (path === "/api/v1/dash/me/notifications") {
        return json(route, 200, { email_severity_min: "warning", telegram_chat_id: null });
      }
      return json(route, 200, {});
    });

    await page.goto("/az/categories?q=vitamin&site=aloe&active=1&coverage=cross3");
    await expect(page.locator("html")).toHaveAttribute("lang", "az");
    await expect(page.getByRole("searchbox", { name: "Kateqoriya axtarışı" })).toHaveValue(
      "vitamin",
    );
    await expect(page.getByRole("alert")).toBeVisible();
    await page.getByRole("button", { name: "Yenidən cəhd et" }).click();
    await expect.poll(() => attempts).toBeGreaterThan(1);
    await expect(page).toHaveURL(/q=vitamin.*site=aloe.*active=1.*coverage=cross3/);
    await page.getByLabel("Kateqoriya saytı").selectOption("aptekonline");
    await expect(page).toHaveURL(/site=aptekonline/);
    await page.goBack();
    await expect(page).toHaveURL(/site=aloe/);
    await page.goForward();
    await expect(page).toHaveURL(/site=aptekonline/);
    await page.reload();
    await expect(page.getByRole("searchbox", { name: "Kateqoriya axtarışı" })).toHaveValue(
      "vitamin",
    );
    await page.getByRole("button", { name: "Tövsiyə olunan əlaqələr" }).click();
    await expect(page.getByLabel("Sayt A")).toBeVisible();
    const suggestionControlHeights = await page
      .locator('select, input[type="number"]')
      .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height));
    expect(suggestionControlHeights.every((height) => height >= 44)).toBe(true);
    await expectMobileShellFits(page);
  });

  test("site catalog preserves filters, pagination and retry on failure", async ({ page }) => {
    let attempts = 0;
    await page.route("**/api/v1/dash/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/dash/products") {
        attempts += 1;
        return json(route, 503, { detail: "fault injection" });
      }
      if (path === "/api/v1/dash/products/facets") {
        return json(route, 200, {
          categories: [{ name: "vitamins", label: "Витамины", count: 10 }],
          brands: [{ name: "Solgar", count: 10 }],
        });
      }
      if (path === "/api/v1/dash/products/summary") {
        return json(route, 200, {
          total_products: 10,
          total_brands: 1,
          exclusive_brands: 0,
          on_sale_count: 1,
          on_sale_pct: 10,
          last_run_at: null,
          last_run_id: null,
        });
      }
      if (path === "/api/v1/dash/brand-share") return json(route, 200, []);
      if (path === "/api/v1/dash/products/1/price-history") {
        return json(route, 200, { points: [], delta_pct: null });
      }
      if (path === "/api/v1/dash/roi/recommendations") {
        return json(route, 200, {
          items: [],
          provenance: {
            available: true,
            client_site: "aloe",
            run_id: 1,
            computed_at: null,
            run_started_at: null,
            run_finished_at: null,
            item_count: 0,
          },
        });
      }
      if (path === "/api/v1/dash/me/notifications") {
        return json(route, 200, { email_severity_min: "warning", telegram_chat_id: null });
      }
      return json(route, 200, {});
    });

    await page.goto(
      "/ru/site/aloe?q=vitamin&category=retired-category&brand=LegacyBrand&sale=1&page=2",
    );
    await expect(page.getByRole("searchbox", { name: "Поиск в каталоге" })).toHaveValue(
      "vitamin",
    );
    await expect(page.getByLabel("Категория каталога")).toHaveValue("retired-category");
    await expect(page.getByLabel("Бренд каталога")).toHaveValue("LegacyBrand");
    await expect(page.getByRole("alert")).toBeVisible();
    await page.getByRole("button", { name: "Повторить" }).click();
    await expect.poll(() => attempts).toBeGreaterThan(1);
    await expect(page).toHaveURL(
      /q=vitamin.*category=retired-category.*brand=LegacyBrand.*sale=1.*page=2/,
    );
    await page.getByRole("checkbox", { name: "Скидка" }).click();
    await expect(page).not.toHaveURL(/sale=1/);
    await expect(page.getByRole("checkbox", { name: "Скидка" })).not.toBeChecked();
    await page.goBack();
    await expect(page).toHaveURL(/sale=1/);
    await page.goForward();
    await expect(page).not.toHaveURL(/sale=1/);
    await expectMobileShellFits(page);
  });

  test("site catalog canonicalizes an out-of-range page after a successful response", async ({
    page,
  }) => {
    await page.route("**/api/v1/dash/**", (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === "/api/v1/dash/products") {
        const offset = Number(url.searchParams.get("offset") || "0");
        return json(route, 200, {
          items:
            offset === 0
              ? [
                  {
                    id: 1,
                    external_id: "canonical-product",
                    name: "Canonical product",
                    brand: "Trusted",
                    category: "vitamins",
                    url: "https://example.test/product",
                    image_url: null,
                    price: 10,
                    discount_price: null,
                    effective_price: 10,
                    is_on_sale: false,
                    last_seen_at: null,
                  },
                ]
              : [],
          total: 10,
          limit: 50,
          offset,
        });
      }
      if (path === "/api/v1/dash/products/facets") {
        return json(route, 200, { categories: [], brands: [] });
      }
      if (path === "/api/v1/dash/products/summary") {
        return json(route, 200, {
          total_products: 10,
          total_brands: 1,
          exclusive_brands: 0,
          on_sale_count: 0,
          on_sale_pct: 0,
          last_run_at: null,
          last_run_id: null,
        });
      }
      if (path === "/api/v1/dash/brand-share") return json(route, 200, []);
      if (path === "/api/v1/dash/products/1/price-history") {
        return json(route, 200, { points: [], delta_pct: null });
      }
      if (path === "/api/v1/dash/roi/recommendations") {
        return json(route, 200, {
          items: [],
          provenance: {
            available: true,
            client_site: "aloe",
            run_id: 1,
            computed_at: null,
            run_started_at: null,
            run_finished_at: null,
            item_count: 0,
          },
        });
      }
      if (path === "/api/v1/dash/me/notifications") {
        return json(route, 200, { email_severity_min: "warning", telegram_chat_id: null });
      }
      return json(route, 200, {});
    });

    await page.goto("/ru/site/aloe?page=2");
    await expect(page).toHaveURL(/\/ru\/site\/aloe$/);
    await expect(page.getByRole("link", { name: /Canonical product Trusted/ })).toBeVisible();
    await expect(page.getByText("51–10 из 10")).toHaveCount(0);
    await expectMobileShellFits(page);
  });
});
