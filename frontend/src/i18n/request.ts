/**
 * Phase 6.1 retry (2026-05-28) — URL-based locale via requestLocale.
 *
 * next-intl 3.22+ pattern: read locale from URL segment (set by Next.js
 * routing → app/[locale]/...). NO middleware involved.
 *
 * Cookie fallback оставлен на migration window — старые сессии с
 * pm_locale cookie могли остаться, для не-locale-routes (legacy redirect
 * stubs если будут) cookie ещё работает.
 */
import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";
import { defaultLocale, locales, type Locale } from "./config";

function isLocale(value: string | undefined): value is Locale {
  return value !== undefined && (locales as readonly string[]).includes(value);
}

export default getRequestConfig(async ({ requestLocale }) => {
  // 1. URL-derived locale (set by app/[locale]/... routing)
  const fromUrl = await requestLocale;
  if (isLocale(fromUrl)) {
    return await loadConfig(fromUrl);
  }

  // 2. Legacy cookie fallback (migration window — старые сессии)
  const cookieStore = await cookies();
  const fromCookie = cookieStore.get("pm_locale")?.value;
  if (isLocale(fromCookie)) {
    return await loadConfig(fromCookie);
  }

  // 3. Default
  return await loadConfig(defaultLocale);
});

async function loadConfig(locale: Locale) {
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: "Asia/Baku",
    now: new Date(),
  };
}
