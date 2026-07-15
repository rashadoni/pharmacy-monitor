import { describe, expect, it } from "vitest";
import { choiceParam, integerParam, queryWithPatch } from "./filter-query";

describe("dashboard filter query helpers", () => {
  it("updates selected values without dropping unrelated working context", () => {
    expect(
      queryWithPatch("view=read&severity=warning&type=price_drop", {
        severity: "critical",
        hours: 24,
      }),
    ).toBe("view=read&severity=critical&type=price_drop&hours=24");
  });

  it("removes defaults and false flags from shareable URLs", () => {
    expect(
      queryWithPatch("q=aspirin&active=1&page=3", {
        q: "",
        active: false,
        page: null,
      }),
    ).toBe("");
  });

  it("fails closed to declared enum and integer defaults", () => {
    const params = new URLSearchParams(
      "view=admin&hours=-1&overlap=999&page=9007199254740992",
    );
    expect(choiceParam(params, "view", ["inbox", "read"] as const, "inbox")).toBe(
      "inbox",
    );
    expect(integerParam(params, "hours", 168, { allowed: [0, 24, 168] })).toBe(168);
    expect(integerParam(params, "overlap", 5, { min: 2, max: 50 })).toBe(5);
    expect(integerParam(params, "page", 1, { min: 1, max: 10_000 })).toBe(1);
  });
});
