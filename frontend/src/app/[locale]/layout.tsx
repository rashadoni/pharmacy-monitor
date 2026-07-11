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
import type { Metadata, Viewport } from "next";
import { Providers } from "@/components/providers";
import { locales, type Locale } from "@/i18n/config";
import "../globals.css";

export const metadata: Metadata = {
  title: "Pharmacy Monitor",
  description: "Daily competitive monitoring for AZ pharmacies",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

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

  // Pass the URL locale explicitly. Without it, a legacy `pm_locale` cookie can
  // win during standalone/RSC rendering and produce Russian messages under an
  // `/az/...` URL even though `<html lang>` is already `az`.
  const messages = await getMessages({ locale });

  return (
    <html lang={locale} suppressHydrationWarning>
      <body className="min-h-screen antialiased">
        <NextIntlClientProvider locale={locale} messages={messages}>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
