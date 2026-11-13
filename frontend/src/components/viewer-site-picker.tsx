"use client";

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { SITES, siteTitle } from "@/lib/site-profile";
import { setViewerSite, useViewerSite } from "@/lib/viewer-site";

/**
 * «Свой магазин»: чьими глазами показывать цены (lib/viewer-site.ts).
 * Выбор хранится в куке этого браузера; страницы сравнения, категорий, обзора
 * и магазина перестраиваются сразу, тексты — после обновления с сервера.
 */
export function ViewerSitePicker() {
  const t = useTranslations("settings");
  const current = useViewerSite();
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  function choose(site: string) {
    if (site === current) return;
    setViewerSite(site);
    // Тексты интерфейса называют «свой» магазин по имени и собираются на
    // сервере по куке — перечитываем их без перезагрузки страницы.
    startTransition(() => router.refresh());
  }
  return (
    <div className="space-y-2">
      <div
        className="inline-flex flex-wrap overflow-hidden rounded-md border border-border text-sm"
        role="group"
        aria-label={t("viewer_site_title")}
      >
        {SITES.map((site) => (
          <button
            key={site}
            type="button"
            onClick={() => choose(site)}
            disabled={pending}
            aria-pressed={site === current}
            className={
              "flex min-h-11 items-center px-4 py-1.5 transition-colors md:min-h-9 " +
              (site === current
                ? "bg-primary text-primary-foreground font-semibold"
                : "bg-card text-muted-foreground hover:bg-secondary")
            }
          >
            {siteTitle(site)}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{t("viewer_site_hint")}</p>
    </div>
  );
}
