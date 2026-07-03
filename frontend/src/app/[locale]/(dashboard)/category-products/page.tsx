"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { api, type SiteProduct } from "@/lib/api";
import { formatPrice } from "@/lib/utils";

const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type SiteName = (typeof SITES)[number];
const PAGE_LIMIT = 500;

export default function CategoryProductsPage() {
  const t = useTranslations("category_products");
  const tCommon = useTranslations("common");
  const searchParams = useSearchParams();
  const label = searchParams.get("label") || t("fallback_label");
  const pharmonline = searchParams.get("pharmonline") || "";
  const aptekonline = searchParams.get("aptekonline") || "";
  const aloe = searchParams.get("aloe") || "";

  const pharmonlineQ = useCategoryProducts("pharmonline", pharmonline);
  const aptekonlineQ = useCategoryProducts("aptekonline", aptekonline);
  const aloeQ = useCategoryProducts("aloe", aloe);

  const sections = [
    { site: "pharmonline" as const, slug: pharmonline, query: pharmonlineQ },
    { site: "aptekonline" as const, slug: aptekonline, query: aptekonlineQ },
    { site: "aloe" as const, slug: aloe, query: aloeQ },
  ].filter((section) => section.slug);
  const missingSites = SITES.filter(
    (site) =>
      !sections.some((section) => section.site === site),
  ).map((site) => `${site}.az`);

  const configuredSites = sections.length;
  const totalProducts = sections.reduce(
    (sum, section) => sum + (section.query.data?.pages[0]?.total ?? 0),
    0,
  );

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("subtitle", { label })}
        </p>
      </header>

      <div className="grid gap-3 md:grid-cols-3">
        <Metric label={t("configured_sites")} value={configuredSites} />
        <Metric label={t("total_products")} value={totalProducts} />
        <Metric label={t("page_limit")} value={PAGE_LIMIT} />
      </div>

      {sections.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {t("no_sites")}
        </div>
      )}

      {sections.length > 0 && missingSites.length > 0 && (
        <div
          className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-200"
          data-testid="category-products-missing-sites"
        >
          {t("missing_sites", { sites: missingSites.join(", ") })}
        </div>
      )}

      <div className="space-y-4">
        {sections.map(({ site, slug, query }) => (
          <SiteSection
            key={site}
            site={site}
            slug={slug}
            products={query.data?.pages.flatMap((page) => page.items) ?? []}
            total={query.data?.pages[0]?.total ?? 0}
            isLoading={query.isLoading}
            isError={query.isError}
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            onLoadMore={() => query.fetchNextPage()}
            loadingText={tCommon("loading")}
          />
        ))}
      </div>
    </div>
  );
}

function useCategoryProducts(site: SiteName, category: string) {
  return useInfiniteQuery({
    queryKey: ["category-products", site, category],
    queryFn: ({ pageParam = 0 }) =>
      api.siteProducts({ site, category, limit: PAGE_LIMIT, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.items.length;
      return nextOffset < lastPage.total ? nextOffset : undefined;
    },
    enabled: Boolean(category),
  });
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function SiteSection({
  site,
  slug,
  products,
  total,
  isLoading,
  isError,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  loadingText,
}: {
  site: SiteName;
  slug: string;
  products: SiteProduct[];
  total: number;
  isLoading: boolean;
  isError: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  loadingText: string;
}) {
  const t = useTranslations("category_products");
  const shown = products.length;

  return (
    <section className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex flex-col gap-1 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold">{site}.az</h2>
          <p className="text-xs text-muted-foreground">
            {t("site_slug", { slug })}
          </p>
        </div>
        <div className="text-xs text-muted-foreground tabular-nums">
          {t("shown_count", { shown, total })}
        </div>
      </div>

      {isLoading && (
        <div className="p-5 text-sm text-muted-foreground">{loadingText}</div>
      )}
      {isError && (
        <div className="p-5 text-sm text-destructive">{t("load_error")}</div>
      )}
      {!isLoading && !isError && products.length === 0 && (
        <div className="p-5 text-sm text-muted-foreground">{t("empty")}</div>
      )}

      {!isLoading && !isError && products.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">{t("th_name")}</th>
                <th className="px-3 py-2 text-left">{t("th_brand")}</th>
                <th className="px-3 py-2 text-right">{t("th_price")}</th>
                <th className="px-3 py-2 w-8"></th>
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <tr key={product.id} className="border-t border-border">
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
                  <td className="px-3 py-2 text-muted-foreground">
                    {product.brand ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatPrice(product.effective_price ?? product.price)}
                  </td>
                  <td className="px-3 py-2">
                    <a
                      href={product.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-muted-foreground hover:text-foreground"
                      title={t("open_product")}
                    >
                      <ExternalLink className="h-4 w-4" />
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-3 text-xs text-muted-foreground">
            <span className="tabular-nums">
              {t("shown_count", { shown, total })}
            </span>
            {hasNextPage && (
              <button
                type="button"
                onClick={onLoadMore}
                disabled={isFetchingNextPage}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-xs font-medium text-foreground hover:bg-secondary disabled:opacity-50"
              >
                {isFetchingNextPage ? loadingText : t("load_more")}
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
