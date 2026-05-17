"use client";

import { useQuery } from "@tanstack/react-query";
import { notFound, useParams } from "next/navigation";
import { useState } from "react";
import { Building2, ExternalLink, Leaf, Pill, Search } from "lucide-react";
import { api, type SiteProduct } from "@/lib/api";
import { ActionRow } from "@/components/action-row";
import { KpiCard } from "@/components/kpi-card";
import { Sparkline } from "@/components/sparkline";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice, formatTime } from "@/lib/utils";

const PAGE_LIMIT = 50;

const VALID_SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type SiteName = (typeof VALID_SITES)[number];

const SITE_META: Record<SiteName, { icon: typeof Leaf; iconColor: string; tagline: string }> = {
  pharmonline: {
    icon: Building2,
    iconColor: "text-primary",
    tagline: "Основной клиентский сайт. Каталог, бренды и ROI с перспективы pharmonline.",
  },
  aptekonline: {
    icon: Pill,
    iconColor: "text-warning",
    tagline: "Крупнейший конкурент по ассортименту. Срез с перспективы aptekonline.",
  },
  aloe: {
    icon: Leaf,
    iconColor: "text-success",
    tagline: "Бутиковая аптека. Каталог, бренды и ROI с перспективы aloe.",
  },
};

function competitorsOf(site: SiteName): SiteName[] {
  return VALID_SITES.filter((s) => s !== site);
}

export default function SitePage() {
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
    queryKey: ["site", site, "facets"],
    queryFn: () => api.siteProductsFacets(site),
  });
  const brandsQ = useQuery({
    queryKey: ["site", site, "brands"],
    queryFn: () => api.brandShare({ top_n: 50, site }),
  });
  const roiQ = useQuery({
    queryKey: ["site", site, "roi"],
    queryFn: () => api.roiActions(site),
  });

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Icon className={`h-6 w-6 ${meta.iconColor}`} /> {site}.az
          </h1>
          <p className="text-sm text-muted-foreground">{meta.tagline}</p>
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
          hint={`Нет на ${competitors.join("/")}`}
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
          С точки зрения {site} — где мы дешевле/дороже конкурентов и какие SKU
          есть у {competitors.join("/")}, но нет у нас.
        </p>
        {roiQ.isLoading && (
          <div className="rounded-md border border-border bg-card p-3 text-sm text-muted-foreground flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full bg-primary animate-pulse" />
            Анализ pricing-recommendations… может занять до 15 сек
          </div>
        )}
        {roiQ.error && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive flex items-center justify-between gap-3">
            <div>
              {roiQ.error instanceof Error
                ? roiQ.error.message
                : "Не удалось загрузить рекомендации"}
            </div>
            <button
              onClick={() => roiQ.refetch()}
              className="rounded border border-destructive/50 px-2 py-1 text-xs hover:bg-destructive/20"
            >
              Повторить
            </button>
          </div>
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
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">Топ брендов на {site}</h2>
        <p className="text-xs text-muted-foreground">
          По количеству SKU. Зелёный значок — эксклюзив только у {site}.
        </p>
      </div>
      {loading && (
        <div className="p-4 text-sm text-muted-foreground">Загрузка…</div>
      )}
      {!loading && brands.length === 0 && (
        <div className="p-4 text-sm text-muted-foreground">Нет данных</div>
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
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">Категории на {site}</h2>
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
      <ul className="divide-y divide-border max-h-96 overflow-y-auto">
        {categories.map((c) => {
          const hasLabel = c.label && c.label !== c.name;
          return (
            <li
              key={c.name}
              className="flex items-center justify-between px-4 py-2 text-sm gap-2"
            >
              <span className="min-w-0 flex-1 truncate">
                {hasLabel ? (
                  <>
                    <span className="font-medium">{c.label}</span>
                    <span className="ml-1.5 text-[10px] font-mono text-muted-foreground/60">
                      {c.name}
                    </span>
                  </>
                ) : (
                  <span className="font-mono text-xs text-muted-foreground">
                    {c.name}
                  </span>
                )}
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
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const [category, setCategory] = useState("");
  const [brand, setBrand] = useState("");
  const [onSaleOnly, setOnSaleOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const filterKey = JSON.stringify({ site, debouncedSearch, category, brand, onSaleOnly });
  useFilterReset(filterKey, () => setOffset(0));

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

  return (
    <section>
      <h2 className="text-lg font-semibold mb-3">Каталог {site}</h2>

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
              {c.label && c.label !== c.name ? c.label : c.name} ({c.count})
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
        <div className="text-sm text-muted-foreground py-6 text-center">
          Загрузка…
        </div>
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
                  <th className="px-3 py-2 text-right">Тренд 30д</th>
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

function ProductRow({ product, site }: { product: SiteProduct; site: SiteName }) {
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
          title={`Открыть на ${site}.az`}
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

function useFilterReset(key: string, onChange: () => void) {
  const [prev, setPrev] = useState(key);
  if (prev !== key) {
    setPrev(key);
    onChange();
  }
}
