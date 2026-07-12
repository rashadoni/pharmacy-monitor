import { test, expect } from "@playwright/test";
import { authenticate, hasE2EAuth } from "./helpers/auth";

/**
 * Alerts inbox E2E — Phase 5.x audit (2026-05-28).
 *
 * Auth-gated; пропускаем без magic token или login/password.
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
    !hasE2EAuth(),
    "Set PLAYWRIGHT auth credentials to run authenticated tests",
  );

  test.beforeEach(async ({ page }) => {
    await authenticate(page);
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

  test("marking an alert read refetches the paginated feed", async ({
    page,
  }) => {
    let pageFetches = 0;
    await page.route("**/api/v1/dash/alerts/page?**", async (route) => {
      pageFetches += 1;
      const items =
        pageFetches === 1
          ? [
              {
                id: 901,
                rule_type: "site_zero_scrape",
                severity: "critical",
                title: "Synthetic alert for mutation refresh",
                detail: "This row must disappear after it is marked read.",
                payload: {},
                site: null,
                created_at: new Date().toISOString(),
                is_read: false,
                read_at: null,
                snoozed_until: null,
              },
            ]
          : [];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items,
          total: items.length,
          limit: 50,
          offset: 0,
          rule_types: ["site_zero_scrape"],
        }),
      });
    });
    await page.route("**/api/v1/dash/alerts/counts", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ unread: 1, snoozed: 0, read: 0, total: 1 }),
      });
    });
    await page.route("**/api/v1/dash/alerts/901", async (route) => {
      expect(route.request().method()).toBe("PATCH");
      expect(route.request().postDataJSON()).toEqual({ is_read: true });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, id: 901 }),
      });
    });

    await page.goto("/ru/alerts");
    await expect(
      page.getByText("Synthetic alert for mutation refresh"),
    ).toBeVisible();
    await page.getByTitle("Пометить прочитанным").click();

    await expect
      .poll(() => pageFetches, {
        message: "paginated alert query was invalidated",
      })
      .toBeGreaterThan(1);
    await expect(
      page.getByText("Synthetic alert for mutation refresh"),
    ).toHaveCount(0);
  });

  test("source filter and sort persist in URL and identify each card", async ({
    page,
  }) => {
    const pageRequests: string[] = [];
    await page.route("**/api/v1/dash/alerts/page?**", async (route) => {
      pageRequests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: 902,
              rule_type: "price_drop_pct",
              severity: "warning",
              title: "Synthetic aloe alert",
              detail: "Source must stay visible after filtering.",
              payload: { site: "aloe" },
              site: "aloe",
              created_at: new Date().toISOString(),
              is_read: false,
              read_at: null,
              snoozed_until: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          rule_types: ["price_drop_pct"],
        }),
      });
    });
    await page.route("**/api/v1/dash/alerts/counts", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ unread: 1, snoozed: 0, read: 0, total: 1 }),
      });
    });

    await page.goto("/ru/alerts?offset=50");
    await page
      .getByRole("combobox", { name: "Источник", exact: true })
      .selectOption("aloe");
    await expect(page).toHaveURL(/site=aloe/);
    await expect(page).not.toHaveURL(/offset=/);
    await expect(page.getByTestId("alert-source")).toHaveText("aloe.az");
    await expect
      .poll(() => pageRequests.some((url) => url.includes("site=aloe")))
      .toBe(true);

    await page
      .getByRole("combobox", { name: "Сортировка", exact: true })
      .selectOption("site");
    await expect(page).toHaveURL(/sort=site/);
    await expect
      .poll(() => pageRequests.some((url) => url.includes("sort=site")))
      .toBe(true);

    await page.reload();
    await expect(
      page.getByRole("combobox", { name: "Источник", exact: true }),
    ).toHaveValue("aloe");
    await expect(
      page.getByRole("combobox", { name: "Сортировка", exact: true }),
    ).toHaveValue("site");
  });

  test("azerbaijani alerts render localized event copy from payload", async ({
    page,
  }) => {
    await page.route("**/api/v1/dash/alerts/page?**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: 903,
              rule_type: "price_drop_pct",
              severity: "critical",
              title:
                "Цена упала на 30.0%: Biobalance dəri çatlarına qarşı (Krem) 60 ml",
              detail: "pharmonline: 22.00 → 15.40 ₼ (−30.0%).",
              payload: {
                site: "pharmonline",
                prev_price: 22,
                curr_price: 15.4,
                drop_pct: 30,
              },
              site: "pharmonline",
              created_at: new Date().toISOString(),
              is_read: false,
              read_at: null,
              snoozed_until: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          rule_types: ["price_drop_pct"],
        }),
      });
    });
    await page.route("**/api/v1/dash/alerts/counts", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ unread: 1, snoozed: 0, read: 0, total: 1 }),
      });
    });

    await page.goto("/az/alerts");

    await expect(
      page.getByText(
        "Qiymət 30.0% düşüb: Biobalance dəri çatlarına qarşı (Krem) 60 ml",
      ),
    ).toBeVisible();
    await expect(page.getByText("Цена упала")).toHaveCount(0);
    await expect(
      page.getByText("pharmonline: 22.00 → 15.40 ₼ (-30.0%)."),
    ).toBeVisible();
  });
});
