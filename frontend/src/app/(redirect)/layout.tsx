import type { Metadata, Viewport } from "next";
import "../globals.css";
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

/** Root layout used only by the locale redirect at `/`. */
export default function RedirectLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang={defaultLocale}>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
