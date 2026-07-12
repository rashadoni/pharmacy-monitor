import type { AlertEvent } from "@/lib/api";

export type AlertCopyPresenter = (
  key: string,
  values?: Record<string, string | number>,
) => string;

function numberPayload(
  payload: AlertEvent["payload"],
  key: string,
): number | null {
  const value = payload?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringPayload(
  payload: AlertEvent["payload"],
  key: string,
): string | null {
  const value = payload?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function titleSubject(event: AlertEvent): string {
  const title = event.title || "";
  const colon = title.indexOf(":");
  if (colon >= 0 && colon + 1 < title.length) {
    return title.slice(colon + 1).trim();
  }
  return title;
}

function titleSubjectWithoutTrailingPercent(event: AlertEvent): string {
  return titleSubject(event)
    .replace(/\s+\(\+\d+(?:[.,]\d+)?%\)$/u, "")
    .trim();
}

function formatAlertAmount(value: number | null): string {
  return value === null ? "?" : value.toFixed(2);
}

function siteOrGeneral(event: AlertEvent, t: AlertCopyPresenter): string {
  return (
    stringPayload(event.payload, "site") ??
    event.site ??
    t("site_general_short")
  );
}

export function localizedAlertCopy(
  event: AlertEvent,
  t: AlertCopyPresenter,
): { title: string; detail: string | null } {
  const payload = event.payload;
  const subject = titleSubject(event);
  const site = siteOrGeneral(event, t);

  if (event.rule_type === "price_drop_pct") {
    const dropPct = numberPayload(payload, "drop_pct");
    const prevPrice = numberPayload(payload, "prev_price");
    const currPrice = numberPayload(payload, "curr_price");
    return {
      title: t("event_price_drop_title", {
        pct: dropPct === null ? "?" : dropPct.toFixed(1),
        product: subject,
      }),
      detail: t("event_price_drop_detail", {
        site,
        previous: formatAlertAmount(prevPrice),
        current: formatAlertAmount(currPrice),
        pct: dropPct === null ? "?" : dropPct.toFixed(1),
      }),
    };
  }

  if (event.rule_type === "undercut_threshold") {
    const diffPct = numberPayload(payload, "diff_pct");
    const clientPrice = numberPayload(payload, "client_price");
    const competitorPrice = numberPayload(payload, "competitor_price");
    return {
      title: t("event_undercut_title", {
        site,
        pct: diffPct === null ? "?" : diffPct.toFixed(1),
        product: subject,
      }),
      detail: t("event_undercut_detail", {
        client: formatAlertAmount(clientPrice),
        site,
        competitor: formatAlertAmount(competitorPrice),
        pct: diffPct === null ? "?" : diffPct.toFixed(1),
      }),
    };
  }

  if (event.rule_type === "new_product") {
    return {
      title: t("event_new_product_title", {
        site,
        product: subject,
      }),
      detail: t("event_new_product_detail", { site }),
    };
  }

  if (event.rule_type === "promo_started") {
    const promoTitle = stringPayload(payload, "title") ?? subject;
    return {
      title: t("event_promo_started_title", {
        site,
        promo: promoTitle,
      }),
      detail: promoTitle,
    };
  }

  if (event.rule_type === "price_raise_opportunity") {
    const gapPct = numberPayload(payload, "gap_pct");
    const clientPrice = numberPayload(payload, "client_price");
    const median = numberPayload(payload, "median_competitor");
    return {
      title: t("event_price_raise_title", {
        product: titleSubjectWithoutTrailingPercent(event),
        pct: gapPct === null ? "?" : gapPct.toFixed(1),
      }),
      detail: t("event_price_raise_detail", {
        client: formatAlertAmount(clientPrice),
        median: formatAlertAmount(median),
      }),
    };
  }

  if (event.rule_type === "site_drop_smoke") {
    const current = numberPayload(payload, "current");
    const avg = numberPayload(payload, "avg");
    return {
      title: t("event_site_drop_smoke_title", { site }),
      detail: t("event_site_drop_smoke_detail", {
        site,
        current: current === null ? "?" : current.toFixed(0),
        avg: avg === null ? "?" : avg.toFixed(0),
      }),
    };
  }

  return { title: event.title, detail: event.detail };
}
