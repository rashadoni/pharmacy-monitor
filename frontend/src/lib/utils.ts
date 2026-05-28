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

/** ISO timestamp → "29 апр, 12:53". */
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

/** ISO timestamp → relative «5 мин назад / вчера / 2 дня назад». */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  const min = Math.round(diff / 60_000);
  const hour = Math.round(diff / 3_600_000);
  const day = Math.round(diff / 86_400_000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  if (hour < 24) return `${hour} ч назад`;
  if (day === 1) return "вчера";
  if (day < 7) return `${day} дн назад`;
  return formatTime(iso);
}
