"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useMemo } from "react";
import { X, ChevronUp, ChevronDown, TrendingUp, TrendingDown } from "lucide-react";
import { api, type ComparisonRow } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatPct } from "@/lib/utils";
import { OnboardingTip } from "@/components/onboarding-tip";
import { Sparkline } from "@/components/sparkline";
import { TableSkeleton } from "@/components/skeleton";

const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type SiteName = typeof SITES[number];

export default function ComparisonPage() {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [minSites, setMinSites] = useState(2);
  const [diffOnly, setDiffOnly] = useState(false);
  const [withAloe, setWithAloe] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const queryClient = useQueryClient();

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["comparison", debouncedSearch, minSites],
    queryFn: () => api.comparison({ search: debouncedSearch, min_sites: minSites }),
  });

  // Client-side filters + default sort (P1.2 PO Audit 2026-05-17)
  // По умолчанию: сверху строки с наибольшим |spread_pct| — это самое полезное
  // для PO (где конкурент бьёт по цене / где мы можем поднять).
  const filtered = useMemo(() => {
    if (!data) return data;
    const filtered = data.filter((r) => {
      if (diffOnly && (!r.spread_pct || r.spread_pct < 0.5)) return false;
      if (withAloe && !r.prices["aloe"]) return false;
      return true;
    });
    return [...filtered].sort(
      (a, b) => Math.abs(b.spread_pct ?? 0) - Math.abs(a.spread_pct ?? 0),
    );
  }, [data, diffOnly, withAloe]);

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
        "comparison", debouncedSearch, minSites,
      ]);
      queryClient.setQueryData<ComparisonRow[]>(
        ["comparison", debouncedSearch, minSites],
        (old) => old?.filter((r) => r.canonical_id !== id),
      );
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      // Rollback
      if (ctx?.prev) {
        queryClient.setQueryData(
          ["comparison", debouncedSearch, minSites],
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
    if (!confirm(`Отвергнуть как false match?\n\n${row.name}\n(brand: ${row.brand ?? "—"})\n\nПары будут добавлены в match_rejections, matcher не предложит их снова.`)) {
      return;
    }
    rejectMutation.mutate(row.canonical_id);
  }

  return (
    <div className="space-y-4">
      <OnboardingTip
        id="comparison-arrows-v1"
        title="Как читать таблицу"
        description={
          <>
            Зелёный <span className="text-success">▼</span> = pharmonline
            (client) дешевле всех. Красный <span className="text-destructive">▲</span>{" "}
            = конкурент дешевле, надо реагировать. Клик на ▼/▲ кнопке справа от
            spread — открывает тренд 30 дней. Кнопка «⬇ CSV» вверху —
            экспорт отфильтрованного.
          </>
        }
      />

      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Сравнение цен</h1>
          <p className="text-sm text-muted-foreground">
            Cross-site matched товары. Зелёным — самая низкая цена, красным — самая высокая.
            Сортировка по |spread| desc.
          </p>
        </div>
        <button
          onClick={handleExportCsv}
          disabled={!filtered || filtered.length === 0}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          title="Скачать CSV отфильтрованного списка"
        >
          ⬇ CSV
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-2">
        <input
          type="search"
          placeholder="🔎 Поиск (например: Friso, Nestle, Nutrilak)"
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
          <option value={3}>3 сайта</option>
          <option value={2}>≥ 2 сайтов (default)</option>
          <option value={1}>Все</option>
        </select>
      </div>

      {/* Status */}
      {(isLoading || isFetching) && (
        <div className="text-xs text-muted-foreground" data-testid="loading">
          {isLoading ? "Загрузка…" : "Обновление…"}
        </div>
      )}
      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive" data-testid="error">
          Ошибка загрузки данных
        </div>
      )}
      {data && data.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center" data-testid="empty">
          По текущему фильтру ничего не найдено.
        </div>
      )}

      {/* Mobile: card list */}
      <div className="md:hidden space-y-2" data-testid="mobile-list">
        {data?.map((row) => (
          <ComparisonCard key={row.canonical_id} row={row} onReject={handleReject} />
        ))}
      </div>

      {/* Desktop: table */}
      <div className="hidden md:block rounded-lg border border-border overflow-hidden" data-testid="desktop-table">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">Название</th>
              {visibleSites.map((s) => (
                <th key={s} className="px-3 py-2 text-right">
                  {s}
                </th>
              ))}
              <th className="px-3 py-2 text-right">Spread</th>
              <th className="px-3 py-2 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {data?.map((row) => (
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

      {data && data.length > 0 && (
        <div className="text-xs text-muted-foreground text-center" data-testid="result-count">
          {data.length} матчей
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
  return (
    <span
      className={`inline-flex items-center gap-0.5 tabular-nums ${color}`}
      title={
        clientCheapest
          ? `pharmonline дешевле остальных на ${abs.toFixed(1)}%`
          : `${row.cheapest_site} дешевле pharmonline на ${abs.toFixed(1)}%`
      }
    >
      <Icon />
      {abs.toFixed(1)}%
    </span>
  );
}

function priceCell(row: ComparisonRow, site: string) {
  const p = row.prices[site];
  if (!p) return <span className="text-muted-foreground/50">—</span>;
  const isMin = row.min_price === p.price;
  const isMax = row.max_price === p.price && row.min_price !== row.max_price;
  return (
    <a
      href={p.url}
      target="_blank"
      rel="noopener noreferrer"
      className={`inline-block tabular-nums hover:underline ${
        isMin ? "text-success font-semibold" : isMax ? "text-destructive" : ""
      }`}
    >
      {formatPrice(p.price)}
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
  return (
    <>
      <tr className="border-t border-border hover:bg-muted/30 group">
        <td className="px-3 py-2 max-w-md truncate">
          <span className="inline-flex items-center gap-1">
            {row.name}
            {row.needs_review && (
              <span
                className="text-amber-500 text-xs leading-none"
                title="Подозрительный spread ≥50% — проверить матч"
                aria-label="Требует проверки"
              >
                ⚠
              </span>
            )}
            {row.confidence < 0.95 && (
              <span
                className="text-muted-foreground/60 text-[10px] tabular-nums leading-none"
                title={`Уверенность матча: ${Math.round(row.confidence * 100)}%`}
              >
                {Math.round(row.confidence * 100)}%
              </span>
            )}
          </span>
        </td>
        <td className="px-3 py-2 text-muted-foreground">{row.brand ?? "—"}</td>
        {sites.map((s) => (
          <td key={s} className="px-3 py-2 text-right">
            {priceCell(row, s)}
          </td>
        ))}
        <td className="px-3 py-2 text-right tabular-nums">
          <SpreadCell row={row} />
        </td>
        <td className="px-3 py-2">
          <button
            onClick={onToggleExpand}
            title={expanded ? "Скрыть тренд" : "Показать тренд за 30 дней"}
            aria-label={expanded ? "Скрыть тренд" : "Показать тренд за 30 дней"}
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
            title="Отвергнуть как false match"
            aria-label="Отвергнуть как false match"
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
  return (
    <div className="rounded-lg border border-border bg-card p-3 relative">
      <button
        onClick={() => onReject(row)}
        className="absolute top-2 right-2 text-muted-foreground hover:text-destructive p-1"
        title="Отвергнуть"
        data-testid={`reject-mobile-${row.canonical_id}`}
      >
        <X className="h-4 w-4" />
      </button>
      <div className="font-medium text-sm pr-8 inline-flex items-center gap-1 flex-wrap">
        {row.name}
        {row.needs_review && (
          <span
            className="text-amber-500 text-xs leading-none"
            title="Подозрительный spread ≥50% — проверить матч"
            aria-label="Требует проверки"
          >
            ⚠
          </span>
        )}
        {row.confidence < 0.95 && (
          <span
            className="text-muted-foreground/60 text-[10px] tabular-nums leading-none"
            title={`Уверенность матча: ${Math.round(row.confidence * 100)}%`}
          >
            {Math.round(row.confidence * 100)}%
          </span>
        )}
      </div>
      <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-1.5">
        <span>
          {row.brand ?? "—"} · {row.sites_with_price} сайтов · spread
        </span>
        <SpreadCell row={row} />
      </div>
      <div className="grid grid-cols-3 gap-2 mt-3">
        {SITES.map((s) => (
          <div key={s} className="text-center">
            <div className="text-[10px] text-muted-foreground uppercase">{s}</div>
            <div className="mt-0.5">{priceCell(row, s)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TrendPanel({ row }: { row: ComparisonRow }) {
  const sitesWithPrice = SITES.filter((s) => row.prices[s]);
  const queries = useQueries({
    queries: sitesWithPrice.map((s) => ({
      queryKey: ["price-history", row.prices[s].product_id, 30],
      queryFn: () => api.productPriceHistory(row.prices[s].product_id, 30),
      staleTime: 60_000,
    })),
  });

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {sitesWithPrice.map((s, idx) => {
        const q = queries[idx];
        return (
          <div key={s} className="flex items-center gap-2 text-xs">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground w-20 shrink-0">
              {s}
            </span>
            {q.isLoading ? (
              <span className="text-muted-foreground">…</span>
            ) : q.error || !q.data ? (
              <span className="text-muted-foreground/70">нет данных</span>
            ) : q.data.points.filter((p) => p.price != null).length < 2 ? (
              <span className="text-muted-foreground/70 text-[11px]">
                {q.data.current != null
                  ? `стабильно ${formatPrice(q.data.current)}`
                  : "—"}
              </span>
            ) : (
              <div className="flex items-center gap-2 flex-1">
                <Sparkline
                  points={q.data.points}
                  delta_pct={q.data.delta_pct}
                  width={90}
                  height={24}
                />
                {q.data.delta_pct != null && Math.abs(q.data.delta_pct) >= 0.5 && (
                  <span
                    className={`inline-flex items-center gap-0.5 text-[11px] ${
                      q.data.delta_pct > 0 ? "text-destructive" : "text-success"
                    }`}
                  >
                    {q.data.delta_pct > 0 ? (
                      <TrendingUp className="h-3 w-3" />
                    ) : (
                      <TrendingDown className="h-3 w-3" />
                    )}
                    {q.data.delta_pct > 0 ? "+" : ""}
                    {q.data.delta_pct.toFixed(1)}%
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
