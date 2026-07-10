"use client";

import { useTranslations } from "next-intl";

/**
 * Маленький SVG sparkline без зависимостей. Показывает динамику цены за N дней
 * + ниже — числовая delta_pct ("+5.2%" / "-3.1%").
 *
 * Цвет линии:
 *   - red если delta положительная (цена выросла — плохо для клиента)
 *   - green если отрицательная (цена упала — хорошо)
 *   - muted если изменение <0.5%
 */
export function Sparkline({
  points,
  delta_pct,
  width = 80,
  height = 24,
}: {
  points: { date: string; price: number | null }[];
  delta_pct: number | null;
  width?: number;
  height?: number;
}) {
  const t = useTranslations("common");
  const vals = points.map((p) => p.price).filter((v): v is number => v != null);
  if (vals.length < 2) {
    return (
      <div
        className="inline-flex items-center text-[10px] text-muted-foreground/70"
        style={{ width }}
        aria-label={t("insufficient_trend_data")}
      >
        —
      </div>
    );
  }

  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const stepX = width / (vals.length - 1);

  const path = vals
    .map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");

  const colorClass =
    delta_pct == null || Math.abs(delta_pct) < 0.5
      ? "text-muted-foreground"
      : delta_pct > 0
        ? "text-destructive"
        : "text-success";

  return (
    <div className="inline-flex flex-col items-end gap-0.5">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className={colorClass}
        aria-hidden
      >
        <path
          d={path}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {/* last point dot */}
        <circle
          cx={(vals.length - 1) * stepX}
          cy={height - ((vals[vals.length - 1] - min) / range) * height}
          r="2"
          fill="currentColor"
        />
      </svg>
      {delta_pct != null && (
        <span className={`text-[10px] tabular-nums ${colorClass}`}>
          {delta_pct > 0 ? "+" : ""}
          {delta_pct.toFixed(1)}%
        </span>
      )}
    </div>
  );
}
