"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { X, ChevronUp, ChevronDown, ChevronsUpDown, TrendingUp, TrendingDown } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { api, ApiError, type ComparisonRow } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatPct } from "@/lib/utils";
import { OnboardingTip } from "@/components/onboarding-tip";
import { Sparkline } from "@/components/sparkline";
import { TableSkeleton } from "@/components/skeleton";

const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type SiteName = typeof SITES[number];
type SortKey = "name" | "brand" | "spread" | SiteName;

export default function ComparisonPage() {
  const t = useTranslations("comparison");
  const tCommon = useTranslations("common");
  const searchParams = useSearchParams();
  const router = useRouter();
  // Drill-down из /category-comparison: фильтр по категории товара-клиента.
  const category = searchParams.get("category");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [minSites, setMinSites] = useState(2);
  const [diffOnly, setDiffOnly] = useState(false);
  const [withAloe, setWithAloe] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "spread",
    dir: "desc",
  });
  const queryClient = useQueryClient();

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["comparison", debouncedSearch, minSites, category],
    queryFn: () =>
      api.comparison({
        search: debouncedSearch,
        min_sites: minSites,
        category: category ?? undefined,
      }),
  });

  // Client-side filters + user sort. Дефолт: |spread_pct| desc — самое полезное
  // для PO (где конкурент бьёт по цене / где можем поднять). Клик по заголовку
  // колонки меняет ключ/направление сортировки.
  const filtered = useMemo(() => {
    if (!data) return data;
    const rows = data.filter((r) => {
      if (diffOnly && (!r.spread_pct || r.spread_pct < 0.5)) return false;
      if (withAloe && !r.prices["aloe"]) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      if (sort.key === "name") return (a.name ?? "").localeCompare(b.name ?? "") * dir;
      if (sort.key === "brand")
        return (a.brand ?? "").localeCompare(b.brand ?? "") * dir;
      if (sort.key === "spread")
        return (Math.abs(a.spread_pct ?? 0) - Math.abs(b.spread_pct ?? 0)) * dir;
      // per-site price: отсутствующая цена всегда внизу, независимо от dir
      const av = a.prices[sort.key]?.price;
      const bv = b.prices[sort.key]?.price;
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av - bv) * dir;
    });
  }, [data, diffOnly, withAloe, sort]);

  function toggleSort(key: SortKey) {
    setSort((cur) =>
      cur.key === key
        ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "name" || key === "brand" ? "asc" : "desc" },
    );
  }

  // P1.2: auto-collapse колонок без данных в текущем срезе. Например когда
  // включён фильтр «Только различия» — почти все строки могут не иметь aloe.
  const visibleSites = useMemo(() => {
    if (!filtered) return SITES;
    return SITES.filter((s) => filtered.some((r) => r.prices[s] != null));
  }, [filtered]);

  // P1.2: Export CSV. Берёт уже-отфильтрованный + отсортированный набор.
  function handleExportCsv() {
    if (!filtered || filtered.length === 0) return;
    const header = [
      "id",
      "name",
      "brand",
      ...SITES.flatMap((s) => [`${s}_price`, `${s}_url`]),
      "spread_pct",
      "cheapest_site",
    ];
    const escape = (v: unknown): string => {
      if (v == null) return "";
      const s = String(v);
      // RFC 4180: escape если содержит ", , или newline
      if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
      return s;
    };
    const lines = [header.join(",")];
    for (const r of filtered) {
      const row: string[] = [
        String(r.canonical_id),
        escape(r.name),
        escape(r.brand ?? ""),
      ];
      for (const s of SITES) {
        row.push(r.prices[s]?.price != null ? r.prices[s].price.toFixed(2) : "");
        row.push(escape(r.prices[s]?.url ?? ""));
      }
      row.push(r.spread_pct != null ? r.spread_pct.toFixed(2) : "");
      row.push(escape(r.cheapest_site ?? ""));
      lines.push(row.join(","));
    }
    const blob = new Blob([lines.join("\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const today = new Date().toISOString().slice(0, 10);
    a.download = `comparison_${today}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const rejectMutation = useMutation({
    mutationFn: (id: number) => api.rejectMatch(id),
    onMutate: async (id: number) => {
      // Optimistic: filter the row out immediately
      await queryClient.cancelQueries({ queryKey: ["comparison"] });
      const prev = queryClient.getQueryData<ComparisonRow[]>([
        "comparison", debouncedSearch, minSites, category,
      ]);
      queryClient.setQueryData<ComparisonRow[]>(
        ["comparison", debouncedSearch, minSites, category],
        (old) => old?.filter((r) => r.canonical_id !== id),
      );
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      // Rollback
      if (ctx?.prev) {
        queryClient.setQueryData(
          ["comparison", debouncedSearch, minSites, category],
          ctx.prev,
        );
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["comparison"] });
      queryClient.invalidateQueries({ queryKey: ["match-quality"] });
    },
  });

  function handleReject(row: ComparisonRow) {
    if (!confirm(t("reject_confirm", { name: row.name, brand: row.brand ?? "—" }))) {
      return;
    }
    rejectMutation.mutate(row.canonical_id);
  }

  return (
    <div className="space-y-4">
      <OnboardingTip
        id="comparison-sort-v2"
        title={t("how_to_read_title")}
        description={t.rich("how_to_read", {
          green: (chunks) => <span className="text-success">{chunks}</span>,
          red: (chunks) => <span className="text-destructive">{chunks}</span>,
          b: (chunks) => <b>{chunks}</b>,
        })}
      />

      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("page_title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("page_subtitle")}
          </p>
        </div>
        <button
          onClick={handleExportCsv}
          disabled={!filtered || filtered.length === 0}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          title={t("export_csv_title")}
        >
          ⬇ CSV
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-2">
        <input
          type="search"
          placeholder={t("search_placeholder")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="search-input"
        />
        <select
          value={minSites}
          onChange={(e) => setMinSites(Number(e.target.value))}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          data-testid="min-sites-select"
        >
          <option value={3}>{t("min_sites_3")}</option>
          <option value={2}>{t("min_sites_2")}</option>
          <option value={1}>{t("min_sites_1")}</option>
        </select>
        <select
          value={`${sort.key}:${sort.dir}`}
          onChange={(e) => {
            const [k, d] = e.target.value.split(":");
            setSort({ key: k as SortKey, dir: d as "asc" | "desc" });
          }}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          data-testid="sort-select"
          title={t("sort_label")}
          aria-label={t("sort_label")}
        >
          <optgroup label={t("sort_label")}>
            <option value="spread:desc">{t("th_spread")} ↓</option>
            <option value="spread:asc">{t("th_spread")} ↑</option>
            <option value="name:asc">{t("th_name")} ↑</option>
            <option value="name:desc">{t("th_name")} ↓</option>
            <option value="brand:asc">{t("th_brand")} ↑</option>
            <option value="brand:desc">{t("th_brand")} ↓</option>
            <option value="pharmonline:asc">pharmonline ↑</option>
            <option value="pharmonline:desc">pharmonline ↓</option>
            <option value="aptekonline:asc">aptekonline ↑</option>
            <option value="aptekonline:desc">aptekonline ↓</option>
            <option value="aloe:asc">aloe ↑</option>
            <option value="aloe:desc">aloe ↓</option>
          </optgroup>
        </select>
      </div>

      {/* Drill-down filter chip (из /category-comparison) */}
      {category && (
        <div className="flex items-center gap-2 text-sm" data-testid="category-filter-chip">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 text-primary px-3 py-1">
            {t("category_filter", { category })}
            <button
              onClick={() => router.replace("/comparison")}
              aria-label={t("category_filter_clear")}
              title={t("category_filter_clear")}
              className="hover:text-primary/70"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </span>
        </div>
      )}

      {/* Status */}
      {(isLoading || isFetching) && (
        <div className="text-xs text-muted-foreground" data-testid="loading">
          {isLoading ? tCommon("loading") : tCommon("updating")}
        </div>
      )}
      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive" data-testid="error">
          {tCommon("error")}
        </div>
      )}
      {data && data.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center" data-testid="empty">
          {t("empty")}
        </div>
      )}

      {/* Mobile: card list */}
      <div className="md:hidden space-y-2" data-testid="mobile-list">
        {filtered?.map((row) => (
          <ComparisonCard key={row.canonical_id} row={row} onReject={handleReject} />
        ))}
      </div>

      {/* Desktop: table */}
      <div className="hidden md:block rounded-lg border border-border overflow-hidden" data-testid="desktop-table">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <SortableTh label={t("th_name")} col="name" active={sort} onClick={() => toggleSort("name")} align="left" />
              <SortableTh label={t("th_brand")} col="brand" active={sort} onClick={() => toggleSort("brand")} align="left" />
              {visibleSites.map((s) => (
                <SortableTh key={s} label={s} col={s} active={sort} onClick={() => toggleSort(s)} align="right" />
              ))}
              <SortableTh label={t("th_spread")} col="spread" active={sort} onClick={() => toggleSort("spread")} align="right" />
              <th className="px-3 py-2 w-10"></th>
              <th className="px-3 py-2 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {filtered?.map((row) => (
              <ComparisonRowDesktop
                key={row.canonical_id}
                row={row}
                sites={visibleSites}
                expanded={expandedId === row.canonical_id}
                onToggleExpand={() =>
                  setExpandedId((id) =>
                    id === row.canonical_id ? null : row.canonical_id,
                  )
                }
                onReject={handleReject}
              />
            ))}
          </tbody>
        </table>
      </div>

      {filtered && filtered.length > 0 && (
        <div className="text-xs text-muted-foreground text-center" data-testid="result-count">
          {t("result_count", { count: filtered.length })}
        </div>
      )}
    </div>
  );
}

/**
 * P1.2 (PO Audit 2026-05-17): spread sign + arrow. PO ранее видел `+57.1%`
 * и не понимал — клиент дешевле или дороже. Теперь:
 *   ▲ красный  = у клиента (cheapest_site) самая высокая цена → конкурент дешевле
 *   ▼ зелёный  = у клиента самая низкая → у нас лучший price
 *   • muted    = нет cheapest_site (паритет / нет sites with price)
 */
function SpreadCell({ row }: { row: ComparisonRow }) {
  const t = useTranslations("comparison");
  if (row.spread_pct == null || row.cheapest_site == null) {
    return <span className="text-muted-foreground/70">—</span>;
  }
  const abs = Math.abs(row.spread_pct);
  // По умолчанию клиент = pharmonline (см. roi.CLIENT_SITE). Stable, простой
  // эвристический сигнал направления цены: если pharmonline cheapest →
  // зелёный ▼; если cheapest другой → красный ▲ (конкурент бьёт нас по цене).
  const clientCheapest = row.cheapest_site === "pharmonline";
  const Icon = clientCheapest
    ? () => <span aria-hidden>▼</span>
    : () => <span aria-hidden>▲</span>;
  const color = clientCheapest ? "text-success" : "text-destructive";
  const isUnit = row.spread_basis === "unit";
  return (
    <span
      className={`inline-flex items-center gap-0.5 tabular-nums ${color}`}
      title={
        (clientCheapest
          ? t("spread_we_cheaper", { pct: abs.toFixed(1) })
          : t("spread_they_cheaper", { site: row.cheapest_site, pct: abs.toFixed(1) })) +
        (isUnit ? ` · ${t("per_unit_note")}` : "")
      }
    >
      <Icon />
      {abs.toFixed(1)}%
      {isUnit && (
        <span
          className="text-[9px] font-normal text-muted-foreground ml-0.5"
          title={t("per_unit_note")}
        >
          /{t("unit_short")}
        </span>
      )}
    </span>
  );
}

function PriceCell({ row, site }: { row: ComparisonRow; site: string }) {
  const t = useTranslations("comparison");
  const locale = useLocale();
  const p = row.prices[site];
  if (!p) return <span className="text-muted-foreground/50">—</span>;
  // Per-unit basis (2026-05-29): min/max/cheapest посчитаны на цене-за-штуку,
  // поэтому подсветку дешёвого/дорогого считаем по unit_price, а не pack price.
  const isUnit = row.spread_basis === "unit";
  const cmpVal = isUnit && p.unit_price != null ? p.unit_price : p.price;
  // Stale-цена (2026-05-29) НЕ участвует в spread → не подсвечиваем как min/max,
  // приглушаем и зачёркиваем, показываем бейдж «N дн. назад».
  const stale = p.stale === true;
  const isMin = !stale && row.min_price === cmpVal;
  const isMax = !stale && row.max_price === cmpVal && row.min_price !== row.max_price;
  const showUnit =
    !stale && isUnit && p.pack_count != null && p.pack_count > 1 && p.unit_price != null;
  return (
    <a
      href={p.url}
      target="_blank"
      rel="noopener noreferrer"
      title={
        stale && p.age_days != null ? t("stale_note", { days: p.age_days }) : undefined
      }
      className={`inline-flex flex-col items-end tabular-nums hover:underline leading-tight ${
        stale
          ? "text-muted-foreground/60"
          : isMin
            ? "text-success font-semibold"
            : isMax
              ? "text-destructive"
              : ""
      }`}
    >
      <span className={stale ? "line-through decoration-muted-foreground/40" : ""}>
        {formatPrice(p.price, locale)}
      </span>
      {stale && p.age_days != null && (
        <span className="text-[10px] font-normal text-amber-600 dark:text-amber-500">
          {t("stale_badge", { days: p.age_days })}
        </span>
      )}
      {showUnit && (
        <span className="text-[10px] font-normal text-muted-foreground">
          {formatPrice(p.unit_price!, locale)}/{t("unit_short")} · {p.pack_count}{" "}
          {t("unit_short")}
        </span>
      )}
    </a>
  );
}

function ComparisonRowDesktop({
  row,
  sites,
  expanded,
  onToggleExpand,
  onReject,
}: {
  row: ComparisonRow;
  sites: readonly SiteName[];
  expanded: boolean;
  onToggleExpand: () => void;
  onReject: (r: ComparisonRow) => void;
}) {
  const t = useTranslations("comparison");
  return (
    <>
      <tr className="border-t border-border hover:bg-muted/30 group">
        <td className="px-3 py-2 max-w-md truncate">
          <span className="inline-flex items-center gap-1">
            {row.name}
            {row.needs_review && (
              <button
                onClick={onToggleExpand}
                className="text-amber-500 text-xs leading-none inline-flex items-center gap-0.5 rounded hover:text-amber-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                title={t("relink_title")}
                aria-label={t("relink_title")}
                aria-expanded={expanded}
                data-testid={`fix-${row.canonical_id}`}
              >
                ⚠ <span className="underline decoration-dotted">{t("relink_title")}</span>
              </button>
            )}
            {row.confidence < 0.95 && (
              <span
                className="text-muted-foreground/60 text-[10px] tabular-nums leading-none"
                title={t("match_confidence", { pct: Math.round(row.confidence * 100) })}
              >
                {Math.round(row.confidence * 100)}%
              </span>
            )}
          </span>
        </td>
        <td className="px-3 py-2 text-muted-foreground">{row.brand ?? "—"}</td>
        {sites.map((s) => (
          <td key={s} className="px-3 py-2 text-right">
            <PriceCell row={row} site={s} />
          </td>
        ))}
        <td className="px-3 py-2 text-right tabular-nums">
          <SpreadCell row={row} />
        </td>
        <td className="px-3 py-2">
          <button
            onClick={onToggleExpand}
            title={expanded ? t("trend_hide") : t("trend_show")}
            aria-label={expanded ? t("trend_hide") : t("trend_show")}
            aria-expanded={expanded}
            className="text-muted-foreground hover:text-foreground p-0.5 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid={`expand-${row.canonical_id}`}
          >
            {expanded ? (
              <ChevronUp className="h-4 w-4" />
            ) : (
              <ChevronDown className="h-4 w-4" />
            )}
          </button>
        </td>
        <td className="px-3 py-2">
          <button
            onClick={() => onReject(row)}
            title={t("reject_tooltip")}
            aria-label={t("reject_tooltip")}
            className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity text-muted-foreground hover:text-destructive rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid={`reject-${row.canonical_id}`}
          >
            <X className="h-4 w-4" />
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-muted/20 border-t border-border">
          <td colSpan={sites.length + 3} className="px-3 py-3">
            <TrendPanel row={row} />
            <RelinkPanel row={row} />
          </td>
        </tr>
      )}
    </>
  );
}

function ComparisonCard({
  row,
  onReject,
}: {
  row: ComparisonRow;
  onReject: (r: ComparisonRow) => void;
}) {
  const t = useTranslations("comparison");
  return (
    <div className="rounded-lg border border-border bg-card p-3 relative">
      <button
        onClick={() => onReject(row)}
        className="absolute top-2 right-2 text-muted-foreground hover:text-destructive p-1"
        title={t("reject_tooltip")}
        data-testid={`reject-mobile-${row.canonical_id}`}
      >
        <X className="h-4 w-4" />
      </button>
      <div className="font-medium text-sm pr-8 inline-flex items-center gap-1 flex-wrap">
        {row.name}
        {row.needs_review && (
          <span
            className="text-amber-500 text-xs leading-none"
            title={t("suspicious_spread")}
            aria-label={t("needs_review_tooltip")}
          >
            ⚠
          </span>
        )}
        {row.confidence < 0.95 && (
          <span
            className="text-muted-foreground/60 text-[10px] tabular-nums leading-none"
            title={t("match_confidence", { pct: Math.round(row.confidence * 100) })}
          >
            {Math.round(row.confidence * 100)}%
          </span>
        )}
      </div>
      <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-1.5">
        <span>
          {row.brand ?? "—"} · {t("sites_spread", { n: row.sites_with_price })}
        </span>
        <SpreadCell row={row} />
      </div>
      <div className="grid grid-cols-3 gap-2 mt-3">
        {SITES.map((s) => (
          <div key={s} className="text-center">
            <div className="text-[10px] text-muted-foreground uppercase">{s}</div>
            <div className="mt-0.5"><PriceCell row={row} site={s} /></div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TrendPanel({ row }: { row: ComparisonRow }) {
  const t = useTranslations("comparison");
  const locale = useLocale();
  const sitesWithPrice = SITES.filter((s) => row.prices[s]);
  // Batch fetch: 1 запрос вместо N×3. Сортируем ids для стабильного queryKey.
  const productIds = useMemo(
    () => sitesWithPrice.map((s) => row.prices[s].product_id).sort((a, b) => a - b),
    [row, sitesWithPrice],
  );
  const { data, isLoading, error } = useQuery({
    queryKey: ["price-history-batch", productIds, 30],
    queryFn: () => api.productPriceHistoryBatch(productIds, 30),
    staleTime: 60_000,
    enabled: productIds.length > 0,
  });

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {sitesWithPrice.map((s) => {
        const pid = row.prices[s].product_id;
        const ph = data?.[String(pid)];
        // Codex review fix (2026-05-28): отличаем batch-level error от
        // "продукт отсутствует в ответе" (missing pid). При batch-error все
        // сайты показывали no_data, маскируя partial успешные данные.
        return (
          <div key={s} className="flex items-center gap-2 text-xs">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground w-20 shrink-0">
              {s}
            </span>
            {isLoading ? (
              <span className="text-muted-foreground">…</span>
            ) : error ? (
              <span className="text-destructive/80 text-[11px]" title={String(error)}>
                {t("load_error")}
              </span>
            ) : !ph ? (
              <span className="text-muted-foreground/70">{t("no_data")}</span>
            ) : ph.points.filter((p) => p.price != null).length < 2 ? (
              <span className="text-muted-foreground/70 text-[11px]">
                {ph.current != null
                  ? t("stable_price", { price: formatPrice(ph.current, locale) })
                  : "—"}
              </span>
            ) : (
              <div className="flex items-center gap-2 flex-1">
                <Sparkline
                  points={ph.points}
                  delta_pct={ph.delta_pct}
                  width={90}
                  height={24}
                />
                {ph.delta_pct != null && Math.abs(ph.delta_pct) >= 0.5 && (
                  <span
                    className={`inline-flex items-center gap-0.5 text-[11px] ${
                      ph.delta_pct > 0 ? "text-destructive" : "text-success"
                    }`}
                  >
                    {ph.delta_pct > 0 ? (
                      <TrendingUp className="h-3 w-3" />
                    ) : (
                      <TrendingDown className="h-3 w-3" />
                    )}
                    {ph.delta_pct > 0 ? "+" : ""}
                    {ph.delta_pct.toFixed(1)}%
                  </span>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Ручной override матча по URL (2026-05-29): матчер ошибся → пользователь
// вставляет ссылку на правильный товар сайта. swap_alternative на бэкенде:
// старый товар сайта отвязывается (+rejection), новый привязывается, is_manual.
function RelinkPanel({ row }: { row: ComparisonRow }) {
  const t = useTranslations("comparison");
  const queryClient = useQueryClient();
  const [msg, setMsg] = useState<{ site: string; ok: boolean; text: string } | null>(null);

  const mutation = useMutation({
    mutationFn: ({ site, url }: { site: string; url: string }) =>
      api.matchRelink(row.canonical_id, site, url),
    onSuccess: (res, vars) => {
      setMsg({ site: vars.site, ok: true, text: t("relink_ok", { name: res.name }) });
      queryClient.invalidateQueries({ queryKey: ["comparison"] });
      queryClient.invalidateQueries({ queryKey: ["match-quality"] });
    },
    onError: (err: unknown, vars) => {
      const text = err instanceof ApiError && err.message ? err.message : t("relink_error");
      setMsg({ site: vars.site, ok: false, text });
    },
  });

  const relink = (site: string, url: string) => {
    if (url.trim()) mutation.mutate({ site, url: url.trim() });
  };

  return (
    <div className="mt-3 pt-3 border-t border-border/50">
      <p className="text-xs font-medium text-foreground mb-0.5">{t("relink_title")}</p>
      <p className="text-[11px] text-muted-foreground mb-2">{t("relink_hint")}</p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {SITES.map((s) => (
          <RelinkSite
            key={s}
            row={row}
            site={s}
            onRelink={relink}
            busy={mutation.isPending && mutation.variables?.site === s}
            msg={msg?.site === s ? msg : null}
          />
        ))}
      </div>
    </div>
  );
}

function RelinkSite({
  row,
  site,
  onRelink,
  busy,
  msg,
}: {
  row: ComparisonRow;
  site: string;
  onRelink: (site: string, url: string) => void;
  busy: boolean;
  msg: { ok: boolean; text: string } | null;
}) {
  const t = useTranslations("comparison");
  const [val, setVal] = useState("");
  const [showAlts, setShowAlts] = useState(false);
  const cur = row.prices[site];
  const altsQ = useQuery({
    queryKey: ["alternatives", row.canonical_id, site],
    queryFn: () => api.matchAlternatives(row.canonical_id, site),
    enabled: showAlts,
    staleTime: 60_000,
  });

  return (
    <div className="flex flex-col gap-1 rounded-md border border-border/60 p-2">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground font-medium">
        {site}
      </span>
      {cur ? (
        <a
          href={cur.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[10px] text-muted-foreground/70 truncate hover:underline"
          title={cur.url}
        >
          {t("relink_current")}: {cur.url.split("/").filter(Boolean).pop()}
        </a>
      ) : (
        <span className="text-[10px] text-muted-foreground/60">{t("relink_no_product")}</span>
      )}

      {/* «сначала — другие варианты» */}
      <button
        onClick={() => setShowAlts((v) => !v)}
        className="text-[11px] text-primary hover:underline self-start"
        data-testid={`alts-toggle-${site}-${row.canonical_id}`}
      >
        {showAlts ? t("alts_hide") : t("alts_show")}
      </button>
      {showAlts && (
        <div className="flex flex-col gap-1">
          {altsQ.isLoading && <span className="text-[10px] text-muted-foreground">…</span>}
          {altsQ.data && altsQ.data.items.length === 0 && (
            <span className="text-[10px] text-muted-foreground">{t("alts_none")}</span>
          )}
          {altsQ.data?.items.map((a) => (
            <div
              key={a.product_id}
              className="flex items-center justify-between gap-1 rounded bg-background/60 px-1.5 py-1"
            >
              <div className="min-w-0 flex-1">
                <a
                  href={a.url ?? "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[11px] hover:underline truncate block"
                  title={a.name}
                >
                  {a.name}
                </a>
                <span className="text-[9px] text-muted-foreground tabular-nums">
                  {a.price != null ? `${a.price.toFixed(2)} ₼ · ` : ""}
                  {a.score}%
                </span>
              </div>
              <button
                onClick={() => a.url && onRelink(site, a.url)}
                disabled={busy || !a.url}
                className="px-1.5 py-0.5 text-[10px] rounded bg-primary text-primary-foreground disabled:opacity-40 whitespace-nowrap"
                data-testid={`alts-pick-${a.product_id}`}
              >
                {t("alts_pick")}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* «или свой — ссылка» */}
      <div className="flex gap-1 mt-0.5">
        <input
          type="url"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          placeholder={t("relink_placeholder")}
          className="flex-1 min-w-0 px-2 py-1 text-xs border border-border rounded bg-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid={`relink-input-${site}-${row.canonical_id}`}
        />
        <button
          onClick={() => {
            onRelink(site, val);
            setVal("");
          }}
          disabled={busy || !val.trim()}
          className="px-2 py-1 text-xs rounded bg-secondary text-secondary-foreground disabled:opacity-40 whitespace-nowrap"
          data-testid={`relink-apply-${site}-${row.canonical_id}`}
        >
          {busy ? "…" : t("relink_apply")}
        </button>
      </div>
      {msg && (
        <span className={`text-[10px] ${msg.ok ? "text-success" : "text-destructive"}`}>
          {msg.text}
        </span>
      )}
    </div>
  );
}

/** Кликабельный заголовок-сортировщик для таблицы сравнения (как в category-comparison). */
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
    <th className={`px-3 py-2 font-medium ${align === "right" ? "text-right" : "text-left"}`}>
      <button
        onClick={onClick}
        className={`inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-1 hover:bg-muted hover:text-foreground ${
          align === "right" ? "flex-row-reverse" : ""
        } ${isActive ? "text-foreground" : "text-muted-foreground"}`}
      >
        {label}
        {isActive ? (
          active.dir === "asc" ? (
            <ChevronUp className="h-3 w-3" />
          ) : (
            <ChevronDown className="h-3 w-3" />
          )
        ) : (
          <ChevronsUpDown className="h-3.5 w-3.5 opacity-60" />
        )}
      </button>
    </th>
  );
}
