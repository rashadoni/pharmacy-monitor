import { describe, expect, it } from "vitest";
import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import type { AlertEvent } from "./api";
import { localizedAlertCopy, type AlertCopyPresenter } from "./alert-copy";

function tFromMessages(locale: { alerts: unknown } = az): AlertCopyPresenter {
  const messages = locale.alerts as Record<string, string>;
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
    destination_url: null,
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

  it("does not leak stored Russian detail for new-product alerts", () => {
    const copy = localizedAlertCopy(
      event({
        rule_type: "new_product",
        title: "Новый товар на aloe: Test product",
        detail: "Цена: 12 ₼. Появился впервые на aloe.",
        payload: { site: "aloe" },
        site: "aloe",
      }),
      tFromMessages(),
    );

    expect(copy.title).toBe("aloe saytında yeni məhsul: Test product");
    expect(copy.detail).toBe("Bu məhsul ilk dəfə aloe saytında göründü.");
    expect(copy.detail).not.toContain("Появился");
  });

  it("does not duplicate the stored percent for price-raise alerts", () => {
    const copy = localizedAlertCopy(
      event({
        rule_type: "price_raise_opportunity",
        title: "Можно поднять: Test product (+12.3%)",
        detail: "Клиент: 10.00 ₼. Медиана конкурентов: 11.23 ₼.",
        payload: {
          client_price: 10,
          median_competitor: 11.23,
          gap_pct: 12.3,
        },
      }),
      tFromMessages(),
    );

    expect(copy.title).toBe("Qiyməti qaldırmaq olar: Test product (+12.3%)");
    expect(copy.title).not.toContain("(+12.3%) (+12.3%)");
    expect(copy.detail).toBe("Müştəri: 10.00 ₼. Rəqiblərin medianı: 11.23 ₼.");
  });

  const cheapestRaise = event({
    rule_type: "price_raise_opportunity",
    title: "Можно поднять: Test product (+25.0%)",
    detail: "Клиент: 8.00 ₼. Самый дешёвый конкурент — aloe: 10.00 ₼.",
    payload: {
      client_price: 8,
      cheapest_competitor: 10,
      cheapest_competitor_site: "aloe",
      gap_pct: 25,
    },
  });

  it("names the cheapest competitor for price-raise alerts", () => {
    const copy = localizedAlertCopy(cheapestRaise, tFromMessages());

    expect(copy.title).toBe("Qiyməti qaldırmaq olar: Test product (+25.0%)");
    expect(copy.detail).toBe("Müştəri: 8.00 ₼. Ən ucuz rəqib — aloe: 10.00 ₼.");
    expect(copy.detail).not.toContain("median");
  });

  it("has the cheapest-competitor copy in every locale", () => {
    expect(localizedAlertCopy(cheapestRaise, tFromMessages(ru)).detail).toBe(
      "Клиент: 8.00 ₼. Самый дешёвый конкурент — aloe: 10.00 ₼.",
    );
    expect(localizedAlertCopy(cheapestRaise, tFromMessages(en)).detail).toBe(
      "Client: 8.00 ₼. Cheapest competitor — aloe: 10.00 ₼.",
    );
  });

  it("keeps the median copy for price-raise alerts stored with a median", () => {
    for (const [locale, expected] of [
      [ru, "Клиент: 10.00 ₼. Медиана конкурентов: 11.23 ₼."],
      [en, "Client: 10.00 ₼. Competitor median: 11.23 ₼."],
    ] as const) {
      const copy = localizedAlertCopy(
        event({
          rule_type: "price_raise_opportunity",
          title: "Можно поднять: Test product (+12.3%)",
          detail: "Клиент: 10.00 ₼. Медиана конкурентов: 11.23 ₼.",
          payload: { client_price: 10, median_competitor: 11.23, gap_pct: 12.3 },
        }),
        tFromMessages(locale),
      );
      expect(copy.detail).toBe(expected);
    }
  });
});
