import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** AZN-formatted price. Returns "—" if null/0. */
export function formatPrice(value: number | null | undefined): string {
  if (value == null || value === 0) return "—";
  return new Intl.NumberFormat("ru-RU", {
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

/** ISO timestamp → "29 Apr, 12:53". */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
