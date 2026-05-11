/**
 * next-intl request config — cookie-based locale (no route prefix).
 *
 * Reads `pm_locale` cookie set by the LocaleSwitcher. Falls back to default.
 * No URL changes needed — same /comparison works for all locales.
 */
import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";
import { defaultLocale, locales, type Locale } from "./config";

export default getRequestConfig(async () => {
  const cookieStore = await cookies();
  const fromCookie = cookieStore.get("pm_locale")?.value as Locale | undefined;
  const locale: Locale =
    fromCookie && (locales as readonly string[]).includes(fromCookie)
      ? fromCookie
      : defaultLocale;

  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: "Asia/Baku",
    now: new Date(),
  };
});
