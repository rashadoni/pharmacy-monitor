import { describe, expect, it } from "vitest";
import az from "../../messages/az.json";
import type { AlertEvent } from "./api";
import { localizedAlertCopy, type AlertCopyPresenter } from "./alert-copy";

function tFromMessages(): AlertCopyPresenter {
  const messages = az.alerts as Record<string, string>;
  return (key, values = {}) => {
    const template = messages[key] ?? key;
    return Object.entries(values).reduce(
      (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
      template,
    );
  };
}

function event(partial: Partial<AlertEvent>): AlertEvent {
  return {
    id: 1,
    rule_type: null,
    severity: "warning",
    title: "Raw title",
    detail: null,
    payload: null,
    site: null,
    created_at: "2026-07-12T00:00:00Z",
    is_read: false,
    read_at: null,
    snoozed_until: null,
    ...partial,
  };
}

describe("localizedAlertCopy", () => {
  it("renders Azerbaijani price-drop copy from payload instead of stored Russian title", () => {
    const copy = localizedAlertCopy(
      event({
        rule_type: "price_drop_pct",
        severity: "critical",
        title:
          "Цена упала на 30.0%: Biobalance dəri çatlarına qarşı (Krem) 60 ml",
        detail: "pharmonline: 22.00 → 15.40 ₼ (−30.0%).",
        payload: {
          site: "pharmonline",
          prev_price: 22,
          curr_price: 15.4,
          drop_pct: 30,
        },
        site: "pharmonline",
      }),
      tFromMessages(),
    );

    expect(copy.title).toBe(
      "Qiymət 30.0% düşüb: Biobalance dəri çatlarına qarşı (Krem) 60 ml",
    );
    expect(copy.title).not.toContain("Цена упала");
    expect(copy.detail).toBe("pharmonline: 22.00 → 15.40 ₼ (-30.0%).");
  });

  it("falls back to stored copy for unknown rule types", () => {
    const copy = localizedAlertCopy(
      event({
        rule_type: "custom_rule",
        title: "Custom title",
        detail: "Custom detail",
      }),
      tFromMessages(),
    );

    expect(copy).toEqual({ title: "Custom title", detail: "Custom detail" });
  });
});
