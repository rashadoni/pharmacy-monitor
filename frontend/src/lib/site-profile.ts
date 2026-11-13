/**
 * Профиль экземпляра в интерфейсе: какие сайты он показывает и кто из них клиент.
 *
 * Зеркало `src/site_profile.py`. Один и тот же код обслуживает разных клиентов
 * отдельными экземплярами: у каждого своя сборка интерфейса, и профиль
 * вшивается в неё при сборке переменной `NEXT_PUBLIC_PM_PROFILE`. Без
 * переменной экземпляр аптечный — аптечная сборка её не задаёт, и для неё этот
 * модуль не меняет ничего.
 *
 * Модуль без импортов через `@/`: его читают и тесты vitest, у которых алиаса нет.
 */

export type SiteProfile = {
  name: string;
  /** Сайт клиента — первым: это порядок колонок сравнения. */
  sites: readonly string[];
  clientSite: string;
  /** Подпись сайта в меню и фильтрах — его адрес. */
  domains: Readonly<Record<string, string>>;
  /** Название магазина в тексте. */
  titles: Readonly<Record<string, string>>;
  appName: string;
  /** Файл окружения экземпляра на сервере — его называют подсказки настроек. */
  envFile: string;
  /**
   * Страницы, построенные на аптечной модели данных: связки категорий трёх
   * сайтов, закреплённые ссылки с колонкой на сайт, ручное сопоставление по
   * аптечным правилам. У другого профиля их нет в меню.
   */
  pharmacyPages: boolean;
};

export const PHARMACY: SiteProfile = {
  name: "pharmacy",
  sites: ["pharmonline", "aptekonline", "aloe"],
  clientSite: "pharmonline",
  domains: {
    pharmonline: "pharmonline.az",
    aptekonline: "aptekonline.az",
    aloe: "aloe.az",
  },
  titles: { pharmonline: "Pharmonline", aptekonline: "Aptekonline", aloe: "Aloe" },
  appName: "Pharmacy Monitor",
  envFile: "/etc/pharmacy-monitor/env",
  pharmacyPages: true,
};

export const ELECTRONICS: SiteProfile = {
  name: "electronics",
  sites: ["kontakt", "irshad", "bakuelectronics"],
  clientSite: "kontakt",
  domains: {
    kontakt: "kontakt.az",
    irshad: "irshad.az",
    bakuelectronics: "bakuelectronics.az",
  },
  titles: { kontakt: "Kontakt Home", irshad: "Irshad", bakuelectronics: "Baku Electronics" },
  appName: "Price Monitor",
  envFile: "/etc/electro-monitor/env",
  pharmacyPages: false,
};

const PROFILES: Readonly<Record<string, SiteProfile>> = {
  [PHARMACY.name]: PHARMACY,
  [ELECTRONICS.name]: ELECTRONICS,
};

/**
 * Профиль по значению переменной; пусто — аптечный, незнакомое имя — отказ.
 * Отказ, а не аптечный по умолчанию: сборка экземпляра электроники с опечаткой
 * в переменной показывала бы аптечные сайты поверх чужих данных.
 */
export function profileFromEnvironment(value: string | undefined): SiteProfile {
  const name = (value ?? "").trim().toLowerCase() || PHARMACY.name;
  const profile = PROFILES[name];
  if (!profile) {
    throw new Error(
      `NEXT_PUBLIC_PM_PROFILE=${JSON.stringify(name)} is not a known profile ` +
        `(known: ${Object.keys(PROFILES).sort().join(", ")})`,
    );
  }
  return profile;
}

export const PROFILE: SiteProfile = profileFromEnvironment(process.env.NEXT_PUBLIC_PM_PROFILE);

export const SITES: readonly string[] = PROFILE.sites;
export const CLIENT_SITE: string = PROFILE.clientSite;
export const COMPETITOR_SITES: readonly string[] = PROFILE.sites.filter(
  (site) => site !== PROFILE.clientSite,
);
export const IS_PHARMACY: boolean = PROFILE.name === PHARMACY.name;

export function isProfileSite(site: string, profile: SiteProfile = PROFILE): boolean {
  return profile.sites.includes(site);
}

/** Адрес сайта для меню и фильтров; незнакомый сайт подписывается своим именем. */
export function siteDomain(site: string, profile: SiteProfile = PROFILE): string {
  return profile.domains[site] ?? site;
}

/** Название магазина; незнакомый сайт подписывается своим именем. */
export function siteTitle(site: string, profile: SiteProfile = PROFILE): string {
  return profile.titles[site] ?? site;
}

type Messages = { [key: string]: string | Messages };

/**
 * Тексты интерфейса написаны для аптечного экземпляра и называют его сайты
 * прямо («pharmonline дешевле остальных на 5%»). Для другого профиля имена
 * заменяются по порядку сайтов: клиент на клиента, первый конкурент на первого.
 * Аптечному профилю тексты возвращаются как есть.
 */
export function messagesForProfile<T>(messages: T, profile: SiteProfile = PROFILE): T {
  if (profile.name === PHARMACY.name) return messages;

  const replacements: [RegExp, string][] = [];
  PHARMACY.sites.forEach((pharmacySite, position) => {
    const site = profile.sites[position];
    if (!site) return;
    // Сначала адрес, потом имя: «pharmonline.az» не должен стать «Kontakt Home.az».
    replacements.push([new RegExp(escapeRegExp(PHARMACY.domains[pharmacySite]), "gi"), profile.domains[site]]);
    replacements.push([new RegExp(`\\b${escapeRegExp(pharmacySite)}\\b`, "gi"), profile.titles[site]]);
  });
  // Раньше названия: «/etc/pharmacy-monitor/env» не должен попасть под замену имени.
  replacements.unshift([new RegExp(escapeRegExp(PHARMACY.envFile), "g"), profile.envFile]);
  replacements.push([new RegExp(escapeRegExp(PHARMACY.appName), "gi"), profile.appName]);

  const rewrite = (value: string | Messages): string | Messages => {
    if (typeof value === "string") {
      return replacements.reduce((text, [pattern, to]) => text.replace(pattern, to), value);
    }
    return Object.fromEntries(Object.entries(value).map(([key, inner]) => [key, rewrite(inner)]));
  };
  return rewrite(messages as unknown as Messages) as unknown as T;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
