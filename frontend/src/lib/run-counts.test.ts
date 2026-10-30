import { describe, expect, it } from "vitest";

import { runProductCount, siteProductCount } from "./run-counts";

describe("product counts of a run", () => {
  it("shows distinct products of a site and keeps listing rows as the second number", () => {
    const run = {
      products_scraped: 96347,
      products_per_site: { aptekonline: 96347 },
      products_distinct_per_site: { aptekonline: 27097 },
    };

    expect(siteProductCount(run, "aptekonline")).toEqual({
      value: 27097,
      distinct: true,
      listingRows: 96347,
    });
    expect(runProductCount(run)).toEqual({
      value: 27097,
      distinct: true,
      listingRows: 96347,
    });
  });

  it("drops the second number when every listing row is a different product", () => {
    const run = {
      products_scraped: 9520,
      products_per_site: { pharmonline: 9520 },
      products_distinct_per_site: { pharmonline: 9520 },
    };

    expect(siteProductCount(run, "pharmonline")).toEqual({
      value: 9520,
      distinct: true,
      listingRows: null,
    });
  });

  it("labels listing rows of a run recorded before distinct products were counted", () => {
    const run = {
      products_scraped: 96347,
      products_per_site: { aptekonline: 96347 },
      products_distinct_per_site: {},
    };

    expect(siteProductCount(run, "aptekonline")).toEqual({
      value: 96347,
      distinct: false,
      listingRows: null,
    });
    expect(runProductCount(run)).toEqual({
      value: 96347,
      distinct: false,
      listingRows: null,
    });
  });

  it("falls back to the run total when a legacy run has no per-site counts", () => {
    expect(siteProductCount({ products_scraped: 120 }, "aloe")).toEqual({
      value: 120,
      distinct: false,
      listingRows: null,
    });
    expect(siteProductCount(null, "aloe")).toBeNull();
    expect(runProductCount({ products_scraped: null })).toBeNull();
  });

  it("adds distinct products of several sites only when every collected site has them", () => {
    const both = {
      products_scraped: 104777,
      products_per_site: { aptekonline: 96347, aloe: 8430, pharmonline: 0 },
      products_distinct_per_site: { aptekonline: 27097, aloe: 6100 },
    };
    expect(runProductCount(both)).toEqual({
      value: 33197,
      distinct: true,
      listingRows: 104777,
    });

    const oneMissing = { ...both, products_distinct_per_site: { aptekonline: 27097 } };
    expect(runProductCount(oneMissing)).toEqual({
      value: 104777,
      distinct: false,
      listingRows: null,
    });
  });
});
