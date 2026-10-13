import { describe, expect, it } from "vitest";

import { digestToggleLocked, notifPrefsReadOnly, shouldShowNotifBanner } from "./notif-prefs";

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
