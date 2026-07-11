import { describe, expect, it } from "vitest";
import { ApiError, friendlyError, isVerifiedScanPendingError } from "./api";

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
