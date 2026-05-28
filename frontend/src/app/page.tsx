/**
 * Phase 6.1 retry (2026-05-28) — root redirect to default locale.
 *
 * / → /ru (default locale). /ru/page.tsx сам redirect-нет на /ru/overview.
 *
 * Если пользователь предпочитает другой locale, он переключит через
 * LocaleSwitcher → router.replace на /az/... или /en/...
 */
import { redirect } from "next/navigation";
import { defaultLocale } from "@/i18n/config";

export default function RootPage() {
  redirect(`/${defaultLocale}`);
}
