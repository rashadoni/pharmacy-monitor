export type QueryPatch = Record<string, string | number | boolean | null | undefined>;

export function queryWithPatch(
  current: URLSearchParams | string,
  patch: QueryPatch,
): string {
  const next = new URLSearchParams(
    typeof current === "string" ? current.replace(/^\?/, "") : current.toString(),
  );
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined || value === false || value === "") {
      next.delete(key);
    } else {
      next.set(key, value === true ? "1" : String(value));
    }
  }
  return next.toString();
}

export function choiceParam<const T extends readonly string[]>(
  params: URLSearchParams,
  key: string,
  allowed: T,
  fallback: T[number],
): T[number] {
  const value = params.get(key);
  return value !== null && (allowed as readonly string[]).includes(value)
    ? (value as T[number])
    : fallback;
}

export function integerParam(
  params: URLSearchParams,
  key: string,
  fallback: number,
  options: { allowed?: readonly number[]; min?: number; max?: number } = {},
): number {
  const raw = params.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed)) return fallback;
  if (options.allowed && !options.allowed.includes(parsed)) return fallback;
  if (options.min !== undefined && parsed < options.min) return fallback;
  if (options.max !== undefined && parsed > options.max) return fallback;
  return parsed;
}
