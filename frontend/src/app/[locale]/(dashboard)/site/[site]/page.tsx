"use client";

import { useQuery } from "@tanstack/react-query";
import { notFound, useParams, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { Building2, ExternalLink, Leaf, Pill, Search } from "lucide-react";
import {
  api,
  friendlyError,
  isVerifiedScanPendingError,
  type SiteProduct,
} from "@/lib/api";
import { ActionRow } from "@/components/action-row";
import { MetricStrip } from "@/components/metric-strip";
import { Sparkline } from "@/components/sparkline";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatRelative, formatTime } from "@/lib/utils";
import { QueryErrorState } from "@/components/query-error-state";
import { useRouter } from "@/i18n/navigation";
import { integerParam, queryWithPatch } from "@/lib/filter-query";

const PAGE_LIMIT = 50;

/**
 * P1.5 (PO Audit 2026-05-17): human-readable category label.
 *
 * Backend возвращает {name: slug, label: localized label || slug}.
 * - `label = "Daha çox"` (Azerbaijani "Show more") — UI-artefact из aloe-scrape
 *   seed-данных, бесполезный для пользователя
 * - `label = slug` (отсутствует label_ru) — slug нечитаемый: `ushaqlar-uchun-vasiteler`
 *
 * Эвристика:
 * 1. Если label один из generic UI-маркеров — fallback на красивый slug
 * 2. Иначе если label !== slug — показать label
 * 3. Иначе beautify slug: dashes→spaces, capitalize first
 */
const _GENERIC_LABELS = new Set([
  "daha çox", "daha cox", "show more", "view all", "все", "more",
]);

function prettyCategoryLabel(c: { name: string; label?: string }): string {
  const slug = c.name;
  const label = c.label?.trim();

  // Beautify slug as fallback
  const beautify = (s: string) =>
    s
      .replace(/[-_]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^./, (ch) => ch.toUpperCase());

  if (!label || label === slug) return beautify(slug);
  if (_GENERIC_LABELS.has(label.toLowerCase())) return beautify(slug);
  return label;
}

const VALID_SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type SiteName = (typeof VALID_SITES)[number];

const SITE_META: Record<SiteName, { icon: typeof Leaf; iconColor: string; taglineKey: string }> = {
  pharmonline: {
    icon: Building2,
    iconColor: "text-primary",
    taglineKey: "tagline_pharmonline",
  },
  aptekonline: {
    icon: Pill,
    iconColor: "text-warning",
    taglineKey: "tagline_aptekonline",
  },
  aloe: {
    icon: Leaf,
    iconColor: "text-success",
    taglineKey: "tagline_aloe",
  },
};

function competitorsOf(site: SiteName): SiteName[] {
  return VALID_SITES.filter((s) => s !== site);
}

export default function SitePage() {
  const t = useTranslations("site");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const params = useParams();
  const siteParam = (params.site as string) ?? "";

  if (!VALID_SITES.includes(siteParam as SiteName)) {
    notFound();
  }
  const site = siteParam as SiteName;
  const meta = SITE_META[site];
  const Icon = meta.icon;
  const competitors = competitorsOf(site);

  const summaryQ = useQuery({
    queryKey: ["site", site, "summary"],
    queryFn: () => api.siteProductsSummary(site),
  });
  const facetsQ = useQuery({
    queryKey: ["site", site, "facets", locale],
    queryFn: () => api.siteProductsFacets(site, locale),
  });
  const brandsQ = useQuery({
    queryKey: ["site", site, "brands"],
    queryFn: () => api.brandShare({ top_n: 50, site }),
  });
  const recommendationsQ = useQuery({
    queryKey: ["site", site, "roi-recommendations", locale],
    queryFn: () => api.roiRecommendations(site, locale),
  });
  const roiWaitingForVerifiedScan = isVerifiedScanPendingError(recommendationsQ.error);
  const pageDataError = summaryQ.error ?? facetsQ.error ?? brandsQ.error;

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Icon className={`h-6 w-6 ${meta.iconColor}`} /> {site}.az
          </h1>
          <p className="text-sm text-muted-foreground">{t(meta.taglineKey as any)}</p>
        </div>
        <div className="text-xs text-muted-foreground">
          {summaryQ.data?.last_run_at ? (
            <>
              {t("last_run")}{" "}
              <span className="font-mono">
                {formatTime(summaryQ.data.last_run_at, locale)}
              </span>
              {summaryQ.data.last_run_id != null && (
                <> — run #{summaryQ.data.last_run_id}</>
              )}
            </>
          ) : (
            t("no_run_data")
          )}
        </div>
      </header>

      {pageDataError && (
        <QueryErrorState
          message={friendlyError(pageDataError, locale)}
          retryLabel={tCommon("retry")}
          onRetry={() => {
            if (summaryQ.error) summaryQ.refetch();
            if (facetsQ.error) facetsQ.refetch();
            if (brandsQ.error) brandsQ.refetch();
          }}
        />
      )}

      <section aria-label={t("catalog_title", { site })}>
        <MetricStrip
          items={[
            {
              label: t("kpi_total_products"),
              value: summaryQ.data?.total_products ?? "—",
              loading: summaryQ.isLoading,
            },
            {
              label: t("kpi_brands"),
              value: summaryQ.data?.total_brands ?? "—",
              loading: summaryQ.isLoading,
            },
            {
              label: t("kpi_exclusive_brands"),
              value: summaryQ.data?.exclusive_brands ?? "—",
              loading: summaryQ.isLoading,
              hint: t("kpi_exclusive_hint", { competitors: competitors.join("/") }),
            },
            {
              label: t("kpi_on_sale"),
              value: summaryQ.data
                ? `${summaryQ.data.on_sale_count} (${summaryQ.data.on_sale_pct.toFixed(1)}%)`
                : "—",
              loading: summaryQ.isLoading,
            },
          ]}
        />
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">{t("roi_title")}</h2>
        <p className="text-xs text-muted-foreground mb-3">
          {t("roi_subtitle", { site, competitors: competitors.join("/") })}
        </p>
        {recommendationsQ.data?.provenance.run_id != null && (
          <p className="mb-3 text-xs text-muted-foreground">
            {t("roi_provenance", {
              run: recommendationsQ.data.provenance.run_id,
              completed: formatRelative(recommendationsQ.data.provenance.run_finished_at, locale),
            })}
          </p>
        )}
        {recommendationsQ.isLoading && (
          <div className="rounded-md border border-border bg-card p-3 text-sm text-muted-foreground flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full bg-primary animate-pulse" />
            {t("roi_loading")}
          </div>
        )}
        {recommendationsQ.error && (
          <div
            className={`rounded-md border p-3 text-sm flex items-center justify-between gap-3 ${
              roiWaitingForVerifiedScan
                ? "border-warning/40 bg-warning/5 text-warning"
                : "border-destructive/30 bg-destructive/10 text-destructive"
            }`}
          >
            <div>
              {roiWaitingForVerifiedScan
                ? t("roi_waiting_verified")
                : friendlyError(recommendationsQ.error, locale)}
            </div>
            {!roiWaitingForVerifiedScan && (
              <button
                type="button"
                onClick={() => recommendationsQ.refetch()}
                className="min-h-11 rounded border border-destructive/50 px-3 py-1 text-xs hover:bg-destructive/20 md:min-h-9"
              >
                {t("roi_retry")}
              </button>
            )}
          </div>
        )}
        {recommendationsQ.data && recommendationsQ.data.items.length === 0 && (
          <div className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
            {t("roi_empty")}
          </div>
        )}
        <div className="space-y-2">
          {recommendationsQ.data?.items.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <BrandsPanel
          brands={brandsQ.data ?? []}
          loading={brandsQ.isLoading}
          site={site}
        />
        <CategoriesPanel
          categories={facetsQ.data?.categories ?? []}
          loading={facetsQ.isLoading}
          site={site}
        />
      </section>

      <ProductsSection site={site} facets={facetsQ.data} />
    </div>
  );
}

function BrandsPanel({
  brands,
  loading,
  site,
}: {
  brands: { brand: string; counts: Record<string, number>; exclusive_to: string | null }[];
  loading: boolean;
  site: SiteName;
}) {
  const t = useTranslations("site");
  const tCommon = useTranslations("common");
  const tOverview = useTranslations("overview");
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">{t("brands_title", { site })}</h2>
        <p className="text-xs text-muted-foreground">
          {t("brands_subtitle", { site })}
        </p>
      </div>
      {loading && (
        <div className="p-4 text-sm text-muted-foreground">{tCommon("loading")}</div>
      )}
      {!loading && brands.length === 0 && (
        <div className="p-4 text-sm text-muted-foreground">{tOverview("no_data")}</div>
      )}
      {/*
        P0.4 (PO Audit 2026-05-17): backend brand_share сортирует по `total`
        (сумма SKU по всем сайтам). Для site-страницы хотим видеть топ брендов
        ЭТОГО сайта — сортируем по counts[site] desc.
        Например на /site/aloe Solgar (74 aloe / 0 elsewhere) важнее чем
        La Roche-Posay (1 aloe / много прочих).
      */}
      <ul className="divide-y divide-border max-h-96 overflow-y-auto">
        {[...brands]
          .sort((a, b) => (b.counts[site] ?? 0) - (a.counts[site] ?? 0))
          .map((b) => (
          <li
            key={b.brand}
            className="flex items-center justify-between px-4 py-2 text-sm"
          >
            <span className="font-medium truncate" title={b.brand}>
              {b.brand}
            </span>
            <span className="flex items-center gap-2 shrink-0">
              {b.exclusive_to === site && (
                <span className="rounded bg-success/10 text-success text-[10px] px-1.5 py-0.5 font-semibold uppercase">
                  exclusive
                </span>
              )}
              <span className="font-mono tabular-nums text-muted-foreground">
                {b.counts[site] ?? 0}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CategoriesPanel({
  categories,
  loading,
  site,
}: {
  categories: { name: string; label?: string; count: number }[];
  loading: boolean;
  site: SiteName;
}) {
  const t = useTranslations("site");
  const tCommon = useTranslations("common");
  const tOverview = useTranslations("overview");
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">{t("categories_title", { site })}</h2>
        <p className="text-xs text-muted-foreground">
          {t("categories_subtitle")}
        </p>
      </div>
      {loading && (
        <div className="p-4 text-sm text-muted-foreground">{tCommon("loading")}</div>
      )}
      {!loading && categories.length === 0 && (
        <div className="p-4 text-sm text-muted-foreground">{tOverview("no_data")}</div>
      )}
      <ul className="divide-y divide-border max-h-96 overflow-y-auto">
        {categories.map((c) => {
          const display = prettyCategoryLabel(c);
          return (
            <li
              key={c.name}
              className="flex items-center justify-between px-4 py-2 text-sm gap-2"
              title={c.name}
            >
              <span className="min-w-0 flex-1 truncate font-medium">
                {display}
              </span>
              <span className="font-mono tabular-nums shrink-0">{c.count}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ProductsSection({
  site,
  facets,
}: {
  site: SiteName;
  facets:
    | {
        categories: { name: string; label?: string; count: number }[];
        brands: { name: string; count: number }[];
      }
    | undefined;
}) {
  const t = useTranslations("site");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryRef = useRef(searchParams.toString());
  useEffect(() => {
    queryRef.current = searchParams.toString();
  }, [searchParams]);
  const parsedParams = new URLSearchParams(searchParams.toString());
  const urlSearch = searchParams.get("q") ?? "";
  const [search, setSearch] = useState(urlSearch);
  useEffect(() => setSearch(urlSearch), [urlSearch]);
  const debouncedSearch = useDebounce(search, 300);
  const category = searchParams.get("category") ?? "";
  const brand = searchParams.get("brand") ?? "";
  const onSaleOnly = searchParams.get("sale") === "1";
  const page = integerParam(parsedParams, "page", 1, { min: 1, max: 10_000 });
  const offset = (page - 1) * PAGE_LIMIT;

  function updateFilters(
    patch: Record<string, string | number | boolean | null>,
    resetPage = true,
    history: "push" | "replace" = "push",
  ) {
    const query = queryWithPatch(queryRef.current, {
      ...patch,
      ...(resetPage ? { page: null } : {}),
    });
    queryRef.current = query;
    const href = query ? `/site/${site}?${query}` : `/site/${site}`;
    router[history](href, { scroll: false });
  }

  const productsQ = useQuery({
    queryKey: [
      "site",
      site,
      "products",
      debouncedSearch,
      category,
      brand,
      onSaleOnly,
      offset,
    ],
    queryFn: () =>
      api.siteProducts({
        site,
        search: debouncedSearch || undefined,
        category: category || undefined,
        brand: brand || undefined,
        on_sale: onSaleOnly ? true : undefined,
        limit: PAGE_LIMIT,
        offset,
      }),
  });

  const total = productsQ.data?.total ?? 0;
  const items = productsQ.data?.items ?? [];
  const totalPages = Math.max(1, Math.ceil(total / PAGE_LIMIT));
  const isCanonicalizingPage = productsQ.isSuccess && page > totalPages;

  useEffect(() => {
    if (!isCanonicalizingPage) return;
    const query = queryWithPatch(queryRef.current, {
      page: totalPages === 1 ? null : totalPages,
    });
    queryRef.current = query;
    const href = query ? `/site/${site}?${query}` : `/site/${site}`;
    router.replace(href, { scroll: false });
  }, [isCanonicalizingPage, router, site, totalPages]);

  return (
    <section>
      <h2 className="text-lg font-semibold mb-3">{t("catalog_title", { site })}</h2>

      <div className="flex flex-col md:flex-row gap-2 mb-3">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="search"
            placeholder={t("search_placeholder")}
            value={search}
            aria-label={t("search_label")}
            onChange={(e) => {
              setSearch(e.target.value);
              updateFilters({ q: e.target.value || null }, true, "replace");
            }}
            className="min-h-11 w-full rounded-md border border-input bg-background py-2 pl-8 pr-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
          />
        </div>
        <select
          value={category}
          onChange={(e) => updateFilters({ category: e.target.value || null })}
          aria-label={t("category_filter_label")}
          className="min-h-11 min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        >
          <option value="">{t("filter_all_categories")}</option>
          {category && !facets?.categories.some((item) => item.name === category) && (
            <option value={category}>{category}</option>
          )}
          {facets?.categories.map((c) => (
            <option key={c.name} value={c.name} title={c.name}>
              {prettyCategoryLabel(c)} ({c.count})
            </option>
          ))}
        </select>
        <select
          value={brand}
          onChange={(e) => updateFilters({ brand: e.target.value || null })}
          aria-label={t("brand_filter_label")}
          className="min-h-11 min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        >
          <option value="">{t("filter_all_brands")}</option>
          {brand && !facets?.brands.slice(0, 50).some((item) => item.name === brand) && (
            <option value={brand}>{brand}</option>
          )}
          {facets?.brands.slice(0, 50).map((b) => (
            <option key={b.name} value={b.name}>
              {b.name} ({b.count})
            </option>
          ))}
        </select>
        <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9">
          <input
            type="checkbox"
            checked={onSaleOnly}
            onChange={(e) => updateFilters({ sale: e.target.checked })}
            className="h-4 w-4"
          />
          {t("discount_label")}
        </label>
      </div>

      {(productsQ.isLoading || isCanonicalizingPage) && (
        <div className="text-sm text-muted-foreground py-6 text-center">
          {tCommon("loading")}
        </div>
      )}
      {productsQ.error && (
        <QueryErrorState
          message={friendlyError(productsQ.error, locale)}
          retryLabel={tCommon("retry")}
          onRetry={() => productsQ.refetch()}
        />
      )}

      {!productsQ.isLoading && !productsQ.error && !isCanonicalizingPage && (
        <>
          <div className="hidden md:block rounded-lg border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-3 py-2 text-left">{t("th_name")}</th>
                  <th className="px-3 py-2 text-left">{t("th_brand")}</th>
                  <th className="px-3 py-2 text-left">{t("th_category")}</th>
                  <th className="px-3 py-2 text-right">{t("th_price")}</th>
                  <th className="px-3 py-2 text-right">{t("th_discount_col")}</th>
                  <th className="px-3 py-2 text-right">{t("th_trend")}</th>
                  <th className="px-3 py-2 w-8"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <ProductRow key={p.id} product={p} site={site} />
                ))}
                {items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-3 py-6 text-center text-muted-foreground">
                      {t("empty_products")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="md:hidden space-y-2">
            {items.map((p) => (
              <ProductCard key={p.id} product={p} />
            ))}
            {items.length === 0 && (
              <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
                {t("empty_products")}
              </div>
            )}
          </div>

          <Pagination
            offset={offset}
            limit={PAGE_LIMIT}
            total={total}
            onChange={(nextOffset) =>
              updateFilters(
                { page: nextOffset === 0 ? null : nextOffset / PAGE_LIMIT + 1 },
                false,
              )
            }
          />
        </>
      )}
    </section>
  );
}

function ProductRow({ product, site }: { product: SiteProduct; site: SiteName }) {
  const t = useTranslations("site");
  const locale = useLocale();
  // Lazy-load price history per row (React Query dedups + caches)
  const historyQ = useQuery({
    queryKey: ["product-price-history", product.id, 30],
    queryFn: () => api.productPriceHistory(product.id, 30),
    staleTime: 5 * 60_000, // 5 min — данные обновляются раз в день
  });

  return (
    <tr className="border-t border-border hover:bg-muted/30">
      <td className="px-3 py-2">
        <a
          href={product.url}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:underline"
        >
          {product.name}
        </a>
      </td>
      <td className="px-3 py-2 text-muted-foreground">{product.brand ?? "—"}</td>
      <td className="px-3 py-2 text-xs font-mono text-muted-foreground">
        {product.category ?? "—"}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {product.is_on_sale && product.price != null ? (
          <span className="line-through text-muted-foreground/70">
            {formatPrice(product.price, locale)}
          </span>
        ) : (
          formatPrice(product.price, locale)
        )}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {product.is_on_sale && product.discount_price != null ? (
          <span className="text-success font-medium">
            {formatPrice(product.discount_price, locale)}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2 text-right">
        {historyQ.data ? (
          <Sparkline
            points={historyQ.data.points}
            delta_pct={historyQ.data.delta_pct}
          />
        ) : (
          <span className="text-xs text-muted-foreground/70">…</span>
        )}
      </td>
      <td className="px-3 py-2">
        <a
          href={product.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-muted-foreground hover:text-foreground"
          title={t("open_link", { site })}
        >
          <ExternalLink className="h-4 w-4" />
        </a>
      </td>
    </tr>
  );
}

function ProductCard({ product }: { product: SiteProduct }) {
  const locale = useLocale();
  return (
    <a
      href={product.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block rounded-lg border border-border bg-card p-3 hover:bg-muted/30 transition-colors"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-sm">{product.name}</div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {product.brand ?? "—"}
            {product.category && (
              <span className="ml-1 text-muted-foreground/70">· {product.category}</span>
            )}
          </div>
        </div>
        <div className="text-right shrink-0 tabular-nums">
          {product.is_on_sale && product.discount_price != null ? (
            <>
              <div className="text-success font-semibold text-sm">
                {formatPrice(product.discount_price, locale)}
              </div>
              <div className="text-xs text-muted-foreground line-through">
                {formatPrice(product.price, locale)}
              </div>
            </>
          ) : (
            <div className="text-sm font-semibold">{formatPrice(product.price, locale)}</div>
          )}
        </div>
      </div>
    </a>
  );
}

function Pagination({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  const t = useTranslations("site");
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  if (total === 0) return null;
  return (
    <div className="flex items-center justify-between gap-2 mt-3 text-sm">
      <div className="text-muted-foreground tabular-nums">
        {t("pagination_range", { from: offset + 1, to: Math.min(offset + limit, total), total })}
      </div>
      <div className="flex gap-2">
        <button
          disabled={!canPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="min-h-11 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-muted/50 disabled:opacity-40 md:min-h-9"
        >
          {t("pagination_prev")}
        </button>
        <div className="px-3 py-1.5 text-sm tabular-nums text-muted-foreground">
          {t("pagination_page", { page, total: totalPages })}
        </div>
        <button
          disabled={!canNext}
          onClick={() => onChange(offset + limit)}
          className="min-h-11 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-muted/50 disabled:opacity-40 md:min-h-9"
        >
          {t("pagination_next")}
        </button>
      </div>
    </div>
  );
}
