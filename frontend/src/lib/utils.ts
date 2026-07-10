import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** AZN-formatted price. Returns "—" if null/0. */
export function formatPrice(value: number | null | undefined, locale: string): string {
  if (value == null || value === 0) return "—";
  return new Intl.NumberFormat(intlLocale(locale), {
    style: "currency",
    currency: "AZN",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

/** Percentage with sign: "+44.9%" / "-15.9%". */
export function formatPct(value: number | null | undefined): string {
  if (value == null) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

export function intlLocale(locale: string): string {
  if (locale === "az") return "az-AZ";
  if (locale === "en") return "en-GB";
  return "ru-RU";
}

export function formatNumber(value: number, locale: string): string {
  return new Intl.NumberFormat(intlLocale(locale)).format(value);
}

export function formatClock(value: Date, locale: string): string {
  return value.toLocaleTimeString(intlLocale(locale), {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** ISO timestamp formatted in the active UI locale. */
export function formatTime(iso: string | null | undefined, locale = "ru"): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(intlLocale(locale), {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** ISO timestamp formatted as a locale-aware relative time. */
export function formatRelative(iso: string | null | undefined, locale = "ru"): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = d.getTime() - Date.now();
  const abs = Math.abs(diff);
  const relative = new Intl.RelativeTimeFormat(intlLocale(locale), { numeric: "auto" });
  if (abs < 60_000) return relative.format(0, "second");
  if (abs < 3_600_000) return relative.format(Math.round(diff / 60_000), "minute");
  if (abs < 86_400_000) return relative.format(Math.round(diff / 3_600_000), "hour");
  if (abs < 7 * 86_400_000) return relative.format(Math.round(diff / 86_400_000), "day");
  return formatTime(iso, locale);
}
