"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import {
  api,
  isFullCatalogTrustError,
  type CategoryComparisonRow,
} from "@/lib/api";
import { formatPrice } from "@/lib/utils";
import { MetricStrip } from "@/components/metric-strip";
import { TableSkeleton } from "@/components/skeleton";

// Конкуренты показываем отдельными колонками (клиент = pharmonline).
const COMPETITORS = ["aptekonline", "aloe"] as const;

type SortKey = "misprice" | "label" | "skus" | "client" | "index" | "cheaper";

export default function CategoryComparisonPage() {
  const t = useTranslations("category_comparison");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedCategory = searchParams.get("category");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "misprice",
    dir: "desc",
  });
  const [expandedCategory, setExpandedCategory] = useState<string | null>(null);

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["category-comparison", locale],
    queryFn: () => api.categoryComparison("pharmonline", locale),
  });
  const { data: coverage, isLoading: isCoverageLoading } = useQuery({
    queryKey: ["category-comparison-coverage", "pharmonline"],
    queryFn: () => api.categoryComparisonCoverage("pharmonline"),
  });
  const { data: catalogRows, isLoading: isCatalogLoading } = useQuery({
    queryKey: ["category-comparison-catalog", "pharmonline", locale],
    queryFn: () => api.categoryCatalog("pharmonline", locale),
  });

  // Дефолт-сортировка — наибольший «мисприсинг» |index-100|×matched_skus:
  // вверху категории где клиент сильнее всего отклонён от рынка и с весом SKU.
  const visibleRows = useMemo(() => {
    if (!data) return data;
    if (!selectedCategory) return data;
    return data.filter((row) => row.category === selectedCategory);
  }, [data, selectedCategory]);

  const sorted = useMemo(() => {
    if (!visibleRows) return visibleRows;
    const val = (r: CategoryComparisonRow): number | string => {
      switch (sort.key) {
        case "label":
          return r.label.toLowerCase();
        case "skus":
          return r.matched_skus;
        case "client":
          return r.avg_client_price;
        case "index":
          return r.index;
        case "cheaper":
          return r.cheaper_pct;
        default:
          return Math.abs(r.index - 100) * r.matched_skus;
      }
    };
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...visibleRows].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * dir;
      }
      return ((va as number) - (vb as number)) * dir;
    });
  }, [visibleRows, sort]);

  // KPI: категорий где клиент дешевле рынка (index<100), SKU-взвешенный средний
  // индекс, всего matched SKU.
  const kpi = useMemo(() => {
    if (!visibleRows || visibleRows.length === 0) {
      return { cheaperCats: 0, avgIndex: null as number | null, totalSkus: 0 };
    }
    const cheaperCats = visibleRows.filter((r) => r.index < 100).length;
    const totalSkus = visibleRows.reduce((s, r) => s + r.matched_skus, 0);
    const weighted = visibleRows.reduce(
      (s, r) => s + r.index * r.matched_skus,
      0,
    );
    const avgIndex = totalSkus > 0 ? weighted / totalSkus : null;
    return { cheaperCats, avgIndex, totalSkus };
  }, [visibleRows]);

  function toggleSort(key: SortKey) {
    setSort((cur) =>
      cur.key === key
        ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "label" ? "asc" : "desc" },
    );
  }

  function drill(category: string) {
    router.push(`/comparison?category=${encodeURIComponent(category)}`);
  }

  function handleExportCsv() {
    if (!sorted || sorted.length === 0) return;
    const header = [
      "category",
      "label",
      "matched_skus",
      "avg_client_price",
      ...COMPETITORS.map((s) => `${s}_avg`),
      "avg_competitor_price",
      "index",
      "cheaper_pct",
      "pricier_pct",
      "parity_pct",
    ];
    const escape = (v: unknown): string => {
      if (v == null) return "";
      const s = String(v);
      if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
      return s;
    };
    const lines = [header.join(",")];
    for (const r of sorted) {
      const row = [
        escape(r.category),
        escape(r.label),
        String(r.matched_skus),
        r.avg_client_price.toFixed(2),
        ...COMPETITORS.map((s) =>
          r.per_site_avg[s] != null ? r.per_site_avg[s].toFixed(2) : "",
        ),
        r.avg_competitor_price.toFixed(2),
        r.index.toFixed(1),
        r.cheaper_pct.toFixed(1),
        r.pricier_pct.toFixed(1),
        r.parity_pct.toFixed(1),
      ];
      lines.push(row.join(","));
    }
    const blob = new Blob([lines.join("\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const today = new Date().toISOString().slice(0, 10);
    a.download = `category_comparison_${today}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("title")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
          {selectedCategory && (
            <div className="mt-2 inline-flex items-center gap-2 rounded-full bg-primary/10 px-3 py-1 text-xs text-primary">
              {t("filter_category", { category: selectedCategory })}
              <button
                onClick={() => router.replace("/category-comparison")}
                className="hover:text-primary/70"
                aria-label={t("filter_clear")}
                title={t("filter_clear")}
              >
                ×
              </button>
            </div>
          )}
        </div>
        <button
          onClick={handleExportCsv}
          disabled={!sorted || sorted.length === 0}
          className="shrink-0 inline-flex min-h-11 items-center gap-1.5 rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
          title={t("export_csv_title")}
        >
          ⬇ CSV
        </button>
      </div>

      {/* KPI */}
      <section aria-label={t("title")}>
        <MetricStrip
          items={[
            {
              label: t("kpi_cheaper_cats"),
              value: visibleRows ? `${kpi.cheaperCats} / ${visibleRows.length}` : "—",
              loading: isLoading,
              hint: t("kpi_cheaper_cats_hint"),
            },
            {
              label: t("kpi_avg_index"),
              value: kpi.avgIndex != null ? kpi.avgIndex.toFixed(1) : "—",
              loading: isLoading,
              hint: t("kpi_avg_index_hint"),
            },
            {
              label: t("kpi_total_skus"),
              value: kpi.totalSkus || "—",
              loading: isLoading,
              hint: t("kpi_total_skus_hint"),
            },
          ]}
        />
      </section>

      <section aria-label={t("coverage_title")}>
        <h2 className="mb-2 text-sm font-medium">{t("coverage_title")}</h2>
        <MetricStrip
          items={[
            {
              label: t("coverage_catalog"),
              value: coverage?.catalog_skus ?? "—",
              loading: isCoverageLoading,
            },
            {
              label: t("coverage_categorized"),
              value: coverage
                ? `${coverage.categorized_skus} (${coverage.categorization_pct.toFixed(1)}%)`
                : "—",
              loading: isCoverageLoading,
            },
            {
              label: t("coverage_matched"),
              value: coverage
                ? `${coverage.matched_skus} (${coverage.matching_pct.toFixed(1)}%)`
                : "—",
              loading: isCoverageLoading,
              hint: t("coverage_matched_hint"),
            },
          ]}
        />
      </section>

      <section className="rounded-lg border border-border overflow-hidden">
        <div className="border-b border-border p-4">
          <h2 className="font-medium">{t("catalog_title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("catalog_hint")}
          </p>
        </div>
        {isCatalogLoading ? (
          <div className="p-4 text-sm text-muted-foreground">
            {tCommon("loading")}
          </div>
        ) : (
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-muted/90 text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">
                    {t("th_category")}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {t("catalog_all_skus")}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {t("catalog_comparable_skus")}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {t("manual_action")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {catalogRows?.map((row) => (
                  <Fragment key={`catalog-${row.category}`}>
                    <tr className="border-t border-border">
                      <td className="px-3 py-2 font-medium">{row.label}</td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {row.catalog_skus}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                        {row.comparable_skus}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedCategory((current) =>
                              current === row.category ? null : row.category,
                            )
                          }
                          className="rounded-md border border-input px-2 py-1 text-xs hover:bg-muted"
                        >
                          {t("manual_add_product")}
                        </button>
                      </td>
                    </tr>
                    {expandedCategory === row.category && (
                      <tr className="border-t border-border">
                        <td colSpan={4} className="bg-muted/20 p-3">
                          <InlineCategoryAssigner
                            categoryKey={row.category}
                            categoryLabel={row.label}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Status */}
      {(isLoading || isFetching) && (
        <div className="text-xs text-muted-foreground" data-testid="loading">
          {isLoading ? tCommon("loading") : tCommon("updating")}
        </div>
      )}
      {error && (
        <div
          className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive"
          data-testid="error"
        >
          {isFullCatalogTrustError(error)
            ? tCommon("financial_data_paused")
            : tCommon("error")}
        </div>
      )}
      {isLoading && <TableSkeleton rows={8} cols={7} />}
      {visibleRows && visibleRows.length === 0 && !isLoading && (
        <div
          className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center"
          data-testid="empty"
        >
          {t("empty")}
        </div>
      )}

      {/* Mobile cards */}
      <div className="md:hidden space-y-2" data-testid="mobile-list">
        {sorted?.map((row) => (
          <CategoryCard
            key={row.category}
            row={row}
            onClick={() => drill(row.category)}
          />
        ))}
      </div>

      {/* Desktop table */}
      {visibleRows && visibleRows.length > 0 && (
        <div
          className="hidden md:block rounded-lg border border-border overflow-hidden"
          data-testid="desktop-table"
        >
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <SortableTh
                  label={t("th_category")}
                  active={sort}
                  col="label"
                  onClick={() => toggleSort("label")}
                  align="left"
                />
                <SortableTh
                  label={t("th_skus")}
                  active={sort}
                  col="skus"
                  onClick={() => toggleSort("skus")}
                  align="right"
                />
                <SortableTh
                  label={t("th_client")}
                  active={sort}
                  col="client"
                  onClick={() => toggleSort("client")}
                  align="right"
                />
                {COMPETITORS.map((s) => (
                  <th key={s} className="px-3 py-2 text-right font-medium">
                    {s}
                  </th>
                ))}
                <SortableTh
                  label={t("th_index")}
                  active={sort}
                  col="index"
                  onClick={() => toggleSort("index")}
                  align="right"
                />
                <SortableTh
                  label={t("th_cheaper")}
                  active={sort}
                  col="cheaper"
                  onClick={() => toggleSort("cheaper")}
                  align="right"
                />
                <th className="px-3 py-2 text-right font-medium">
                  {t("manual_action")}
                </th>
              </tr>
            </thead>
            <tbody>
              {sorted?.map((row) => (
                <Fragment key={row.category}>
                  <tr
                    key={row.category}
                    onClick={() => drill(row.category)}
                    className="border-t border-border hover:bg-muted/30 cursor-pointer"
                    title={t("drill_hint")}
                    data-testid={`cat-row-${row.category}`}
                  >
                    <td className="px-3 py-2 max-w-xs truncate font-medium">
                      {row.label}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {row.matched_skus}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {formatPrice(row.avg_client_price, locale)}
                    </td>
                    {COMPETITORS.map((s) => (
                      <td key={s} className="px-3 py-2 text-right tabular-nums">
                        {row.per_site_avg[s] != null ? (
                          formatPrice(row.per_site_avg[s], locale)
                        ) : (
                          <span className="text-muted-foreground/50">—</span>
                        )}
                      </td>
                    ))}
                    <td className="px-3 py-2 text-right">
                      <IndexBadge index={row.index} t={t} />
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {row.cheaper_pct.toFixed(0)}%
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setExpandedCategory((current) =>
                            current === row.category ? null : row.category,
                          );
                        }}
                        className="rounded-md border border-input px-2 py-1 text-xs hover:bg-muted"
                      >
                        {t("manual_add_product")}
                      </button>
                    </td>
                  </tr>
                  {expandedCategory === row.category && (
                    <tr key={`${row.category}-assign`} className="border-t border-border">
                      <td colSpan={8} className="bg-muted/20 p-3">
                        <InlineCategoryAssigner
                          categoryKey={row.category}
                          categoryLabel={row.label}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {visibleRows && visibleRows.length > 0 && (
        <div
          className="text-xs text-muted-foreground text-center"
          data-testid="result-count"
        >
          {t("result_count", { count: visibleRows.length })}
        </div>
      )}
    </div>
  );
}

function InlineCategoryAssigner({
  categoryKey,
  categoryLabel,
}: {
  categoryKey: string;
  categoryLabel: string;
}) {
  const t = useTranslations("category_comparison");
  const tCommon = useTranslations("common");
  const [search, setSearch] = useState("");
  const queryClient = useQueryClient();
  const { data: suggestions, isFetching } = useQuery({
    queryKey: ["category-product-suggestions", search],
    queryFn: () => api.categoryProductSuggestions(search),
    enabled: search.trim().length >= 2,
  });
  const assignment = useMutation({
    mutationFn: (productId: number) =>
      api.assignProductCategory(productId, categoryKey),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["category-comparison"] }),
        queryClient.invalidateQueries({
          queryKey: ["category-comparison-coverage"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["category-comparison-catalog"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["category-product-suggestions"],
        }),
      ]);
    },
  });

  return (
    <div>
      <div className="text-xs font-medium">
        {t("manual_add_to", { category: categoryLabel })}
      </div>
      <input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder={t("manual_search_placeholder")}
        autoFocus
        className="mt-2 min-h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
      />
      {search.trim().length >= 2 && (
        <div className="mt-2 divide-y divide-border rounded-md border border-border bg-background">
          {isFetching && (
            <div className="p-3 text-xs text-muted-foreground">
              {tCommon("loading")}
            </div>
          )}
          {suggestions?.map((product) => (
            <div
              key={product.id}
              className="flex items-center justify-between gap-3 p-3"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{product.name}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {product.manual_category_key
                    ? t("manual_current", {
                        category: product.manual_category_key,
                      })
                    : product.source_category || t("manual_uncategorized")}
                </div>
              </div>
              <button
                type="button"
                disabled={assignment.isPending}
                onClick={() => assignment.mutate(product.id)}
                className="shrink-0 rounded-md bg-primary px-3 py-2 text-xs text-primary-foreground disabled:opacity-40"
              >
                {assignment.isPending ? tCommon("loading") : t("manual_assign")}
              </button>
            </div>
          ))}
          {!isFetching && suggestions?.length === 0 && (
            <div className="p-3 text-xs text-muted-foreground">
              {t("manual_no_results")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SortableTh({
  label,
  col,
  active,
  onClick,
  align,
}: {
  label: string;
  col: SortKey;
  active: { key: SortKey; dir: "asc" | "desc" };
  onClick: () => void;
  align: "left" | "right";
}) {
  const isActive = active.key === col;
  return (
    <th
      className={`px-3 py-2 font-medium ${align === "right" ? "text-right" : "text-left"}`}
    >
      <button
        onClick={onClick}
        className={`inline-flex items-center gap-0.5 hover:text-foreground ${
          align === "right" ? "flex-row-reverse" : ""
        } ${isActive ? "text-foreground" : ""}`}
      >
        {label}
        {isActive &&
          (active.dir === "asc" ? (
            <ChevronUp className="h-3 w-3" />
          ) : (
            <ChevronDown className="h-3 w-3" />
          ))}
      </button>
    </th>
  );
}

/**
 * Ценовой индекс: <100 клиент дешевле рынка (зелёный, хорошо), >100 дороже
 * (красный), ≈100 паритет (muted). Полоса ±0.5 вокруг 100 = паритет.
 */
function IndexBadge({
  index,
  t,
}: {
  index: number;
  t: ReturnType<typeof useTranslations>;
}) {
  const cheaper = index < 99.5;
  const pricier = index > 100.5;
  const color = cheaper
    ? "text-success"
    : pricier
      ? "text-destructive"
      : "text-muted-foreground";
  const title = cheaper
    ? t("index_cheaper")
    : pricier
      ? t("index_pricier")
      : t("index_parity");
  return (
    <span
      className={`inline-flex items-center gap-0.5 tabular-nums font-medium ${color}`}
      title={title}
    >
      {cheaper ? "▼" : pricier ? "▲" : "•"}
      {index.toFixed(1)}
    </span>
  );
}

function CategoryCard({
  row,
  onClick,
}: {
  row: CategoryComparisonRow;
  onClick: () => void;
}) {
  const t = useTranslations("category_comparison");
  const locale = useLocale();
  return (
    <button
      onClick={onClick}
      className="w-full text-left rounded-lg border border-border bg-card p-3 hover:bg-muted/30"
      data-testid={`cat-card-${row.category}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-sm truncate">{row.label}</span>
        <IndexBadge index={row.index} t={t} />
      </div>
      <div className="text-xs text-muted-foreground mt-1">
        {t("card_skus", { count: row.matched_skus })} ·{" "}
        {t("card_cheaper", { pct: row.cheaper_pct.toFixed(0) })}
      </div>
      <div className="grid grid-cols-3 gap-2 mt-2 text-center">
        <div>
          <div className="text-[10px] text-muted-foreground uppercase">
            pharmonline
          </div>
          <div className="tabular-nums text-sm">
            {formatPrice(row.avg_client_price, locale)}
          </div>
        </div>
        {COMPETITORS.map((s) => (
          <div key={s}>
            <div className="text-[10px] text-muted-foreground uppercase">
              {s}
            </div>
            <div className="tabular-nums text-sm">
              {row.per_site_avg[s] != null
                ? formatPrice(row.per_site_avg[s], locale)
                : "—"}
            </div>
          </div>
        ))}
      </div>
    </button>
  );
}
