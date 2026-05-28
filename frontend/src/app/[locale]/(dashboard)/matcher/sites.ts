/**
 * Shared types/constants для /matcher страницы.
 *
 * Next.js не разрешает экспортировать произвольные value/type из page.tsx —
 * только default + специальные ключи (generateMetadata и т.д.). Поэтому
 * списки сайтов живут отдельным модулем.
 */
export const SITES = ["aloe", "pharmonline", "aptekonline"] as const;
export type Site = (typeof SITES)[number];

export const SITE_LABEL: Record<Site, string> = {
  aloe: "aloe.az",
  pharmonline: "pharmonline.az",
  aptekonline: "aptekonline.az",
};
