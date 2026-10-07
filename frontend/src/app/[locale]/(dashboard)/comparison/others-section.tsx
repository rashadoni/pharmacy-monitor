"use client";

import { useLocale, useTranslations } from "next-intl";
import type { ComparisonOther } from "@/lib/api";
import { formatPrice } from "@/lib/utils";
import { SITES } from "./sites";

/**
 * Найдено поиском, но в таблице сравнения нет: у товара пока нет пары на другом
 * сайте — или пара есть, но её строку скрыл фильтр страницы. Без этого блока
 * такой товар для пользователя «не находится», хотя на сайте он есть (жалоба
 * клиента 2026-10-06: veqovi, ozempik, kreon).
 */
export function OthersSection({
  others,
  total,
}: {
  others: ComparisonOther[];
  total: number;
}) {
  const t = useTranslations("comparison");
  return (
    <section className="space-y-2" data-testid="search-others">
      <div>
        <h2 className="text-base font-semibold">
          {t("others_title", { count: total })}
        </h2>
        <p className="text-sm text-muted-foreground">{t("others_hint")}</p>
      </div>

      {/* Mobile: карточки */}
      <div className="md:hidden space-y-2">
        {others.map((o) => (
          <div
            key={o.product_id}
            className="flex items-start justify-between gap-3 rounded-lg border border-border bg-card p-3"
          >
            <div className="min-w-0">
              <div className="text-sm font-medium">{o.name}</div>
              <div className="mt-0.5 text-[10px] uppercase text-muted-foreground">
                {o.site}
              </div>
            </div>
            <OtherPrice other={o} />
          </div>
        ))}
      </div>

      {/* Desktop: те же колонки сайтов, что в таблице сравнения */}
      <div className="hidden md:block rounded-lg border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">{t("th_name")}</th>
              {SITES.map((s) => (
                <th key={s} className="px-3 py-2 text-right font-medium">
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {others.map((o) => (
              <tr
                key={o.product_id}
                className="border-t border-border hover:bg-muted/30"
              >
                <td className="px-3 py-2 max-w-md truncate">{o.name}</td>
                {SITES.map((s) => (
                  <td key={s} className="px-3 py-2 text-right">
                    {o.site === s ? (
                      <OtherPrice other={o} />
                    ) : (
                      <span className="text-muted-foreground/50">—</span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > others.length && (
        <p className="text-xs text-muted-foreground">
          {t("others_truncated", { shown: others.length, total })}
        </p>
      )}
    </section>
  );
}

function OtherPrice({ other }: { other: ComparisonOther }) {
  const t = useTranslations("comparison");
  const locale = useLocale();
  const outOfStock = other.availability_status === "out_of_stock";
  // Страна — часть идентичности товара (разная страна = разный товар), поэтому
  // показываем её так же, как в таблице сравнения.
  const verifiedCountry =
    other.country_resolution_status === "resolved" && other.country_code
      ? other.country_code.toUpperCase()
      : null;
  const faded = other.stale || outOfStock;
  return (
    <a
      href={other.url}
      target="_blank"
      rel="noopener noreferrer"
      className={`inline-flex shrink-0 flex-col items-end tabular-nums leading-tight hover:underline ${
        faded ? "text-muted-foreground/70" : ""
      }`}
    >
      <span>{formatPrice(other.price, locale)}</span>
      <span
        className={`mt-0.5 rounded-sm border px-1 py-px text-[9px] font-medium leading-none ${
          verifiedCountry
            ? "border-border text-muted-foreground"
            : "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-400"
        }`}
        title={
          verifiedCountry
            ? t("country_verified", { code: verifiedCountry })
            : t("country_unverified")
        }
      >
        {verifiedCountry ?? t("country_unknown_short")}
      </span>
      {outOfStock && (
        <span className="text-[10px] font-normal text-amber-600 dark:text-amber-500">
          {t("out_of_stock")}
        </span>
      )}
      {other.stale && other.age_days != null && (
        <span className="text-[10px] font-normal text-amber-600 dark:text-amber-500">
          {t("stale_badge", { days: other.age_days })}
        </span>
      )}
    </a>
  );
}
