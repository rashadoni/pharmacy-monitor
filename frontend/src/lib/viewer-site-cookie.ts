// «Чьими глазами» показывать цены: какой магазин страницы считают своим.
//
// Экземпляр электроники показывают каждому из трёх магазинов — сегодня Kontakt
// Home, завтра Irshad или Baku Electronics. Выбор хранится в куке браузера
// (`pm_viewer_site`) и меняется в настройках; по умолчанию — клиент профиля. У
// аптечного экземпляра клиент один, и выбор не действует.
//
// Кука, а не localStorage: её читает и сервер — тексты интерфейса называют
// «свой» магазин по имени (i18n/request.ts → messagesForProfile). Данные
// сервер считает с точки зрения любого сайта сам (`client_site` в запросах,
// `site_profile.viewer_sites`); здесь — только какой сайт просить.
//
// Этот модуль без React: его импортирует и сервер (i18n/request.ts). Хуки —
// в viewer-site.ts.
import { CLIENT_SITE, IS_PHARMACY, SITES, isProfileSite } from "./site-profile";

export const VIEWER_SITE_COOKIE = "pm_viewer_site";
const CHANGE_EVENT = "pm:viewer-site";
const ONE_YEAR_SECONDS = 365 * 24 * 60 * 60;

/** Можно ли в этом экземпляре выбирать «свой» сайт. */
export const VIEWER_SITE_SELECTABLE = !IS_PHARMACY && SITES.length > 1;

/** Значение куки «свой магазин», проверенное по профилю; иначе клиент профиля. */
export function viewerSiteFromCookie(value: string | null | undefined): string {
  if (!VIEWER_SITE_SELECTABLE || !value) return CLIENT_SITE;
  return isProfileSite(value) ? value : CLIENT_SITE;
}

/** Выбранный «свой» сайт из строки `document.cookie`. */
export function readViewerSite(cookieHeader: string | null = documentCookies()): string {
  if (cookieHeader === null) return CLIENT_SITE;
  for (const part of cookieHeader.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name !== VIEWER_SITE_COOKIE) continue;
    try {
      return viewerSiteFromCookie(decodeURIComponent(rest.join("=")));
    } catch {
      // Битое значение («%») — как нет выбора; страница не должна падать.
      return CLIENT_SITE;
    }
  }
  return CLIENT_SITE;
}

function documentCookies(): string | null {
  try {
    return typeof document === "undefined" ? null : document.cookie;
  } catch {
    return null;
  }
}

export function setViewerSite(site: string): void {
  if (!VIEWER_SITE_SELECTABLE || !isProfileSite(site)) return;
  const age = site === CLIENT_SITE ? 0 : ONE_YEAR_SECONDS;
  document.cookie =
    `${VIEWER_SITE_COOKIE}=${encodeURIComponent(site)}; path=/; max-age=${age}; SameSite=Lax` +
    (window.location.protocol === "https:" ? "; Secure" : "");
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** Сайты экземпляра, «свой» первым, остальные в порядке профиля. */
export function viewerSites(client: string): readonly string[] {
  return [client, ...SITES.filter((site) => site !== client)];
}

export const VIEWER_SITE_CHANGE_EVENT = CHANGE_EVENT;
