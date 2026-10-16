// Порядок колонок сайтов на странице сравнения; клиент — первым.
export const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
export type SiteName = (typeof SITES)[number];
