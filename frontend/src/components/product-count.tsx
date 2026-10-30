"use client";

import React from "react";
import { useLocale, useTranslations } from "next-intl";

import type { ProductCount } from "../lib/run-counts";
import { formatNumber } from "../lib/utils";

/**
 * Число товаров прогона с подписью (`lib/run-counts`). Главное число — разные
 * товары; строки листинга, если их больше, — вторым, мелко. У прогона, записанного
 * до подсчёта разных товаров, главное число — строки листинга, и под ним так и
 * написано: иначе 96 347 строк aptekonline снова читались бы как 96 347 товаров.
 */
export function ProductCountText({
  count,
  inline = false,
}: {
  count: ProductCount | null;
  inline?: boolean;
}) {
  const t = useTranslations("run_counts");
  const locale = useLocale();
  if (!count) return <span className="text-muted-foreground">—</span>;

  const note = count.distinct
    ? count.listingRows != null
      ? t("listing_rows", { count: count.listingRows })
      : null
    : t("listing_rows_unit");
  const title = count.distinct ? (note ? t("hint") : undefined) : t("hint_legacy");

  return (
    <span
      className={
        inline
          ? "inline-flex flex-wrap items-baseline gap-x-1"
          : "inline-flex flex-col items-end"
      }
      title={title}
    >
      <span className="tabular-nums">{formatNumber(count.value, locale)}</span>
      {note && (
        <span className="text-xs font-normal text-muted-foreground tabular-nums">
          {inline ? `· ${note}` : note}
        </span>
      )}
    </span>
  );
}
