// «Чьими глазами» показывать цены: какой магазин страницы считают своим.
//
// Экземпляр электроники показывают каждому из трёх магазинов — сегодня Kontakt
// Home, завтра Irshad или Baku Electronics. Выбор хранится в браузере
// (localStorage) и меняется в настройках; по умолчанию — клиент профиля. У
// аптечного экземпляра клиент один, и выбор не действует.
//
// Сервер считает с точки зрения любого сайта сам (`client_site` в запросах,
// `site_profile.viewer_sites` на сервере); здесь — только какой сайт просить.
import { useMemo, useSyncExternalStore } from "react";

import { CLIENT_SITE, IS_PHARMACY, SITES, isProfileSite } from "./site-profile";

export const VIEWER_SITE_STORAGE_KEY = "pm.viewerSite";
const CHANGE_EVENT = "pm:viewer-site";

/** Можно ли в этом экземпляре выбирать «свой» сайт. */
export const VIEWER_SITE_SELECTABLE = !IS_PHARMACY && SITES.length > 1;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** Выбранный «свой» сайт; без выбора, с чужим значением или у аптеки — клиент профиля. */
export function readViewerSite(store: Pick<Storage, "getItem"> | null = storage()): string {
  if (!VIEWER_SITE_SELECTABLE || store === null) return CLIENT_SITE;
  let value: string | null = null;
  try {
    value = store.getItem(VIEWER_SITE_STORAGE_KEY);
  } catch {
    return CLIENT_SITE;
  }
  return value !== null && isProfileSite(value) ? value : CLIENT_SITE;
}

export function setViewerSite(site: string): void {
  if (!VIEWER_SITE_SELECTABLE || !isProfileSite(site)) return;
  const store = storage();
  try {
    if (site === CLIENT_SITE) store?.removeItem(VIEWER_SITE_STORAGE_KEY);
    else store?.setItem(VIEWER_SITE_STORAGE_KEY, site);
  } catch {
    // Хранилище закрыто (приватное окно): выбор не сохранить, страницы
    // остаются на клиенте профиля.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** Сайты экземпляра, «свой» первым, остальные в порядке профиля. */
export function viewerSites(client: string): readonly string[] {
  return [client, ...SITES.filter((site) => site !== client)];
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
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
