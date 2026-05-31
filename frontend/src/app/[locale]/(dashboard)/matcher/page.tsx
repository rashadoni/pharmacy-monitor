"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import { useMemo, useState, useEffect } from "react";
import {
  Building2,
  CheckCircle2,
  ExternalLink,
  Leaf,
  Link2,
  Pill,
  Search,
  SkipForward,
  Sparkles,
  XCircle,
} from "lucide-react";
import {
  api,
  friendlyError,
  type AnchorProduct,
  type CandidateAnalog,
  type SiteProduct,
  type UnmatchedPair,
} from "@/lib/api";
import { OnboardingTip } from "@/components/onboarding-tip";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice } from "@/lib/utils";
import { CreateFromScratch } from "./create-from-scratch";
import { SITES, SITE_LABEL, type Site } from "./sites";

const SITE_ICON: Record<Site, typeof Search> = {
  aloe: Leaf,
  pharmonline: Building2,
  aptekonline: Pill,
};

const PAGE_LIMIT = 25;

function isSite(v: string | null): v is Site {
  return v != null && (SITES as readonly string[]).includes(v);
}

function isMode(v: string | null): v is "attach" | "create" {
  return v === "attach" || v === "create";
}

export default function MatcherPage() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const siteParam = sp.get("site");
  const modeParam = sp.get("mode");
  const site: Site = isSite(siteParam) ? siteParam : "aloe";
  const mode: "attach" | "create" = isMode(modeParam) ? modeParam : "attach";
  const category = sp.get("category") ?? "";

  // Счётчики unmatched-кластеров per-site — для бейджей в site selector
  const countsQ = useQuery({
    queryKey: ["matcher", "counts"],
    queryFn: () => api.matcherCounts(),
    staleTime: 30_000,
  });

  function updateParams(patch: Record<string, string | null>) {
    const q = new URLSearchParams(sp.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") q.delete(k);
      else q.set(k, v);
    }
    router.replace(`${pathname}?${q.toString()}`);
  }

  return (
    <div className="space-y-6">
      <OnboardingTip
        id="matcher-overview-v1"
        title="Когда нужен ручной матчер"
        description={
          <>
            Auto-matcher на name+brand similarity отрабатывает 99%
            случаев. Сюда заходишь когда: товар точно есть на 2-3 сайтах,
            но названия настолько разные что матчер не справился. Один
            клик «Привязать» — создаётся manual Match (is_manual=True),
            авто-матчер его больше не трогает.
          </>
        }
      />

      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Link2 className="h-6 w-6 text-primary" />
            Ручной матчер
          </h1>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Привяжите продукт сайта <strong>{SITE_LABEL[site]}</strong> к
            существующему cross-site кластеру, или создайте новый кластер
            «с нуля» из 2–3 продуктов разных сайтов. Ручные связки помечаются{" "}
            <code className="text-xs rounded bg-muted/50 px-1 py-0.5">
              is_manual
            </code>{" "}
            и не пересчитываются авто-матчером.
          </p>
        </div>
      </header>

      {/* Mode tabs */}
      <div className="flex items-center gap-1 border-b border-border">
        <ModeTab
          active={mode === "attach"}
          onClick={() => updateParams({ mode: "attach" })}
          icon={Link2}
        >
          Привязать к существующему
        </ModeTab>
        <ModeTab
          active={mode === "create"}
          onClick={() => updateParams({ mode: "create" })}
          icon={Sparkles}
        >
          Создать кластер с нуля
        </ModeTab>
      </div>

      {/* Site selector (только для Attach режима — для Create режима все 3 сайта используются вместе) */}
      {mode === "attach" && (
        <div className="flex flex-col sm:flex-row sm:items-center gap-3">
          <span className="text-xs uppercase tracking-wide text-muted-foreground font-semibold">
            Сайт
          </span>
          <div className="inline-flex rounded-md border border-input bg-background p-0.5 gap-0.5">
            {SITES.map((s) => {
              const Icon = SITE_ICON[s];
              const active = site === s;
              const count = countsQ.data?.[s];
              return (
                <button
                  key={s}
                  onClick={() => updateParams({ site: s })}
                  className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-sm transition-colors ${
                    active
                      ? "bg-primary text-primary-foreground font-medium"
                      : "text-foreground hover:bg-muted/50"
                  }`}
                >
                  <Icon className="h-4 w-4" />
                  {s}
                  {count != null && (
                    <span
                      className={`text-[10px] tabular-nums font-mono ${
                        active
                          ? "text-primary-foreground/80"
                          : "text-muted-foreground"
                      }`}
                    >
                      ({count})
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {mode === "attach" ? (
        <AttachMode
          site={site}
          category={category}
          setCategory={(c) => updateParams({ category: c || null })}
        />
      ) : (
        <CreateFromScratch />
      )}
    </div>
  );
}

function ModeTab({
  active,
  onClick,
  icon: Icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof Search;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
        active
          ? "border-primary text-foreground font-medium"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      <Icon className="h-4 w-4" />
      {children}
    </button>
  );
}

// ─── Attach mode ───────────────────────────────────────────────────────────

function AttachMode({
  site,
  category,
  setCategory,
}: {
  site: Site;
  category: string;
  setCategory: (c: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  // Skipped per-site чтобы переключение сайта не показывало чужие пропущенные
  const [skippedBySite, setSkippedBySite] = useState<Record<Site, Set<number>>>(
    () => ({ aloe: new Set(), pharmonline: new Set(), aptekonline: new Set() }),
  );
  const skipped = skippedBySite[site];

  // Reset pagination when filter/site changes
  useFilterReset(`${site}|${category}`, () => setOffset(0));

  // Берём фасеты с одного из 2 «не-целевых» сайтов — категории у pharmonline/aptekonline богаче
  const facetsSite = site === "aloe" ? "pharmonline" : "aloe";

  const facetsQ = useQuery({
    queryKey: ["matcher", "categories", facetsSite],
    queryFn: () => api.siteProductsFacets(facetsSite),
  });

  const unmatchedQ = useQuery({
    queryKey: ["matcher", "unmatched", site, category, offset],
    queryFn: () =>
      api.unmatchedPairs({
        site,
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
    setSkippedBySite((prev) => {
      const next = { ...prev };
      next[site] = new Set(prev[site]).add(matchId);
      return next;
    });
  }

  function handleSkipAllOnPage() {
    if (!unmatchedQ.data) return;
    const visibleIds = unmatchedQ.data.items
      .filter((r) => !skipped.has(r.match_id))
      .map((r) => r.match_id);
    if (visibleIds.length === 0) return;
    if (
      !confirm(
        `Пропустить все ${visibleIds.length} кластеров на этой странице?\n(можно потом сбросить кнопкой «Сброс»)`,
      )
    )
      return;
    setSkippedBySite((prev) => {
      const next = { ...prev };
      const s = new Set(prev[site]);
      visibleIds.forEach((id) => s.add(id));
      next[site] = s;
      return next;
    });
  }

  function handleSkipCheap(threshold: number) {
    if (!unmatchedQ.data) return;
    const cheapIds = unmatchedQ.data.items
      .filter((r) => !skipped.has(r.match_id))
      .filter((r) => {
        const prices = r.anchor_products
          .map((a) => a.price)
          .filter((p): p is number => p != null);
        if (prices.length === 0) return false;
        return Math.max(...prices) < threshold;
      })
      .map((r) => r.match_id);
    if (cheapIds.length === 0) {
      alert(`Нет товаров дешевле ${threshold} AZN на этой странице`);
      return;
    }
    if (!confirm(`Пропустить ${cheapIds.length} кластеров с ценой < ${threshold} AZN?`))
      return;
    setSkippedBySite((prev) => {
      const next = { ...prev };
      const s = new Set(prev[site]);
      cheapIds.forEach((id) => s.add(id));
      next[site] = s;
      return next;
    });
  }

  function handleResetSkipped() {
    setSkippedBySite((prev) => ({ ...prev, [site]: new Set() }));
  }

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div className="flex flex-col sm:flex-row sm:flex-wrap gap-2 flex-1">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="rounded-md border border-input bg-background px-3 py-2 text-sm md:w-72"
          >
            <option value="">Все категории</option>
            {facetsQ.data?.categories.map((c) => (
              <option key={c.name} value={c.name}>
                {c.label && c.label !== c.name ? c.label : c.name} ({c.count})
              </option>
            ))}
          </select>
          <button
            onClick={() => handleSkipCheap(5)}
            className="rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title="Скрыть кластеры с ценой < 5 AZN (мелочь — не приоритет)"
          >
            Пропустить дешёвые (&lt; 5 ₼)
          </button>
          <button
            onClick={handleSkipAllOnPage}
            className="rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title="Скрыть всю текущую страницу"
          >
            Пропустить страницу
          </button>
          {skipped.size > 0 && (
            <button
              onClick={handleResetSkipped}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-muted/50 text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title="Вернуть всех пропущенных в видимость"
            >
              Сброс ({skipped.size})
            </button>
          )}
        </div>
        <div className="text-xs text-muted-foreground">
          {unmatchedQ.data ? (
            <>
              <span className="font-mono tabular-nums">
                {Math.max(0, unmatchedQ.data.total - skipped.size)}
              </span>{" "}
              кластеров без {site}
            </>
          ) : (
            "—"
          )}
        </div>
      </div>

      {unmatchedQ.isLoading && (
        <div className="text-sm text-muted-foreground py-6 text-center">
          Загрузка…
        </div>
      )}
      {unmatchedQ.error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          Ошибка загрузки
        </div>
      )}

      <div className="space-y-3">
        {items.map((pair) => (
          <PairCard
            key={pair.match_id}
            pair={pair}
            site={site}
            onSkip={handleSkip}
          />
        ))}
        {!unmatchedQ.isLoading && items.length === 0 && (
          <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            Нет кластеров без {site} в этом срезе. Попробуйте другую категорию
            или сайт.
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
  site,
  onSkip,
}: {
  pair: UnmatchedPair;
  site: Site;
  onSkip: (matchId: number) => void;
}) {
  const queryClient = useQueryClient();
  // Авто-search: 2-3 первых слова имени (часто это бренд+название без дозировок).
  const [search, setSearch] = useState(() => {
    const tokens = (pair.canonical_name || "").split(/\s+/).filter(Boolean);
    return tokens.slice(0, 2).join(" ") || pair.canonical_brand || "";
  });
  const debouncedSearch = useDebounce(search, 300);
  const [linkedProductId, setLinkedProductId] = useState<number | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);

  // При смене site сбрасываем поиск (новая первая 2 слова с того же anchor)
  useEffect(() => {
    setLinkedProductId(null);
    setLinkError(null);
  }, [site]);

  const candidatesQ = useQuery({
    queryKey: ["matcher", "candidates", site, pair.match_id, debouncedSearch],
    queryFn: () =>
      api.siteProducts({
        site,
        search: debouncedSearch || undefined,
        limit: 10,
      }),
    enabled: Boolean(debouncedSearch),
  });

  // Авто-подсказки: guard-passing аналоги, ранжированные матчером (auto_safe = ultra-равны)
  const suggestionsQ = useQuery({
    queryKey: ["matcher", "analogs", site, pair.match_id],
    queryFn: () => api.matchCandidateAnalogs(pair.match_id, site),
  });

  const linkMutation = useMutation({
    mutationFn: ({
      matchId,
      productId,
    }: {
      matchId: number;
      productId: number;
    }) => api.matchAddProduct(matchId, productId),
    onMutate: ({ productId }) => {
      setLinkError(null);
      setLinkedProductId(productId);
    },
    onError: (err: unknown) => {
      setLinkedProductId(null);
      setLinkError(friendlyError(err));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["matcher", "unmatched"] });
      queryClient.invalidateQueries({ queryKey: ["matcher", "analogs"] });
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
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 shrink-0 rounded px-1 py-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
          {suggestionsQ.data && suggestionsQ.data.items.length > 0 && (
            <div className="mb-3">
              <div className="text-xs uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1">
                <Sparkles className="h-3.5 w-3.5" /> Предложенные аналоги
              </div>
              <ul className="space-y-1.5">
                {suggestionsQ.data.items.map((p) => (
                  <SuggestionRow
                    key={p.product_id}
                    cand={p}
                    disabled={isLinked || linkMutation.isPending}
                    isLinkedHere={linkedProductId === p.product_id}
                    onLink={() =>
                      linkMutation.mutate({
                        matchId: pair.match_id,
                        productId: p.product_id,
                      })
                    }
                  />
                ))}
              </ul>
            </div>
          )}

          <div className="text-xs uppercase tracking-wide text-muted-foreground mb-2">
            Поиск по {SITE_LABEL[site]}
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
                  linkMutation.mutate({
                    matchId: pair.match_id,
                    productId: p.id,
                  })
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
              <CheckCircle2 className="h-3.5 w-3.5" /> Привязано — кластер
              обновится при следующей загрузке.
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
              <span className="ml-2 text-muted-foreground/70">
                {anchor.category}
              </span>
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
              <span className="ml-2 text-muted-foreground/70">
                {product.category}
              </span>
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

function SuggestionRow({
  cand,
  disabled,
  isLinkedHere,
  onLink,
}: {
  cand: CandidateAnalog;
  disabled: boolean;
  isLinkedHere: boolean;
  onLink: () => void;
}) {
  return (
    <li
      className={`rounded-md border p-2 ${
        cand.auto_safe
          ? "border-success/40 bg-success/5"
          : "border-border bg-background/50"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm flex items-center gap-1">
            <a
              href={cand.url ?? "#"}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline truncate"
            >
              {cand.name}
            </a>
            <ExternalLink className="h-3 w-3 text-muted-foreground shrink-0" />
          </div>
          <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-1.5">
            {cand.auto_safe && (
              <span className="inline-flex items-center gap-0.5 text-success font-medium">
                <CheckCircle2 className="h-3 w-3" /> точное совпадение
              </span>
            )}
            <span className="tabular-nums">{cand.score}%</span>
            {cand.brand && <span className="truncate">{cand.brand}</span>}
          </div>
        </div>
        <div className="text-right shrink-0 flex flex-col items-end gap-1">
          <div className="text-sm font-medium tabular-nums">
            {formatPrice(cand.price)}
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
