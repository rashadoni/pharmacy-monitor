/**
 * Phase 6.1 retry (2026-05-28) — locale-aware home redirect.
 *
 * /ru → /ru/overview
 * /az → /az/overview
 * /en → /en/overview
 *
 * Locale из URL params (по contract'у app/[locale]/page.tsx). Используем
 * standard next/navigation redirect — оно работает БЕЗ middleware, просто
 * 307 redirect.
 */
import { redirect } from "next/navigation";

interface HomeProps {
  params: Promise<{ locale: string }>;
}

export default async function HomePage({ params }: HomeProps) {
  const { locale } = await params;
  redirect(`/${locale}/overview`);
}
