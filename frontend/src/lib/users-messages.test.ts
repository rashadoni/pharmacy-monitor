import { describe, expect, it } from "vitest";
import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";

// Страница «Пользователи» на трёх языках: ключ, забытый в одном, next-intl
// покажет как «users.set_password», а подстановку с другим именем — как «{code}».
const USERS: Record<string, Record<string, string>> = {
  ru: ru.users as Record<string, string>,
  az: az.users as Record<string, string>,
  en: en.users as Record<string, string>,
};

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
}

describe("users messages", () => {
  it.each(["az", "en"])("%s has the same keys as ru", (locale) => {
    expect(Object.keys(USERS[locale]).sort()).toEqual(Object.keys(USERS.ru).sort());
  });

  it.each(["az", "en"])("%s has the same placeholders as ru", (locale) => {
    for (const [key, text] of Object.entries(USERS.ru)) {
      expect([key, placeholders(USERS[locale][key] ?? "")]).toEqual([key, placeholders(text)]);
    }
  });

  it.each(["ru", "az", "en"])("%s has the admin password form", (locale) => {
    for (const key of [
      "set_password",
      "set_password_label",
      "set_password_note",
      "set_password_save",
      "set_password_done",
      "set_password_self",
    ]) {
      expect(USERS[locale][key]).toBeTruthy();
    }
  });
});
