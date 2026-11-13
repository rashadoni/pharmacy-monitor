// Хуки «своего магазина» для страниц; сам выбор и кука — viewer-site-cookie.ts.
import { createContext, useContext, useMemo, useSyncExternalStore } from "react";

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

/**
 * «Свой» сайт, прочитанный сервером из куки (components/viewer-site-provider):
 * значение на сервере и при гидрации. Без провайдера — клиент профиля.
 */
export const ViewerSiteContext = createContext<string>(CLIENT_SITE);

/** Текущий «свой» сайт; на сервере и до гидрации — тот, что прочёл сервер. */
export function useViewerSite(): string {
  const fromServer = useContext(ViewerSiteContext);
  return useSyncExternalStore(subscribe, () => readViewerSite(), () => fromServer);
}

/** Сайты, «свой» первым. */
export function useViewerSites(): readonly string[] {
  const client = useViewerSite();
  return useMemo(() => viewerSites(client), [client]);
}
