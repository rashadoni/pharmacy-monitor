import { describe, expect, it } from "vitest";

import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import {
  digestButtonFeedback,
  digestToggleLocked,
  notifPrefsReadOnly,
  shouldShowNotifBanner,
} from "./notif-prefs";

const LANGUAGES = { ru, az, en };

describe("notifPrefsReadOnly", () => {
  it("follows the server's word", () => {
    expect(notifPrefsReadOnly({ can_edit: false, has_delivery: true })).toBe(true);
    expect(notifPrefsReadOnly({ can_edit: true, has_delivery: true })).toBe(false);
  });

  it("leaves the block editable for an API that does not say", () => {
    // Такой API правку и не отклоняет: запирать блок было бы неправдой.
    expect(notifPrefsReadOnly(undefined)).toBe(false);
    expect(notifPrefsReadOnly(null)).toBe(false);
    expect(notifPrefsReadOnly({})).toBe(false);
    expect(notifPrefsReadOnly({ has_delivery: false })).toBe(false);
  });
});

describe("shouldShowNotifBanner", () => {
  it("never nags a viewer, whose notifications the administrator sets", () => {
    // Сотрудник клиента с выключенными мгновенными письмами и недельным дайджестом.
    expect(shouldShowNotifBanner({ can_edit: false, has_delivery: true })).toBe(false);
    // И даже когда ему не доходит ничего: исправить это он не может.
    expect(shouldShowNotifBanner({ can_edit: false, has_delivery: false })).toBe(false);
  });

  it("stays hidden while something reaches the administrator", () => {
    expect(shouldShowNotifBanner({ can_edit: true, has_delivery: true })).toBe(false);
  });

  it("shows when nothing reaches someone who can fix it", () => {
    expect(shouldShowNotifBanner({ can_edit: true, has_delivery: false })).toBe(true);
  });

  it("stays hidden until the server has answered both questions", () => {
    expect(shouldShowNotifBanner(undefined)).toBe(false);
    expect(shouldShowNotifBanner(null)).toBe(false);
    expect(shouldShowNotifBanner({})).toBe(false);
    expect(shouldShowNotifBanner({ can_edit: true })).toBe(false);
    expect(shouldShowNotifBanner({ has_delivery: false })).toBe(false);
  });
});

describe("digestToggleLocked", () => {
  it("does not let a digest be switched on while its timer is off", () => {
    expect(digestToggleLocked(false, false)).toBe(true);
  });

  it("always lets a digest be switched off", () => {
    expect(digestToggleLocked(false, true)).toBe(false);
    expect(digestToggleLocked(true, true)).toBe(false);
  });

  it("leaves the toggle free while the timer runs or its state is unknown", () => {
    expect(digestToggleLocked(true, false)).toBe(false);
    expect(digestToggleLocked(undefined, false)).toBe(false);
    expect(digestToggleLocked(undefined, true)).toBe(false);
  });
});

describe("digestButtonFeedback", () => {
  it("says the letter went only when the server sent one", () => {
    expect(digestButtonFeedback(1)).toBe("digest_sent");
    // Нет событий за неделю или недельный дайджест у нажавшего выключен.
    expect(digestButtonFeedback(0)).toBe("digest_none");
  });

  it("has a phrase for the button and for each outcome in every language", () => {
    // Ключ без перевода страница показывает как есть: «quick_actions.digest_none».
    const keys = [
      "digest_title",
      "digest_confirm",
      "digest_to_self",
      digestButtonFeedback(1),
      digestButtonFeedback(0),
    ];
    for (const [language, messages] of Object.entries(LANGUAGES)) {
      const phrases = messages.quick_actions as Record<string, string>;
      for (const key of keys) {
        expect(phrases[key], `${language}: quick_actions.${key}`).toBeTruthy();
      }
      // Фраза об отправке — без числа получателей: получатель один, нажавший.
      expect(phrases.digest_sent).not.toContain("{count}");
    }
  });
});

describe("digest wording", () => {
  it("offers the weekly digest and nothing daily in every language", () => {
    // Ежедневного дайджеста получателя нет с 2026-10-13: ни галочки, ни подписей.
    for (const [language, messages] of Object.entries(LANGUAGES)) {
      const settings = messages.settings as Record<string, string>;
      const users = messages.users as Record<string, string>;
      expect(settings.weekly_digest_schedule, language).toBeTruthy();
      expect(users.weekly_digest, language).toBeTruthy();
      expect(users.digest_schedule_weekly_state, language).toBeTruthy();
      const keys = [
        ...Object.keys(settings),
        ...Object.keys(users),
        ...Object.keys(messages.quick_actions),
      ];
      expect(keys.filter((key) => /daily/i.test(key)), language).toEqual([]);
    }
  });
});
