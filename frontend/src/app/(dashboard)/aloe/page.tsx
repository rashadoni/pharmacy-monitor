"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ExternalLink, Leaf, Search } from "lucide-react";
import { api, type SiteProduct } from "@/lib/api";
import { ActionRow } from "@/components/action-row";
import { KpiCard } from "@/components/kpi-card";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatTime } from "@/lib/utils";

const SITE = "aloe";
const PAGE_LIMIT = 50;

export default function AloePage() {
  const summaryQ = useQuery({
    queryKey: ["aloe", "summary"],
    queryFn: () => api.siteProductsSummary(SITE),
  });
  const facetsQ = useQuery({
    queryKey: ["aloe", "facets"],
    queryFn: () => api.siteProductsFacets(SITE),
  });
  const brandsQ = useQuery({
    queryKey: ["aloe", "brands"],
    queryFn: () => api.brandShare({ top_n: 50, site: SITE }),
  });
  const roiQ = useQuery({
    queryKey: ["aloe", "roi"],
    queryFn: () => api.roiActions(SITE),
  });

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Leaf className="h-6 w-6 text-success" /> aloe.az
          </h1>
          <p className="text-sm text-muted-foreground">
            Срез по бутиковой аптеке. Каталог, бренды и ROI с перспективы aloe.
          </p>
        </div>
        <div className="text-xs text-muted-foreground">
          {summaryQ.data?.last_run_at ? (
            <>
              Последний прогон:{" "}
              <span className="font-mono">
                {formatTime(summaryQ.data.last_run_at)}
              </span>
              {summaryQ.data.last_run_id != null && (
                <> — run #{summaryQ.data.last_run_id}</>
              )}
            </>
          ) : (
            "Нет данных о прогонах"
          )}
        </div>
      </header>

      <section className="grid gap-4 grid-cols-2 md:grid-cols-4">
        <KpiCard
          label="Всего продуктов"
          value={summaryQ.data?.total_products ?? "—"}
          loading={summaryQ.isLoading}
        />
        <KpiCard
          label="Брендов"
          value={summaryQ.data?.total_brands ?? "—"}
          loading={summaryQ.isLoading}
        />
        <KpiCard
          label="Эксклюзивных брендов"
          value={summaryQ.data?.exclusive_brands ?? "—"}
          loading={summaryQ.isLoading}
          hint="Нет на pharmonline/aptekonline"
        />
        <KpiCard
          label="Со скидкой"
          value={
            summaryQ.data
              ? `${summaryQ.data.on_sale_count} (${summaryQ.data.on_sale_pct.toFixed(1)}%)`
              : "—"
          }
          loading={summaryQ.isLoading}
        />
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">ROI рекомендации</h2>
        <p className="text-xs text-muted-foreground mb-3">
          С точки зрения aloe — где мы дешевле/дороже конкурентов и какие SKU
          есть у pharmonline/aptekonline, но нет у нас.
        </p>
        {roiQ.isLoading && (
          <div className="text-sm text-muted-foreground">Загрузка…</div>
        )}
        {roiQ.data && roiQ.data.length === 0 && (
          <div className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
            Нет рекомендаций — pricing на уровне.
          </div>
        )}
        <div className="space-y-2">
          {roiQ.data?.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <BrandsPanel brands={brandsQ.data ?? []} loading={brandsQ.isLoading} />
        <CategoriesPanel
          categories={facetsQ.data?.categories ?? []}
          loading={facetsQ.isLoading}
        />
      </section>

      <ProductsSection facets={facetsQ.data} />
    </div>
  );
}

function BrandsPanel({
  brands,
  loading,
}: {
  brands: { brand: string; counts: Record<string, number>; exclusive_to: string | null }[];
  loading: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">Топ брендов на aloe</h2>
        <p className="text-xs text-muted-foreground">
          По количеству SKU. Зелёный значок — эксклюзив только у aloe.
        </p>
      </div>
      {loading && (
        <div className="p-4 text-sm text-muted-foreground">Загрузка…</div>
      )}
      {!loading && brands.length === 0 && (
        <div className="p-4 text-sm text-muted-foreground">Нет данных</div>
      )}
      <ul className="divide-y divide-border max-h-96 overflow-y-auto">
        {brands.map((b) => (
          <li
            key={b.brand}
            className="flex items-center justify-between px-4 py-2 text-sm"
          >
            <span className="font-medium truncate" title={b.brand}>
              {b.brand}
            </span>
            <span className="flex items-center gap-2 shrink-0">
              {b.exclusive_to === SITE && (
                <span className="rounded bg-success/10 text-success text-[10px] px-1.5 py-0.5 font-semibold uppercase">
                  exclusive
                </span>
              )}
              <span className="font-mono tabular-nums text-muted-foreground">
                {b.counts[SITE] ?? 0}
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
}: {
  categories: { name: string; count: number }[];
  loading: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">Категории на aloe</h2>
        <p className="text-xs text-muted-foreground">
          Размер каталога по разделам.
        </p>
      </div>
      {loading && (
        <div className="p-4 text-sm text-muted-foreground">Загрузка…</div>
      )}
      {!loading && categories.length === 0 && (
        <div className="p-4 text-sm text-muted-foreground">Нет данных</div>
      )}
      <ul className="divide-y divide-border">
        {categories.map((c) => (
          <li
            key={c.name}
            className="flex items-center justify-between px-4 py-2 text-sm"
          >
            <span className="font-mono text-xs text-muted-foreground truncate">
              {c.name}
            </span>
            <span className="font-mono tabular-nums">{c.count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProductsSection({
  facets,
}: {
  facets: { categories: { name: string; count: number }[]; brands: { name: string; count: number }[] } | undefined;
}) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [category, setCategory] = useState("");
  const [brand, setBrand] = useState("");
  const [onSaleOnly, setOnSaleOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  // Reset pagination when filters change
  const filterKey = JSON.stringify({ debouncedSearch, category, brand, onSaleOnly });
  const prevFilterKey = useFilterReset(filterKey, () => setOffset(0));

  const productsQ = useQuery({
    queryKey: ["aloe", "products", debouncedSearch, category, brand, onSaleOnly, offset],
    queryFn: () =>
      api.siteProducts({
        site: SITE,
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

  return (
    <section>
      <h2 className="text-lg font-semibold mb-3">Каталог aloe</h2>

      <div className="flex flex-col md:flex-row gap-2 mb-3">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="search"
            placeholder="Поиск по названию или бренду"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-md border border-input bg-background pl-8 pr-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          <option value="">Все категории</option>
          {facets?.categories.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} ({c.count})
            </option>
          ))}
        </select>
        <select
          value={brand}
          onChange={(e) => setBrand(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          <option value="">Все бренды</option>
          {facets?.brands.slice(0, 50).map((b) => (
            <option key={b.name} value={b.name}>
              {b.name} ({b.count})
            </option>
          ))}
        </select>
        <label className="inline-flex items-center gap-2 text-sm px-3 py-2 rounded-md border border-input bg-background cursor-pointer">
          <input
            type="checkbox"
            checked={onSaleOnly}
            onChange={(e) => setOnSaleOnly(e.target.checked)}
            className="h-4 w-4"
          />
          Скидка
        </label>
      </div>

      {productsQ.isLoading && (
        <div className="text-sm text-muted-foreground py-6 text-center">Загрузка…</div>
      )}
      {productsQ.error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          Ошибка загрузки данных
        </div>
      )}

      {!productsQ.isLoading && !productsQ.error && (
        <>
          <div className="hidden md:block rounded-lg border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-3 py-2 text-left">Название</th>
                  <th className="px-3 py-2 text-left">Бренд</th>
                  <th className="px-3 py-2 text-left">Категория</th>
                  <th className="px-3 py-2 text-right">Цена</th>
                  <th className="px-3 py-2 text-right">Скидка</th>
                  <th className="px-3 py-2 w-8"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <ProductRow key={p.id} product={p} />
                ))}
                {items.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                      Ничего не найдено
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
                Ничего не найдено
              </div>
            )}
          </div>

          <Pagination
            offset={offset}
            limit={PAGE_LIMIT}
            total={total}
            onChange={setOffset}
          />
        </>
      )}
    </section>
  );
}

function ProductRow({ product }: { product: SiteProduct }) {
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
            {formatPrice(product.price)}
          </span>
        ) : (
          formatPrice(product.price)
        )}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {product.is_on_sale && product.discount_price != null ? (
          <span className="text-success font-medium">
            {formatPrice(product.discount_price)}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2">
        <a
          href={product.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-muted-foreground hover:text-foreground"
          title="Открыть на aloe.az"
        >
          <ExternalLink className="h-4 w-4" />
        </a>
      </td>
    </tr>
  );
}

function ProductCard({ product }: { product: SiteProduct }) {
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
                {formatPrice(product.discount_price)}
              </div>
              <div className="text-xs text-muted-foreground line-through">
                {formatPrice(product.price)}
              </div>
            </>
          ) : (
            <div className="text-sm font-semibold">{formatPrice(product.price)}</div>
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
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  if (total === 0) return null;
  return (
    <div className="flex items-center justify-between gap-2 mt-3 text-sm">
      <div className="text-muted-foreground tabular-nums">
        {offset + 1}–{Math.min(offset + limit, total)} из {total}
      </div>
      <div className="flex gap-2">
        <button
          disabled={!canPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="rounded-md border border-input px-3 py-1.5 text-sm disabled:opacity-40 hover:bg-muted/50"
        >
          ← Назад
        </button>
        <div className="px-3 py-1.5 text-sm tabular-nums text-muted-foreground">
          стр. {page}/{totalPages}
        </div>
        <button
          disabled={!canNext}
          onClick={() => onChange(offset + limit)}
          className="rounded-md border border-input px-3 py-1.5 text-sm disabled:opacity-40 hover:bg-muted/50"
        >
          Вперёд →
        </button>
      </div>
    </div>
  );
}

function useFilterReset(currentKey: string, onChange: () => void) {
  const [prev, setPrev] = useState(currentKey);
  if (prev !== currentKey) {
    setPrev(currentKey);
    onChange();
  }
  return prev;
}
