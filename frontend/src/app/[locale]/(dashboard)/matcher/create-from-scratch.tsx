"use client";

/**
 * Режим B матчера: создать НОВЫЙ кластер «с нуля».
 *
 * Используется когда auto-matcher вообще не сгруппировал товар (нет cross-site
 * кластера, к которому можно было бы привязать). UI — три search-панели рядом
 * (aloe / pharmonline / aptekonline) и корзина внизу: ≤ 1 продукт на сайт,
 * минимум 2 разных сайта чтобы создать Match.
 *
 * Backend: POST /api/v1/dash/matches/create-with-products валидирует, что среди
 * product_ids все продукты с разных сайтов. Если меньше 2 — 422. Если есть
 * дубль сайта — 409. После создания кластер помечается is_manual=True,
 * match_strategy='manual'.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import {
  Building2,
  CheckCircle2,
  ExternalLink,
  Leaf,
  Pill,
  Plus,
  Search,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import { api, friendlyError, type SiteProduct } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";
import { formatPrice } from "@/lib/utils";
import { SITES, type Site } from "./sites";

const DEFAULT_SEARCH_LIMIT = 15;
const CATEGORY_SCOPED_LIMIT = 100;

const SITE_ICON: Record<Site, typeof Search> = {
  aloe: Leaf,
  pharmonline: Building2,
  aptekonline: Pill,
};

type Selected = Partial<Record<Site, SiteProduct>>;
type CategoryBySite = Partial<Record<Site, string>>;

export function CreateFromScratch({
  categoryBySite = {},
  categoryLabel = "",
}: {
  categoryBySite?: CategoryBySite;
  categoryLabel?: string;
}) {
  const t = useTranslations("matcher");
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Selected>({});
  const [success, setSuccess] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectedCount = Object.values(selected).filter(Boolean).length;
  const canCreate = selectedCount >= 2;

  const createMutation = useMutation({
    mutationFn: (productIds: number[]) =>
      api.matchCreateWithProducts(productIds),
    onMutate: () => {
      setError(null);
      setSuccess(null);
    },
    onError: (err: unknown) => {
      setError(friendlyError(err));
    },
    onSuccess: (data) => {
      setSuccess(data.match_id);
      setSelected({});
      queryClient.invalidateQueries({ queryKey: ["matcher", "unmatched"] });
      queryClient.invalidateQueries({ queryKey: ["match-quality"] });
      queryClient.invalidateQueries({ queryKey: ["normalize-stats"] });
      queryClient.invalidateQueries({ queryKey: ["comparison"] });
    },
  });

  function handleAdd(site: Site, product: SiteProduct) {
    setSelected((prev) => ({ ...prev, [site]: product }));
    setSuccess(null);
    setError(null);
  }

  function handleRemove(site: Site) {
    setSelected((prev) => {
      const next = { ...prev };
      delete next[site];
      return next;
    });
  }

  function handleCreate() {
    const products = Object.values(selected).filter(Boolean) as SiteProduct[];
    if (products.length < 2) return;

    // Дополнительный sanity-check на стороне UI: если бренды у выбранных
    // продуктов различаются — спросим подтверждение (бэк всё равно создаст,
    // но это последний шанс отловить ошибку оператора).
    const brands = new Set(
      products.map((p) => (p.brand || "").trim().toLowerCase()).filter(Boolean),
    );
    if (brands.size > 1) {
      const list = products
        .map((p) => `${p.brand ?? "—"}: ${p.name}`)
        .join("\n");
      if (!confirm(t("confirm_different_brands", { list })))
        return;
    }

    createMutation.mutate(products.map((p) => p.id));
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md bg-muted/30 border border-border p-3 text-xs text-muted-foreground">
        <Sparkles className="inline h-3.5 w-3.5 mr-1 -mt-0.5" />
        {t.rich("create_help", {
          strong: (chunks) => <strong>{chunks}</strong>,
        })}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {SITES.map((s) => (
          <SiteSearchPanel
            key={s}
            site={s}
            category={categoryBySite[s] ?? ""}
            categoryLabel={categoryLabel}
            selectedProduct={selected[s] ?? null}
            onAdd={(p) => handleAdd(s, p)}
          />
        ))}
      </div>

      {/* Sticky basket */}
      <div className="sticky bottom-0 z-10 -mx-4 sm:mx-0 bg-card border-t sm:border border-border sm:rounded-lg p-4 shadow-lg">
        <div className="flex flex-col sm:flex-row sm:items-center gap-3">
          <div className="flex-1 space-y-1.5">
            <div className="text-xs uppercase tracking-wide text-muted-foreground font-semibold">
              {t("basket", { count: selectedCount })}
            </div>
            {selectedCount === 0 && (
              <div className="text-sm text-muted-foreground">
                {t("basket_empty")}
              </div>
            )}
            <div className="flex flex-wrap gap-1.5">
              {SITES.map((s) => {
                const p = selected[s];
                if (!p) return null;
                const Icon = SITE_ICON[s];
                return (
                  <div
                    key={s}
                    className="inline-flex items-center gap-1.5 rounded border border-border bg-background px-2 py-1 text-xs max-w-full"
                  >
                    <Icon className="h-3 w-3 text-muted-foreground shrink-0" />
                    <span className="truncate max-w-[200px]">{p.name}</span>
                    <span className="text-muted-foreground tabular-nums">
                      {formatPrice(p.effective_price)}
                    </span>
                    <button
                      onClick={() => handleRemove(s)}
                      className="text-muted-foreground hover:text-destructive ml-1"
                      title={t("remove_from_basket")}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
          <button
            onClick={handleCreate}
            disabled={!canCreate || createMutation.isPending}
            className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
          >
            {createMutation.isPending
              ? t("creating")
              : canCreate
                ? t("create_cluster_count", { count: selectedCount })
                : t("need_different_sites")}
          </button>
        </div>

        {error && (
          <div className="mt-2 text-xs text-destructive flex items-center gap-1">
            <XCircle className="h-3.5 w-3.5" /> {error}
          </div>
        )}
        {success != null && (
          <div className="mt-2 text-xs text-success flex items-center gap-1">
            <CheckCircle2 className="h-3.5 w-3.5" />{" "}
            {t.rich("cluster_created", {
              id: success,
              link: (chunks) => <a href="/comparison" className="underline">{chunks}</a>,
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function SiteSearchPanel({
  site,
  category,
  categoryLabel,
  selectedProduct,
  onAdd,
}: {
  site: Site;
  category: string;
  categoryLabel: string;
  selectedProduct: SiteProduct | null;
  onAdd: (product: SiteProduct) => void;
}) {
  const t = useTranslations("matcher");
  const Icon = SITE_ICON[site];
  const [search, setSearch] = useState("");
  const debounced = useDebounce(search, 300);
  const limit = category ? CATEGORY_SCOPED_LIMIT : DEFAULT_SEARCH_LIMIT;

  const productsQ = useQuery({
    queryKey: ["matcher", "create-search", site, category, debounced, limit],
    queryFn: () =>
      api.siteProducts({
        site,
        category: category || undefined,
        search: debounced || undefined,
        limit,
      }),
    enabled: Boolean(category) || debounced.length >= 2,
  });

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden flex flex-col min-h-[400px]">
      <div className="px-3 py-2 border-b border-border flex items-center gap-2 bg-muted/30">
        <Icon className="h-4 w-4 text-muted-foreground" />
        <span className="text-sm font-medium">{site}.az</span>
        {selectedProduct && (
          <span className="ml-auto text-[10px] text-success font-medium uppercase tracking-wide">
            {t("selected")}
          </span>
        )}
      </div>
      <div className="p-3 space-y-2 flex-1 flex flex-col">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("create_search_placeholder")}
            className="w-full rounded-md border border-input bg-background pl-8 pr-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        {category && (
          <div className="text-[11px] text-primary bg-primary/10 rounded px-2 py-1">
            {t("create_category_scope", {
              category: categoryLabel || category,
              slug: category,
            })}
          </div>
        )}

        {!category && debounced.length < 2 && (
          <div className="text-xs text-muted-foreground py-2">
            {t("enter_name_or_brand")}
          </div>
        )}
        {productsQ.isLoading && (
          <div className="text-xs text-muted-foreground py-2">{t("searching")}</div>
        )}
        {productsQ.data && productsQ.data.items.length === 0 && (
          <div className="text-xs text-muted-foreground py-2">
            {t("nothing_found_short")}
          </div>
        )}
        {category && productsQ.data && productsQ.data.items.length > 0 && (
          <div className="text-[11px] text-muted-foreground">
            {t("create_category_results", {
              shown: productsQ.data.items.length,
              total: productsQ.data.total,
            })}
          </div>
        )}

        <ul className="space-y-1.5 flex-1 overflow-y-auto max-h-72">
          {productsQ.data?.items.map((p) => {
            const isAlreadySelected = selectedProduct?.id === p.id;
            return (
              <li
                key={p.id}
                className={`rounded-md border p-2 ${
                  isAlreadySelected
                    ? "border-success bg-success/5"
                    : "border-border bg-background/50"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-xs flex items-center gap-1">
                      <a
                        href={p.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="hover:underline truncate"
                      >
                        {p.name}
                      </a>
                      <ExternalLink className="h-3 w-3 text-muted-foreground shrink-0" />
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5">
                      {p.brand ?? "—"}
                    </div>
                  </div>
                  <div className="text-right shrink-0 flex flex-col items-end gap-1">
                    <div className="text-xs font-medium tabular-nums">
                      {formatPrice(p.effective_price)}
                    </div>
                    <button
                      onClick={() => onAdd(p)}
                      className={`text-[10px] rounded px-1.5 py-0.5 font-medium transition-colors ${
                        isAlreadySelected
                          ? "bg-success/20 text-success cursor-default"
                          : "bg-primary text-primary-foreground hover:bg-primary/90"
                      }`}
                    >
                      {isAlreadySelected ? (
                        t("selected_plain")
                      ) : (
                        <>
                          <Plus className="inline h-2.5 w-2.5 -mt-0.5" />{" "}
                          {t("add")}
                        </>
                      )}
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
