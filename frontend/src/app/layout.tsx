/**
 * Phase 6.1 retry (2026-05-28) — root layout.
 *
 * Минимальный passthrough — все локалёзависимые вещи переехали в
 * app/[locale]/layout.tsx. Root отвечает только за <html>/<body> и global CSS.
 *
 * `lang` ставится в defaultLocale (ru) для path'ов БЕЗ locale-префикса
 * (которые next.config.mjs redirects пересылают на /ru/...). После redirect'а
 * [locale]/layout.tsx не может перезаписать <html lang="">, поэтому
 * accessibility tools на момент redirect'а видят lang=ru. После redirect
 * локализованная страница рендерится с правильным контентом через
 * NextIntlClientProvider, hreflang URL'ы корректны.
 */
import type { Metadata, Viewport } from "next";
import "./globals.css";
import { defaultLocale } from "@/i18n/config";

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

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang={defaultLocale} suppressHydrationWarning>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
