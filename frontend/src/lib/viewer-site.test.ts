import { afterEach, describe, expect, it, vi } from "vitest";

// Профиль читается при импорте модуля, поэтому каждый случай грузит модули
// заново под своей переменной окружения. Модуль куки — без React: его
// импортирует сервер (i18n/request.ts).
async function load(profile: string | undefined) {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_PM_PROFILE", profile ?? "");
  return import("./viewer-site-cookie");
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("«свой магазин» у экземпляра электроники", () => {
  it("выбранный магазин из куки становится своим и идёт первым", async () => {
    const m = await load("electronics");
    expect(m.VIEWER_SITE_SELECTABLE).toBe(true);
    expect(m.readViewerSite("a=1; pm_viewer_site=irshad; b=2")).toBe("irshad");
    expect(m.readViewerSite("pm_viewer_site=bakuelectronics")).toBe("bakuelectronics");
    expect(m.viewerSiteFromCookie("irshad")).toBe("irshad");
    expect(m.viewerSites("irshad")).toEqual(["irshad", "kontakt", "bakuelectronics"]);
    expect(m.viewerSites("bakuelectronics")).toEqual(["bakuelectronics", "kontakt", "irshad"]);
  });

  it("без куки, с чужим значением или без документа — клиент профиля", async () => {
    const m = await load("electronics");
    expect(m.readViewerSite("")).toBe("kontakt");
    expect(m.readViewerSite("pm_viewer_site=pharmonline")).toBe("kontakt");
    expect(m.readViewerSite("other_pm_viewer_site=irshad")).toBe("kontakt");
    expect(m.readViewerSite(null)).toBe("kontakt");
    expect(m.viewerSiteFromCookie(undefined)).toBe("kontakt");
  });
});

describe("аптечный экземпляр", () => {
  it("выбор не действует: свой всегда pharmonline", async () => {
    const m = await load(undefined);
    expect(m.VIEWER_SITE_SELECTABLE).toBe(false);
    expect(m.readViewerSite("pm_viewer_site=aloe")).toBe("pharmonline");
    expect(m.viewerSiteFromCookie("aloe")).toBe("pharmonline");
    expect(m.viewerSites("pharmonline")).toEqual(["pharmonline", "aptekonline", "aloe"]);
  });
});
