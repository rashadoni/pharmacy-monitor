"use client";

import { useLocale, useTranslations } from "next-intl";
import { useTransition } from "react";
import { localeFlags, localeNames, locales, type Locale } from "@/i18n/config";

export function LocaleSwitcher() {
  const current = useLocale() as Locale;
  const [pending, startTransition] = useTransition();

  function changeLocale(next: Locale) {
    if (next === current) return;
    startTransition(async () => {
      const res = await fetch("/api/locale", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ locale: next }),
      });
      if (res.ok) {
        window.location.reload();
      }
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
