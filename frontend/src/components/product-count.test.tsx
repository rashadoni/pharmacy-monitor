// @vitest-environment jsdom

import React from "react";
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import type { ProductCount } from "../lib/run-counts";
import { ProductCountText } from "./product-count";

function shown(
  locale: string,
  messages: Record<string, unknown>,
  count: ProductCount | null,
  inline = false,
) {
  const { container } = render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <ProductCountText count={count} inline={inline} />
    </NextIntlClientProvider>,
  );
  const root = container.firstElementChild as HTMLElement;
  // Разделитель разрядов — неразрывный пробел; сравниваем с обычным.
  return {
    text: (root.textContent ?? "").replace(/\s/g, " "),
    title: root.getAttribute("title"),
  };
}

const aptekonline: ProductCount = { value: 27097, distinct: true, listingRows: 96347 };

describe("ProductCountText", () => {
  it("puts distinct products first and labels listing rows as rows", () => {
    expect(shown("ru", ru, aptekonline).text).toBe("27 097" + "96 347 строк в листинге");
    expect(shown("az", az, aptekonline).text).toBe("27.097" + "siyahıda 96.347 sətir");
    expect(shown("en", en, aptekonline).text).toBe("27,097" + "96,347 listing rows");
    expect(shown("ru", ru, aptekonline).title).toBe(ru.run_counts.hint);
  });

  it("shows one number when every listing row is a different product", () => {
    const pharmonline: ProductCount = { value: 9520, distinct: true, listingRows: null };

    expect(shown("ru", ru, pharmonline)).toEqual({ text: "9 520", title: null });
  });

  it("does not pass listing rows of an old run off as products", () => {
    const legacy: ProductCount = { value: 96347, distinct: false, listingRows: null };

    expect(shown("ru", ru, legacy)).toEqual({
      text: "96 347" + "строк в листинге",
      title: ru.run_counts.hint_legacy,
    });
    expect(shown("az", az, legacy).text).toBe("96.347" + "siyahı sətri");
  });

  it("keeps Russian plural forms of the second number", () => {
    const rows = (listingRows: number) =>
      shown("ru", ru, { value: 1, distinct: true, listingRows }).text.slice(1);

    expect(rows(21)).toBe("21 строка в листинге");
    expect(rows(22)).toBe("22 строки в листинге");
    expect(rows(25)).toBe("25 строк в листинге");
  });

  it("writes the second number on the same line when asked", () => {
    expect(shown("ru", ru, aptekonline, true).text).toBe("27 097" + "· 96 347 строк в листинге");
  });

  it("renders a dash when the run has no count at all", () => {
    expect(shown("ru", ru, null).text).toBe("—");
  });
});
