import { describe, expect, it } from "vitest";
import { categoryDisplayLabel } from "./category-label";

describe("categoryDisplayLabel", () => {
  const translated = {
    key: "vitamins",
    label_ru: "Витамины",
    label_az: "Vitaminlər",
  };

  it("uses Azerbaijani copy on Azerbaijani routes", () => {
    expect(categoryDisplayLabel(translated, "az")).toBe("Vitaminlər");
  });

  it("uses Russian copy on Russian and English routes", () => {
    expect(categoryDisplayLabel(translated, "ru")).toBe("Витамины");
    expect(categoryDisplayLabel(translated, "en")).toBe("Витамины");
  });

  it("falls back predictably when Azerbaijani copy is missing", () => {
    expect(categoryDisplayLabel({ ...translated, label_az: " " }, "az")).toBe("vitamins");
  });

  it("falls back to the supplied slug when both labels are missing", () => {
    expect(categoryDisplayLabel({}, "az", "ushaq-qidasi")).toBe("ushaq-qidasi");
  });
});
