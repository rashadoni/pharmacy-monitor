/**
 * i18n configuration via next-intl.
 *
 * Locales: ru (default), az, en.
 *
 * Routing strategy: locale prefix in path (e.g. /ru/comparison, /az/comparison).
 * Default locale (ru) can be either prefixed or root — see middleware.
 */
export const locales = ["ru", "az", "en"] as const;
export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = "ru";

export const localeNames: Record<Locale, string> = {
  ru: "Русский",
  az: "Azərbaycan",
  en: "English",
};

export const localeFlags: Record<Locale, string> = {
  ru: "🇷🇺",
  az: "🇦🇿",
  en: "🇬🇧",
};
