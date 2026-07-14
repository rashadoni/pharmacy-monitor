import { describe, expect, it } from "vitest";

import { safeAlertDestination } from "./alert-link";

describe("safeAlertDestination", () => {
  it("accepts normal product links", () => {
    expect(safeAlertDestination("https://aloe.az/pedikar-50-ml/")).toBe(
      "https://aloe.az/pedikar-50-ml/",
    );
  });

  it("rejects executable and malformed destinations", () => {
    expect(safeAlertDestination("javascript:alert(1)")).toBeNull();
    expect(safeAlertDestination("data:text/html,test")).toBeNull();
    expect(safeAlertDestination("/relative/path")).toBeNull();
    expect(
      safeAlertDestination("https://user:pass@example.com/product"),
    ).toBeNull();
  });

  it("handles absent destinations", () => {
    expect(safeAlertDestination(null)).toBeNull();
    expect(safeAlertDestination(undefined)).toBeNull();
  });
});
