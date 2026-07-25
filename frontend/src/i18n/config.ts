/**
 * i18n configuration via next-intl.
 *
 * Locales: az (default), ru, en.
 *
 * Routing strategy: locale prefix in path (e.g. /ru/comparison, /az/comparison).
 * Default locale (az) is used for root and legacy unprefixed routes.
 */
export const locales = ["ru", "az", "en"] as const;
export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = "az";

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
