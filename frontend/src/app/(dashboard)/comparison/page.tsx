"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { X } from "lucide-react";
import { api, type ComparisonRow } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatPct } from "@/lib/utils";

const SITES = ["pharmonline", "aptekonline", "aloe"] as const;

export default function ComparisonPage() {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [minSites, setMinSites] = useState(2);
  const [diffOnly, setDiffOnly] = useState(true);
  const [withAloe, setWithAloe] = useState(false);
  const queryClient = useQueryClient();

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["comparison", debouncedSearch, minSites],
    queryFn: () => api.comparison({ search: debouncedSearch, min_sites: minSites }),
  });

  // Client-side filters поверх серверного результата
  const filtered = useMemo(() => {
    if (!data) return data;
    return data.filter((r) => {
      if (diffOnly && (!r.spread_pct || r.spread_pct < 0.5)) return false;
      if (withAloe && !r.prices["aloe"]) return false;
      return true;
    });
  }, [data, diffOnly, withAloe]);

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
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Сравнение цен</h1>
        <p className="text-sm text-muted-foreground">
          Cross-site matched товары. Зелёным — самая низкая цена, красным — самая высокая.
        </p>
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row md:flex-wrap gap-2">
        <input
          type="search"
          placeholder="🔎 Поиск (например: Friso, Nestle, Nutrilak)"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
        <label
          className="inline-flex items-center gap-2 text-sm px-3 py-2 rounded-md border border-input bg-background cursor-pointer select-none"
          title="Только товары где spread > 0.5% (есть реальная разница между сайтами)"
        >
          <input
            type="checkbox"
            checked={diffOnly}
            onChange={(e) => setDiffOnly(e.target.checked)}
            className="h-4 w-4"
          />
          Только различия
        </label>
        <label
          className="inline-flex items-center gap-2 text-sm px-3 py-2 rounded-md border border-input bg-background cursor-pointer select-none"
          title="Только товары которые есть в каталоге aloe.az"
        >
          <input
            type="checkbox"
            checked={withAloe}
            onChange={(e) => setWithAloe(e.target.checked)}
            className="h-4 w-4"
          />
          Только с aloe
        </label>
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
      {filtered && filtered.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center" data-testid="empty">
          {data && data.length > 0
            ? `По фильтрам ничего не найдено (всего матчей: ${data.length}). Снимите галочки выше.`
            : "По текущему фильтру ничего не найдено."}
        </div>
      )}

      {/* Mobile: card list */}
      <div className="md:hidden space-y-2" data-testid="mobile-list">
        {filtered?.map((row) => (
          <ComparisonCard key={row.canonical_id} row={row} onReject={handleReject} />
        ))}
      </div>

      {/* Desktop: table — Бренд column dropped (был дубликатом первого слова имени) */}
      <div className="hidden md:block rounded-lg border border-border overflow-hidden" data-testid="desktop-table">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">Название</th>
              {SITES.map((s) => (
                <th key={s} className="px-3 py-2 text-right">
                  {s}
                </th>
              ))}
              <th className="px-3 py-2 text-right">Spread</th>
              <th className="px-3 py-2 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {filtered?.map((row) => (
              <ComparisonRowDesktop
                key={row.canonical_id}
                row={row}
                onReject={handleReject}
              />
            ))}
          </tbody>
        </table>
      </div>

      {filtered && filtered.length > 0 && (
        <div className="text-xs text-muted-foreground text-center" data-testid="result-count">
          {filtered.length} матчей
          {data && filtered.length !== data.length && (
            <span className="text-muted-foreground/60"> из {data.length}</span>
          )}
        </div>
      )}
    </div>
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
      className={`inline-flex items-center gap-1 tabular-nums hover:underline ${
        isMin ? "text-success font-semibold" : isMax ? "text-destructive" : ""
      }`}
      title={p.is_on_sale ? "Со скидкой" : undefined}
    >
      {formatPrice(p.price)}
      {p.is_on_sale && (
        <span className="text-[9px] rounded bg-warning/15 text-warning px-1 py-0.5 font-semibold uppercase">
          sale
        </span>
      )}
    </a>
  );
}

function ComparisonRowDesktop({
  row,
  onReject,
}: {
  row: ComparisonRow;
  onReject: (r: ComparisonRow) => void;
}) {
  return (
    <tr className="border-t border-border hover:bg-muted/30 group">
      <td className="px-3 py-2 max-w-md truncate">
        <div>{row.name}</div>
        {row.brand && (
          <div className="text-[11px] text-muted-foreground/70 truncate">
            {row.brand}
          </div>
        )}
      </td>
      {SITES.map((s) => (
        <td key={s} className="px-3 py-2 text-right">
          {priceCell(row, s)}
        </td>
      ))}
      <td className="px-3 py-2 text-right tabular-nums">{formatPct(row.spread_pct)}</td>
      <td className="px-3 py-2">
        <button
          onClick={() => onReject(row)}
          title="Отвергнуть как false match"
          className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
          data-testid={`reject-${row.canonical_id}`}
        >
          <X className="h-4 w-4" />
        </button>
      </td>
    </tr>
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
      <div className="font-medium text-sm pr-6">{row.name}</div>
      <div className="text-xs text-muted-foreground mt-0.5">
        {row.brand ?? "—"} · {row.sites_with_price} сайтов · spread {formatPct(row.spread_pct)}
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
