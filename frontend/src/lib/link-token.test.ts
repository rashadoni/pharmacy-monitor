import { describe, expect, it } from "vitest";
import { linkTokenFromHash } from "./link-token";

describe("token of a login link", () => {
  it("is read from the part of the address after #", () => {
    expect(linkTokenFromHash("#token=abc_DEF-123")).toBe("abc_DEF-123");
    expect(linkTokenFromHash("token=abc")).toBe("abc");
    expect(linkTokenFromHash("#a=1&token=xyz&b=2")).toBe("xyz");
  });

  it("is absent when the link carries none", () => {
    for (const hash of ["", "#", "#token=", "#token=%20%20", "#other=1", "#Token=abc"]) {
      expect(linkTokenFromHash(hash)).toBeNull();
    }
  });
});
