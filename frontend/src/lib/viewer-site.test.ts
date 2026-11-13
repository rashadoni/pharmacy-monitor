import { afterEach, describe, expect, it, vi } from "vitest";

// Профиль читается при импорте модуля, поэтому каждый случай грузит модули
// заново под своей переменной окружения.
async function load(profile: string | undefined) {
  vi.resetModules();
  if (profile === undefined) vi.stubEnv("NEXT_PUBLIC_PM_PROFILE", "");
  else vi.stubEnv("NEXT_PUBLIC_PM_PROFILE", profile);
  return import("./viewer-site");
}

function store(value: string | null) {
  return { getItem: () => value };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("«свой магазин» у экземпляра электроники", () => {
  it("выбранный магазин становится своим и идёт первым", async () => {
    const m = await load("electronics");
    expect(m.VIEWER_SITE_SELECTABLE).toBe(true);
    expect(m.readViewerSite(store("irshad"))).toBe("irshad");
    expect(m.readViewerSite(store("bakuelectronics"))).toBe("bakuelectronics");
    expect(m.viewerSites("irshad")).toEqual(["irshad", "kontakt", "bakuelectronics"]);
    expect(m.viewerSites("bakuelectronics")).toEqual(["bakuelectronics", "kontakt", "irshad"]);
  });

  it("без выбора, с чужим значением или без хранилища — клиент профиля", async () => {
    const m = await load("electronics");
    expect(m.readViewerSite(store(null))).toBe("kontakt");
    expect(m.readViewerSite(store("pharmonline"))).toBe("kontakt");
    expect(m.readViewerSite(null)).toBe("kontakt");
    const broken = {
      getItem: () => {
        throw new Error("SecurityError");
      },
    };
    expect(m.readViewerSite(broken)).toBe("kontakt");
  });
});

describe("аптечный экземпляр", () => {
  it("выбор не действует: свой всегда pharmonline", async () => {
    const m = await load(undefined);
    expect(m.VIEWER_SITE_SELECTABLE).toBe(false);
    expect(m.readViewerSite(store("aloe"))).toBe("pharmonline");
    expect(m.viewerSites("pharmonline")).toEqual(["pharmonline", "aptekonline", "aloe"]);
  });
});
