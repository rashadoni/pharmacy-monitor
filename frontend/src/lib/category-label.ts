export interface LocalizedCategoryLabel {
  key?: string;
  label_ru?: string | null;
  label_az?: string | null;
}

function nonEmpty(value: string | null | undefined): string | null {
  const normalized = value?.trim();
  return normalized ? normalized : null;
}

/** Resolve DB-backed category copy consistently across every localized route. */
export function categoryDisplayLabel(
  category: LocalizedCategoryLabel,
  locale: string,
  fallback?: string,
): string {
  const ru = nonEmpty(category.label_ru);
  const az = nonEmpty(category.label_az);
  if (locale === "az") return az ?? fallback ?? category.key ?? "—";
  return ru ?? az ?? fallback ?? category.key ?? "—";
}
