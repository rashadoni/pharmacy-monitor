import { describe, expect, it } from "vitest";
import { ApiError, friendlyError, isVerifiedScanPendingError } from "./api";

describe("match edit refusals", () => {
  const refusal = (status: number, code: string, detail: string, params = {}) =>
    new ApiError(status, JSON.stringify({ detail, code, params }));

  it("shows the backend text in Russian and its own text in az and en", () => {
    const busy = refusal(
      409,
      "matching_in_progress",
      "Сейчас идёт сопоставление товаров — правка не записана. Повторите через 2–3 минуты.",
    );
    expect(friendlyError(busy, "ru")).toBe(
      "Сейчас идёт сопоставление товаров — правка не записана. Повторите через 2–3 минуты.",
    );
    expect(friendlyError(busy, "az")).toContain("uyğunlaşdırılması gedir");
    expect(friendlyError(busy, "en")).toContain("matching is running");
  });

  it("names the site and the other comparison from params", () => {
    const dead = refusal(409, "dead_link_member", "…", { site: "pharmonline" });
    expect(friendlyError(dead, "az")).toContain("pharmonline məhsulunun səhifəsi");
    const taken = refusal(409, "product_in_other_match", "…", { match_id: 41 });
    expect(friendlyError(taken, "en")).toContain("(#41)");
  });

  it("keeps the specific text for 404 instead of the generic not found", () => {
    const gone = refusal(404, "match_gone", "Этого сравнения уже нет. Обновите страницу.");
    expect(friendlyError(gone, "ru")).toBe("Этого сравнения уже нет. Обновите страницу.");
    expect(friendlyError(gone, "az")).toBe("Bu müqayisə artıq yoxdur. Səhifəni yeniləyin.");
  });

  it("falls back to the status text for a code it does not know", () => {
    const unknown = refusal(409, "brand_new_code", "Что-то новое");
    expect(friendlyError(unknown, "ru")).toBe("Что-то новое");
    expect(friendlyError(unknown, "az")).toBe(
      "Məlumat ziddiyyəti var. Səhifəni yeniləyib dəyişiklikləri yoxlayın.",
    );
  });
});

describe("friendlyError locale handling", () => {
  it("localizes common HTTP errors", () => {
    expect(friendlyError(new ApiError(401, "unauthorized"), "az")).toBe(
      "Yenidən daxil olun",
    );
    expect(friendlyError(new ApiError(404, "missing"), "en")).toBe("Not found");
  });

  it("does not leak Russian network copy into Azerbaijani UI", () => {
    expect(friendlyError(new ApiError(0, "Сеть недоступна"), "az")).toBe(
      "Şəbəkə əlçatan deyil. Bağlantını yoxlayın.",
    );
  });

  it("localizes Pydantic validation messages", () => {
    const detail = JSON.stringify({
      detail: [{ loc: ["body", "email"], msg: "Field required" }],
    });
    expect(friendlyError(new ApiError(422, detail), "az")).toBe(
      "email: məcburi sahə",
    );
  });

  it("does not expose Russian conflict details in non-Russian locales", () => {
    const detail = JSON.stringify({ detail: "Категория уже маппирована" });
    expect(friendlyError(new ApiError(409, detail), "az")).toBe(
      "Məlumat ziddiyyəti var. Səhifəni yeniləyib dəyişiklikləri yoxlayın.",
    );
    expect(friendlyError(new ApiError(409, detail), "en")).toBe(
      "Data conflict. Refresh the page and review your changes.",
    );
  });
});

describe("isVerifiedScanPendingError", () => {
  it("distinguishes the expected ROI trust-gate response from real server errors", () => {
    expect(
      isVerifiedScanPendingError(
        new ApiError(
          503,
          JSON.stringify({
            detail: "Verified full-catalog recommendations are not available yet",
          }),
        ),
      ),
    ).toBe(true);
    expect(isVerifiedScanPendingError(new ApiError(503, "service unavailable"))).toBe(false);
    expect(isVerifiedScanPendingError(new ApiError(500, "database unavailable"))).toBe(false);
    expect(isVerifiedScanPendingError(new Error("network failure"))).toBe(false);
  });
});
