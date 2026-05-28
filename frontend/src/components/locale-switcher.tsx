"use client";

import { useLocale } from "next-intl";
import { useTransition } from "react";
import { localeFlags, localeNames, locales, type Locale } from "@/i18n/config";
import { useRouter, usePathname } from "@/i18n/navigation";

/**
 * Phase 6.1 retry (2026-05-28) — URL-based locale switching без middleware.
 *
 * До этого: fetch /locale → cookie + window.location.reload(). Грязный
 * full page reload, ломал scroll position, deep-link sharing не работал.
 *
 * После: next-intl createNavigation router.replace({ locale }) → push same
 * pathname с новым locale-префиксом. Client-side навигация, no reload.
 * Deep links `/az/comparison` работают через share.
 */
export function LocaleSwitcher() {
  const current = useLocale() as Locale;
  const router = useRouter();
  const pathname = usePathname();
  const [pending, startTransition] = useTransition();

  function changeLocale(next: Locale) {
    if (next === current) return;
    startTransition(() => {
      // router.replace принимает текущий pathname (без locale prefix —
      // createNavigation абстрагирует) + новый locale. Sub-pathname сохраняется:
      // если ты на /az/comparison → switch to en → /en/comparison.
      router.replace(pathname, { locale: next });
    });
  }

  return (
    <div className="inline-flex rounded-md border border-border overflow-hidden text-xs">
      {locales.map((loc) => (
        <button
          key={loc}
          type="button"
          onClick={() => changeLocale(loc)}
          disabled={pending}
          className={
            "px-3 py-1.5 transition-colors flex items-center gap-1 " +
            (loc === current
              ? "bg-primary text-primary-foreground font-semibold"
              : "bg-card text-muted-foreground hover:bg-secondary")
          }
        >
          <span>{localeFlags[loc]}</span>
          <span>{localeNames[loc]}</span>
        </button>
      ))}
    </div>
  );
}
