/**
 * Сколько товаров увидел прогон — числом, которое понятно человеку.
 *
 * `products_per_site` и `products_scraped` считают строки листинга: товар,
 * стоящий в нескольких категориях сайта, приходит из каждой. У aptekonline это
 * 96 347 строк на 27 097 товаров (прогон №1141), и в колонке «Товары» такое
 * число читалось как размер каталога. Бэкенд эти счётчики не меняет — на них
 * стоят базы прогона и health, — а число разных товаров отдаёт рядом, в
 * `products_distinct_per_site`. У прогонов, записанных до него, разных товаров
 * нет: тогда показываются строки листинга, и экран так их и подписывает.
 */

export interface RunCounters {
  products_scraped?: number | null;
  products_per_site?: Record<string, number> | null;
  products_distinct_per_site?: Record<string, number> | null;
}

export interface ProductCount {
  /** Главное число: разные товары, а у старого прогона — строки листинга. */
  value: number;
  /** `true` — `value` это разные товары; `false` — строки листинга. */
  distinct: boolean;
  /** Строки листинга, когда их больше, чем разных товаров; иначе `null`. */
  listingRows: number | null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function withListing(distinct: number, rows: number | null): ProductCount {
  return {
    value: distinct,
    distinct: true,
    listingRows: rows != null && rows > distinct ? rows : null,
  };
}

/** Число товаров одного сайта в прогоне. */
export function siteProductCount(
  run: RunCounters | null | undefined,
  site: string,
): ProductCount | null {
  if (!run) return null;
  const rows = count(run.products_per_site?.[site]);
  const distinct = count(run.products_distinct_per_site?.[site]);
  if (distinct != null) return withListing(distinct, rows);
  const legacy = rows ?? count(run.products_scraped);
  return legacy == null ? null : { value: legacy, distinct: false, listingRows: null };
}

/**
 * Число товаров всего прогона. Сайты не пересекаются, поэтому разные товары
 * складываются — но только когда они известны у каждого собранного сайта.
 */
export function runProductCount(
  run: RunCounters | null | undefined,
): ProductCount | null {
  if (!run) return null;
  const rows = count(run.products_scraped);
  const perSite = run.products_per_site ?? {};
  const sites = Object.keys(perSite).filter((site) => (count(perSite[site]) ?? 0) > 0);
  const distinctBySite = sites.map((site) => count(run.products_distinct_per_site?.[site]));
  if (sites.length > 0 && distinctBySite.every((value) => value != null)) {
    const total = distinctBySite.reduce<number>((sum, value) => sum + (value ?? 0), 0);
    return withListing(total, rows);
  }
  return rows == null ? null : { value: rows, distinct: false, listingRows: null };
}
