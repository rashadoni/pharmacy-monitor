// Порядок колонок сайтов на странице сравнения; клиент — первым.
// Список — из профиля экземпляра (src/lib/site-profile.ts).
import { SITES as PROFILE_SITES } from "@/lib/site-profile";

export const SITES: readonly string[] = PROFILE_SITES;
export type SiteName = string;
