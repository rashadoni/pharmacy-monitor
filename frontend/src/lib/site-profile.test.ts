import { describe, expect, it } from "vitest";

import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import {
  ELECTRONICS,
  PHARMACY,
  PROFILE,
  SITES,
  CLIENT_SITE,
  COMPETITOR_SITES,
  IS_PHARMACY,
  messagesForProfile,
  profileFromEnvironment,
  siteDomain,
  siteTitle,
} from "./site-profile";

describe("профиль экземпляра", () => {
  it("без переменной сборка аптечная, со всеми прежними значениями", () => {
    // Значения выписаны руками: это то, что стояло в страницах до профиля.
    expect(PROFILE).toBe(PHARMACY);
    expect(SITES).toEqual(["pharmonline", "aptekonline", "aloe"]);
    expect(CLIENT_SITE).toBe("pharmonline");
    expect(COMPETITOR_SITES).toEqual(["aptekonline", "aloe"]);
    expect(IS_PHARMACY).toBe(true);
    expect(SITES.map((site) => siteDomain(site))).toEqual([
      "pharmonline.az",
      "aptekonline.az",
      "aloe.az",
    ]);
    expect(PHARMACY.pharmacyPages).toBe(true);
  });

  it("имя профиля читается без учёта регистра и пробелов", () => {
    for (const value of [undefined, "", "  ", "pharmacy", " Pharmacy "]) {
      expect(profileFromEnvironment(value)).toBe(PHARMACY);
    }
    expect(profileFromEnvironment("Electronics")).toBe(ELECTRONICS);
  });

  it("незнакомое имя — отказ, а не аптечный профиль", () => {
    expect(() => profileFromEnvironment("elektronika")).toThrow(/elektronika/);
  });

  it("электроника: клиент первым, у каждого сайта есть адрес и название", () => {
    expect(ELECTRONICS.sites).toEqual(["kontakt", "irshad", "bakuelectronics"]);
    expect(ELECTRONICS.clientSite).toBe("kontakt");
    expect(ELECTRONICS.pharmacyPages).toBe(false);
    for (const site of ELECTRONICS.sites) {
      expect(siteDomain(site, ELECTRONICS)).toMatch(/\.az$/);
      expect(siteTitle(site, ELECTRONICS)).not.toBe(site);
    }
    expect(siteDomain("unknown", ELECTRONICS)).toBe("unknown");
    expect(siteTitle("unknown", ELECTRONICS)).toBe("unknown");
  });
});

describe("тексты интерфейса под профиль", () => {
  it("аптечному профилю тексты возвращаются как есть", () => {
    expect(messagesForProfile(ru, PHARMACY)).toBe(ru);
  });

  it("имена сайтов заменяются по порядку: клиент на клиента", () => {
    const messages = {
      a: "pharmonline дешевле остальных на 5%",
      nested: {
        b: "Конкурент aptekonline.az или aloe; Pharmonline (вы)",
        c: "Pharmacy Monitor",
        d: "впишите токен в /etc/pharmacy-monitor/env",
      },
    };
    expect(messagesForProfile(messages, ELECTRONICS)).toEqual({
      a: "Kontakt Home дешевле остальных на 5%",
      nested: {
        b: "Конкурент irshad.az или Baku Electronics; Kontakt Home (вы)",
        c: "Price Monitor",
        d: "впишите токен в /etc/electro-monitor/env",
      },
    });
    // Исходный объект не тронут.
    expect(messages.a).toContain("pharmonline");
  });

  it("«свой магазин»: тексты о клиенте называют выбранный сайт", () => {
    const messages = {
      a: "pharmonline (вы) дешевле, чем aptekonline и aloe",
      b: "сайт pharmonline.az",
    };
    expect(messagesForProfile(messages, ELECTRONICS, "irshad")).toEqual({
      a: "Irshad (вы) дешевле, чем Kontakt Home и Baku Electronics",
      b: "сайт irshad.az",
    });
    expect(messagesForProfile(messages, ELECTRONICS, "bakuelectronics").a).toBe(
      "Baku Electronics (вы) дешевле, чем Kontakt Home и Irshad",
    );
    // Чужое значение — клиент профиля; аптеке выбор безразличен.
    expect(messagesForProfile(messages, ELECTRONICS, "aloe")).toEqual(
      messagesForProfile(messages, ELECTRONICS),
    );
    expect(messagesForProfile(ru, PHARMACY, "aloe")).toBe(ru);
  });

  it("слово внутри другого слова не заменяется", () => {
    expect(messagesForProfile({ a: "haloed aloes" }, ELECTRONICS)).toEqual({ a: "haloed aloes" });
  });

  it.each([
    ["ru", ru],
    ["az", az],
    ["en", en],
  ])("в текстах %s для электроники не остаётся аптечных сайтов", (_locale, messages) => {
    const rewritten = JSON.stringify(Object.values(flatten(messagesForProfile(messages, ELECTRONICS))));
    expect(rewritten).not.toMatch(/pharmonline|aptekonline|\baloe\b|Pharmacy Monitor|pharmacy-monitor/i);
    // Ключи те же: страницы ищут тексты по прежним именам.
    expect(Object.keys(flatten(messagesForProfile(messages, ELECTRONICS)))).toEqual(
      Object.keys(flatten(messages)),
    );
  });
});

function flatten(value: unknown, prefix = ""): Record<string, string> {
  if (typeof value === "string") return { [prefix]: value };
  return Object.entries(value as Record<string, unknown>).reduce(
    (out, [key, inner]) => ({ ...out, ...flatten(inner, prefix ? `${prefix}.${key}` : key) }),
    {} as Record<string, string>,
  );
}
