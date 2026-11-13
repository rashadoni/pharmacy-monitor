// Хуки «своего магазина» для страниц; сам выбор и кука — viewer-site-cookie.ts.
import { useMemo, useSyncExternalStore } from "react";

import { CLIENT_SITE } from "./site-profile";
import { VIEWER_SITE_CHANGE_EVENT, readViewerSite, viewerSites } from "./viewer-site-cookie";

export {
  VIEWER_SITE_COOKIE,
  VIEWER_SITE_SELECTABLE,
  readViewerSite,
  setViewerSite,
  viewerSiteFromCookie,
  viewerSites,
} from "./viewer-site-cookie";

function subscribe(onChange: () => void): () => void {
  window.addEventListener(VIEWER_SITE_CHANGE_EVENT, onChange);
  return () => window.removeEventListener(VIEWER_SITE_CHANGE_EVENT, onChange);
}

/** Текущий «свой» сайт. На сервере и до гидрации — клиент профиля. */
export function useViewerSite(): string {
  return useSyncExternalStore(subscribe, () => readViewerSite(), () => CLIENT_SITE);
}

/** Сайты, «свой» первым. */
export function useViewerSites(): readonly string[] {
  const client = useViewerSite();
  return useMemo(() => viewerSites(client), [client]);
}
