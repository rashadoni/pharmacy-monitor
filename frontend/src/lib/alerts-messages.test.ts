import { describe, expect, it } from "vitest";
import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";

// Подписи событий на странице алертов строятся из payload на трёх языках
// (alert-copy.ts): ключ, забытый в одной локали, next-intl покажет как
// «alerts.event_promo_drop_title», а подстановку с другим именем — как «{regular}».
const ALERTS: Record<string, Record<string, string>> = {
  ru: ru.alerts as Record<string, string>,
  az: az.alerts as Record<string, string>,
  en: en.alerts as Record<string, string>,
};

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
}

describe("alerts messages", () => {
  it.each(["az", "en"])("%s has the same keys as ru", (locale) => {
    expect(Object.keys(ALERTS[locale]).sort()).toEqual(Object.keys(ALERTS.ru).sort());
  });

  it.each(["az", "en"])("%s has the same placeholders as ru", (locale) => {
    for (const [key, text] of Object.entries(ALERTS.ru)) {
      expect([key, placeholders(ALERTS[locale][key] ?? "")]).toEqual([key, placeholders(text)]);
    }
  });
});
