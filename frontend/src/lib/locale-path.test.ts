import { describe, expect, it } from "vitest";
import { pathWithSearch } from "./locale-path";

describe("pathWithSearch", () => {
  it("preserves active filters while switching locale", () => {
    expect(pathWithSearch("/comparison", "search=aspirin&min_sites=3")).toBe(
      "/comparison?search=aspirin&min_sites=3",
    );
  });

  it("does not append an empty query", () => {
    expect(pathWithSearch("/overview", "")).toBe("/overview");
  });
});
