/**
 * Phase 6.1 retry (2026-05-28) — locale-aware navigation helpers.
 *
 * createNavigation работает БЕЗ middleware — это просто client-side helpers
 * для построения locale-aware URLs. Routing matching делает Next.js native
 * через app/[locale]/... структуру.
 *
 * Используем 'always' префикс potому что в нашей setup НЕТ middleware → все
 * URLs ЯВНО префиксованы (включая default ru). Backward compat для
 * unprefixed URLs хэндлится next.config.mjs redirects, не middleware.
 *
 * Usage:
 *   import { Link, useRouter, usePathname } from "@/i18n/navigation";
 *   <Link href="/comparison">→ Comparison</Link>   // → /<current-locale>/comparison
 *   const router = useRouter(); router.replace("/overview", { locale: "en" });
 */
import { createNavigation } from "next-intl/navigation";
import { defaultLocale, locales } from "./config";

export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation({
    locales,
    defaultLocale,
    // 'always' — без middleware нет route-matching магии, поэтому проще
    // ВСЕГДА указывать locale в URL (включая /ru/). Внутренние <Link> будут
    // строить корректные /ru/foo, /az/foo автоматически.
    localePrefix: "always",
  });
