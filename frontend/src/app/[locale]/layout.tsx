/**
 * Phase 6.1 retry (2026-05-28) — layout-based locale.
 *
 * Strategy: locale comes from URL params (app/[locale]/...). NO middleware
 * — instead next.config.mjs `redirects()` handles backward compat for
 * unprefixed legacy URLs at framework-level.
 *
 * This avoids next-intl issue #524 — standalone build interpreting
 * `x-middleware-rewrite` header as HTTP proxy → recursive ECONNRESET.
 *
 * Verified path per next-intl maintainer recommendation in discussion #2048
 * + Perplexity Sonar Pro citations.
 */
import { NextIntlClientProvider } from "next-intl";
import { getMessages, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { Providers } from "@/components/providers";
import { locales, type Locale } from "@/i18n/config";

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

function isLocale(value: string): value is Locale {
  return (locales as readonly string[]).includes(value);
}

interface LocaleLayoutProps {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}

export default async function LocaleLayout({
  children,
  params,
}: LocaleLayoutProps) {
  const { locale } = await params;

  // Validate locale — invalid (e.g. user typed /foo) → 404.
  if (!isLocale(locale)) {
    notFound();
  }

  // Enable static rendering for this locale segment (next-intl 3.22+ pattern).
  setRequestLocale(locale);

  const messages = await getMessages();

  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      <Providers>{children}</Providers>
    </NextIntlClientProvider>
  );
}
