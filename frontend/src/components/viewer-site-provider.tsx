"use client";

import type { ReactNode } from "react";
import { ViewerSiteContext } from "@/lib/viewer-site";

/**
 * «Свой магазин», прочитанный сервером из куки, — значение для первого
 * рендера и гидрации (lib/viewer-site.ts). Без него страница сначала
 * запрашивала бы данные клиента профиля, а тексты уже называли бы выбранный
 * магазин.
 */
export function ViewerSiteProvider({ site, children }: { site: string; children: ReactNode }) {
  return <ViewerSiteContext.Provider value={site}>{children}</ViewerSiteContext.Provider>;
}
