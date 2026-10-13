import { describe, expect, it } from "vitest";

import { canEditNotifPrefs, shouldShowNotifBanner } from "./notif-prefs";

describe("canEditNotifPrefs", () => {
  it("follows the server's word", () => {
    expect(canEditNotifPrefs({ can_edit: true, has_delivery: true })).toBe(true);
    expect(canEditNotifPrefs({ can_edit: false, has_delivery: true })).toBe(false);
  });

  it("reads a missing answer as read-only", () => {
    expect(canEditNotifPrefs(undefined)).toBe(false);
    expect(canEditNotifPrefs(null)).toBe(false);
    expect(canEditNotifPrefs({})).toBe(false);
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
