import { afterEach, describe, expect, it, vi } from "vitest";
import { formatPrice, formatRelative, formatTime } from "./utils";

describe("localized date formatting", () => {
  afterEach(() => vi.useRealTimers());

  it("formats Azerbaijani relative time without Russian copy", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-10T12:00:00Z"));
    const value = formatRelative("2026-07-10T11:55:00Z", "az");
    expect(value).not.toMatch(/[А-Яа-яЁё]/);
  });

  it("uses the requested locale for absolute timestamps", () => {
    const value = formatTime("2026-07-10T11:55:00Z", "en");
    expect(value).not.toMatch(/[А-Яа-яЁё]/);
  });

  it("formats AZN with the active route locale", () => {
    expect(formatPrice(1234.5, "az")).toContain("₼");
    expect(formatPrice(1234.5, "en")).toContain("1,234.50");
    expect(formatPrice(1234.5, "ru")).toContain("1 234,50");
  });
});
