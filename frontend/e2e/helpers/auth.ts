import { expect, type Page } from "@playwright/test";

export function hasE2EAuth(): boolean {
  return Boolean(
    process.env.PLAYWRIGHT_AUTH_TOKEN ||
      (process.env.PLAYWRIGHT_AUTH_LOGIN && process.env.PLAYWRIGHT_AUTH_PASSWORD),
  );
}

export async function authenticate(page: Page) {
  const token = process.env.PLAYWRIGHT_AUTH_TOKEN;
  if (token) {
    await page.goto(`/auth/verify?token=${encodeURIComponent(token)}`);
    await page.waitForURL(/\/overview/, { timeout: 10_000 });
    return;
  }

  const login = process.env.PLAYWRIGHT_AUTH_LOGIN;
  const password = process.env.PLAYWRIGHT_AUTH_PASSWORD;
  if (!login || !password) {
    throw new Error(
      "Set PLAYWRIGHT_AUTH_TOKEN or PLAYWRIGHT_AUTH_LOGIN/PLAYWRIGHT_AUTH_PASSWORD",
    );
  }

  const res = await page.context().request.post("/auth/login", {
    data: { login, password },
  });
  expect(res.ok(), await res.text()).toBe(true);
}
