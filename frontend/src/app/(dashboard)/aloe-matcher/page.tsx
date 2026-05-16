"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { CheckCircle2, ExternalLink, Link2, Search, SkipForward, XCircle } from "lucide-react";
import { api, type AnchorProduct, type SiteProduct, type UnmatchedPair } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice } from "@/lib/utils";

const SITE_TO_LINK = "aloe";
const PAGE_LIMIT = 25;

export default function AloeMatcherPage() {
  const [category, setCategory] = useState("");
  const [offset, setOffset] = useState(0);
  const [skipped, setSkipped] = useState<Set<number>>(new Set());

  // Reset pagination when filter changes
  useFilterReset(`${category}`, () => setOffset(0));

  const facetsQ = useQuery({
    queryKey: ["aloe-matcher", "categories"],
    queryFn: () => api.siteProductsFacets("pharmonline"),
  });

  const unmatchedQ = useQuery({
    queryKey: ["aloe-matcher", "unmatched", category, offset],
    queryFn: () =>
      api.unmatchedPairs({
        site: SITE_TO_LINK,
        category: category || undefined,
        limit: PAGE_LIMIT,
        offset,
      }),
  });

  const items = useMemo(
    () => (unmatchedQ.data?.items ?? []).filter((r) => !skipped.has(r.match_id)),
    [unmatchedQ.data, skipped],
  );

  function handleSkip(matchId: number) {
    setSkipped((s) => new Set(s).add(matchId));
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Link2 className="h-6 w-6 text-primary" />
            Ручной матчер: aloe → существующие кластеры
          </h1>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Категория-за-категорией: слева anchor-продукты pharmonline и
            aptekonline, справа поиск по aloe. Один клик «Привязать» — aloe
            добавляется в существующий Match. Эти связки помечены{" "}
            <code className="text-xs rounded bg-muted/50 px-1 py-0.5">is_manual</code>{" "}
            и не пересчитываются авто-матчером.
          </p>
        </div>
        <div className="text-xs text-muted-foreground">
          {unmatchedQ.data ? (
            <>
              <span className="font-mono tabular-nums">
                {Math.max(0, unmatchedQ.data.total - skipped.size)}
              </span>{" "}
              кластеров без aloe
            </>
          ) : (
            "—"
          )}
        </div>
      </header>

      <div className="flex flex-col md:flex-row gap-2">
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm md:w-72"
        >
          <option value="">Все категории</option>
          {facetsQ.data?.categories.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} ({c.count})
            </option>
          ))}
        </select>
      </div>

      {unmatchedQ.isLoading && (
        <div className="text-sm text-muted-foreground py-6 text-center">Загрузка…</div>
      )}
      {unmatchedQ.error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          Ошибка загрузки
        </div>
      )}

      <div className="space-y-3">
        {items.map((pair) => (
          <PairCard key={pair.match_id} pair={pair} onSkip={handleSkip} />
        ))}
        {!unmatchedQ.isLoading && items.length === 0 && (
          <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            Нет кластеров без aloe в этом срезе.
          </div>
        )}
      </div>

      <Pagination
        offset={offset}
        limit={PAGE_LIMIT}
        total={unmatchedQ.data?.total ?? 0}
        onChange={setOffset}
      />
    </div>
  );
}

function PairCard({
  pair,
  onSkip,
}: {
  pair: UnmatchedPair;
  onSkip: (matchId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState(
    pair.canonical_brand ? `${pair.canonical_brand} ${pair.canonical_name}` : pair.canonical_name,
  );
  const debouncedSearch = useDebounce(search, 300);
  const [linkedProductId, setLinkedProductId] = useState<number | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);

  const candidatesQ = useQuery({
    queryKey: ["aloe-matcher", "candidates", pair.match_id, debouncedSearch],
    queryFn: () =>
      api.siteProducts({
        site: "aloe",
        search: debouncedSearch || undefined,
        limit: 10,
      }),
    enabled: Boolean(debouncedSearch),
  });

  const linkMutation = useMutation({
    mutationFn: ({ matchId, productId }: { matchId: number; productId: number }) =>
      api.matchAddProduct(matchId, productId),
    onMutate: ({ productId }) => {
      setLinkError(null);
      setLinkedProductId(productId);
    },
    onError: (err: unknown, _vars) => {
      setLinkedProductId(null);
      const msg = err instanceof Error ? err.message : "Ошибка";
      setLinkError(msg);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aloe-matcher", "unmatched"] });
      queryClient.invalidateQueries({ queryKey: ["match-quality"] });
      queryClient.invalidateQueries({ queryKey: ["normalize-stats"] });
      queryClient.invalidateQueries({ queryKey: ["comparison"] });
    },
  });

  const isLinked = linkedProductId !== null;

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-sm">{pair.canonical_name}</div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {pair.canonical_brand && (
              <span className="mr-2">бренд: {pair.canonical_brand}</span>
            )}
            {pair.canonical_dosage && (
              <span className="mr-2">{pair.canonical_dosage}</span>
            )}
            {pair.canonical_pack_size && <span>{pair.canonical_pack_size}</span>}
          </div>
        </div>
        <button
          onClick={() => onSkip(pair.match_id)}
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 shrink-0"
          title="Скрыть этот кластер в текущей сессии (не запоминается между загрузками)"
        >
          <SkipForward className="h-3.5 w-3.5" />
          Пропустить
        </button>
      </div>

      <div className="grid md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-border">
        <div className="p-4 space-y-2">
          <div className="text-xs uppercase tracking-wide text-muted-foreground mb-2">
            Anchor продукты
          </div>
          {pair.anchor_products.map((a) => (
            <AnchorRow key={a.product_id} anchor={a} />
          ))}
        </div>

        <div className="p-4 space-y-2">
          <div className="text-xs uppercase tracking-wide text-muted-foreground mb-2">
            Поиск по aloe.az
          </div>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Имя или бренд"
              className="w-full rounded-md border border-input bg-background pl-8 pr-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          {candidatesQ.isLoading && (
            <div className="text-xs text-muted-foreground">Ищу…</div>
          )}
          {candidatesQ.data && candidatesQ.data.items.length === 0 && (
            <div className="text-xs text-muted-foreground">
              Ничего не найдено. Попробуйте другие слова или пропустите.
            </div>
          )}

          <ul className="space-y-1.5 max-h-80 overflow-y-auto">
            {candidatesQ.data?.items.map((p) => (
              <CandidateRow
                key={p.id}
                product={p}
                disabled={isLinked || linkMutation.isPending}
                isLinkedHere={linkedProductId === p.id}
                onLink={() =>
                  linkMutation.mutate({ matchId: pair.match_id, productId: p.id })
                }
              />
            ))}
          </ul>

          {linkError && (
            <div className="text-xs text-destructive flex items-center gap-1">
              <XCircle className="h-3.5 w-3.5" /> {linkError}
            </div>
          )}
          {isLinked && !linkError && (
            <div className="text-xs text-success flex items-center gap-1">
              <CheckCircle2 className="h-3.5 w-3.5" /> Привязано — кластер обновится при следующей загрузке.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function AnchorRow({ anchor }: { anchor: AnchorProduct }) {
  return (
    <div className="rounded-md border border-border bg-background/50 p-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm">
            <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mr-1.5">
              {anchor.site}
            </span>
            <a
              href={anchor.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline"
            >
              {anchor.name}
            </a>
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {anchor.brand ?? "—"}
            {anchor.category && (
              <span className="ml-2 text-muted-foreground/70">{anchor.category}</span>
            )}
          </div>
        </div>
        <div className="text-right shrink-0 text-sm font-medium tabular-nums">
          {formatPrice(anchor.price)}
        </div>
      </div>
    </div>
  );
}

function CandidateRow({
  product,
  disabled,
  isLinkedHere,
  onLink,
}: {
  product: SiteProduct;
  disabled: boolean;
  isLinkedHere: boolean;
  onLink: () => void;
}) {
  return (
    <li className="rounded-md border border-border bg-background/50 p-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm flex items-center gap-1">
            <a
              href={product.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline truncate"
            >
              {product.name}
            </a>
            <ExternalLink className="h-3 w-3 text-muted-foreground shrink-0" />
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {product.brand ?? "—"}
            {product.category && (
              <span className="ml-2 text-muted-foreground/70">{product.category}</span>
            )}
          </div>
        </div>
        <div className="text-right shrink-0 flex flex-col items-end gap-1">
          <div className="text-sm font-medium tabular-nums">
            {formatPrice(product.effective_price)}
          </div>
          <button
            onClick={onLink}
            disabled={disabled}
            className={`text-xs rounded px-2 py-1 font-medium transition-colors ${
              isLinkedHere
                ? "bg-success text-success-foreground"
                : "bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed"
            }`}
          >
            {isLinkedHere ? "✓ Привязано" : "Привязать"}
          </button>
        </div>
      </div>
    </li>
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
    <div className="flex items-center justify-between gap-2 text-sm">
      <div className="text-muted-foreground tabular-nums">
        {offset + 1}–{Math.min(offset + limit, total)} из {total}
      </div>
      <div className="flex gap-2">
        <button
          disabled={!canPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="rounded-md border border-input px-3 py-1.5 disabled:opacity-40 hover:bg-muted/50"
        >
          ← Назад
        </button>
        <div className="px-3 py-1.5 tabular-nums text-muted-foreground">
          стр. {page}/{totalPages}
        </div>
        <button
          disabled={!canNext}
          onClick={() => onChange(offset + limit)}
          className="rounded-md border border-input px-3 py-1.5 disabled:opacity-40 hover:bg-muted/50"
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
