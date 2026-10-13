import { afterEach, describe, expect, it, vi } from "vitest";

import { forgetLinkToken, LINK_TOKEN_STORAGE_KEY, storedLinkToken } from "./link-token";

/** Хранилище вкладки: тесты идут без браузера, `window` подставляется. */
function tabStorage(initial: Record<string, string> = {}) {
  const held = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => held.get(key) ?? null,
    removeItem: (key: string) => void held.delete(key),
    held,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("storedLinkToken", () => {
  it("returns the token the link page left for the form", () => {
    vi.stubGlobal("window", {
      sessionStorage: tabStorage({ [LINK_TOKEN_STORAGE_KEY]: " abc-DEF_123 " }),
    });

    expect(storedLinkToken()).toBe("abc-DEF_123");
  });

  it("returns null when nothing was left, or only blanks", () => {
    vi.stubGlobal("window", { sessionStorage: tabStorage() });
    expect(storedLinkToken()).toBeNull();

    vi.stubGlobal("window", {
      sessionStorage: tabStorage({ [LINK_TOKEN_STORAGE_KEY]: "  " }),
    });
    expect(storedLinkToken()).toBeNull();
  });

  it("returns null when the browser refuses tab storage", () => {
    vi.stubGlobal("window", {
      get sessionStorage(): never {
        throw new Error("denied");
      },
    });

    expect(storedLinkToken()).toBeNull();
    expect(() => forgetLinkToken()).not.toThrow();
  });
});

describe("forgetLinkToken", () => {
  it("removes the token and nothing else", () => {
    const storage = tabStorage({ [LINK_TOKEN_STORAGE_KEY]: "abc", other: "kept" });
    vi.stubGlobal("window", { sessionStorage: storage });

    forgetLinkToken();

    expect(storedLinkToken()).toBeNull();
    expect(storage.held.get("other")).toBe("kept");
  });
});
