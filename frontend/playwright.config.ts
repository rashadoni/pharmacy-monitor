import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright e2e config.
 *
 * Pre-requisites for `pnpm test:e2e`:
 *   1. FastAPI backend running on :8080 with seed data:
 *        DATABASE_URL=sqlite:///../data/db.sqlite uvicorn src.api:app --port 8080
 *   2. Next.js dev server running on :3000:
 *        pnpm dev
 *   3. Test user with magic-link enabled (or PHARMACY_AUTH_DEV_SHOW_TOKEN=1)
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [["html", { open: "never" }], ["list"]],

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-safari",
      use: { ...devices["iPhone 14"] },
    },
  ],

  webServer: process.env.CI
    ? [
        {
          command: "pnpm dev",
          url: "http://localhost:3000",
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        },
      ]
    : undefined,
});
