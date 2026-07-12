"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Lightbulb, Pencil, Play, Plus, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { api, friendlyError, type CategoryRow, type CategorySuggestion } from "@/lib/api";
import { OnboardingTip } from "@/components/onboarding-tip";
import { formatNumber, formatTime } from "@/lib/utils";
import { isTerminalRunStatus, scrapeResultTextClass } from "@/lib/run-quality";
import { QueryErrorState } from "@/components/query-error-state";
import { useRouter } from "@/i18n/navigation";
import { choiceParam, integerParam, queryWithPatch } from "@/lib/filter-query";

const CATEGORY_SITES = ["pharmonline", "aptekonline", "aloe"] as const;
type CategorySite = (typeof CATEGORY_SITES)[number];

/**
 * Извлечь slug категории из URL для каждого сайта.
 *
 * Поддерживаемые форматы:
 * - pharmonline: `https://pharmonline.az/products?category=ushaq-qidasi`
 * - aptekonline: `https://www.aptekonline.az/shop/productList?categoryId[]=252&lang=az`
 * - aloe: `https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1`
 *
 * Если не parsится — возвращает исходный input (клиент мог ввести голый slug).
 */
function extractSlug(site: "pharmonline" | "aptekonline" | "aloe", input: string): string {
  const trimmed = input.trim();
  if (!trimmed) return "";
  // Если не похоже на URL — это уже slug
  if (!trimmed.startsWith("http")) return trimmed;
  try {
    const u = new URL(trimmed);
    if (site === "pharmonline") {
      return u.searchParams.get("category") ?? trimmed;
    }
    if (site === "aptekonline") {
      // categoryId[]=252 — array param
      const v = u.searchParams.get("categoryId[]") ?? u.searchParams.get("categoryId");
      if (v) return v;
      // Path-style: /products/292 или /category/N
      const m = u.pathname.match(/\/(?:products|category)\/(\d+)/);
      if (m) return m[1];
      return trimmed;
    }
    if (site === "aloe") {
      const v = u.searchParams.get("category_slug");
      return v ? decodeURIComponent(v) : trimmed;
    }
  } catch {
    return trimmed;
  }
  return trimmed;
}

/**
 * Слаг из label: «Витамины» → `vitaminy`.
 * Простой ASCII-only fallback; кириллица транслитерируется по таблице.
 */
function slugify(s: string): string {
  const map: Record<string, string> = {
    а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "yo", ж: "zh", з: "z",
    и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
    с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh", щ: "sch",
    ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
  };
  return s
    .toLowerCase()
    .split("")
    .map((ch) => map[ch] ?? ch)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export default function CategoriesPage() {
  const t = useTranslations("categories");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryRef = useRef(searchParams.toString());
  useEffect(() => {
    queryRef.current = searchParams.toString();
  }, [searchParams]);
  const parsedParams = new URLSearchParams(searchParams.toString());
  const urlSearch = searchParams.get("q") ?? "";
  const [search, setSearch] = useState(urlSearch);
  useEffect(() => setSearch(urlSearch), [urlSearch]);
  const siteFilter = choiceParam(
    parsedParams,
    "site",
    ["", ...CATEGORY_SITES] as const,
    "",
  );
  const activeOnly = searchParams.get("active") === "1";
  const crossFilter = choiceParam(
    parsedParams,
    "coverage",
    ["", "cross2", "cross3"] as const,
    "",
  );
  const view = choiceParam(
    parsedParams,
    "view",
    ["list", "suggestions"] as const,
    "list",
  );
  const siteA = choiceParam(parsedParams, "site_a", CATEGORY_SITES, "pharmonline");
  const requestedSiteB = choiceParam(parsedParams, "site_b", CATEGORY_SITES, "aptekonline");
  const siteB = requestedSiteB === siteA
    ? CATEGORY_SITES.find((candidate) => candidate !== siteA) ?? "aptekonline"
    : requestedSiteB;
  const minOverlap = integerParam(parsedParams, "overlap", 5, { min: 2, max: 50 });
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<CategoryRow | null>(null);
  const queryClient = useQueryClient();

  function updateFilters(
    patch: Record<string, string | number | boolean | null>,
    history: "push" | "replace" = "push",
  ) {
    const query = queryWithPatch(queryRef.current, patch);
    queryRef.current = query;
    const href = query ? `/categories?${query}` : "/categories";
    router[history](href, { scroll: false });
  }

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["categories"],
    queryFn: api.categories,
  });

  const filtered = (data ?? []).filter((c) => {
    if (search && !`${c.label_ru} ${c.label_az ?? ""} ${c.key}`.toLowerCase().includes(search.toLowerCase())) {
      return false;
    }
    if (siteFilter === "pharmonline" && !c.pharmonline_slug) return false;
    if (siteFilter === "aptekonline" && !c.aptekonline_slug) return false;
    if (siteFilter === "aloe" && !c.aloe_slug) return false;
    if (activeOnly && !c.is_active) return false;
    if (crossFilter === "cross2" && !(c.pharmonline_slug && c.aptekonline_slug)) return false;
    if (crossFilter === "cross3" && !(c.pharmonline_slug && c.aptekonline_slug && c.aloe_slug)) return false;
    return true;
  });

  // Категория «cross-2» — у неё есть pharm+apt-slug'и (двусторонний кейс).
  // «cross-3» — pharm+apt+aloe (полный треугольник, редко).
  const stats = {
    total: data?.length ?? 0,
    active: data?.filter((c) => c.is_active).length ?? 0,
    cross2: data?.filter((c) => c.pharmonline_slug && c.aptekonline_slug).length ?? 0,
    cross3: data?.filter((c) => c.pharmonline_slug && c.aptekonline_slug && c.aloe_slug).length ?? 0,
    pharmonline: data?.filter((c) => c.pharmonline_slug).length ?? 0,
    aptekonline: data?.filter((c) => c.aptekonline_slug).length ?? 0,
    aloe: data?.filter((c) => c.aloe_slug).length ?? 0,
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("subtitle")}
          </p>
        </div>
        <div className="flex gap-2">
          <TriggerScrapeButton />
          <button
            onClick={() => setShowAdd(true)}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 md:min-h-9"
          >
            <Plus className="h-4 w-4" />
            {t("add_button")}
          </button>
        </div>
      </div>

      {/* P1.1 Tabs: Список / Suggested mappings */}
      <div className="flex items-center gap-1 border-b border-border">
        <button
          onClick={() => updateFilters({ view: null })}
          aria-pressed={view === "list"}
          className={`-mb-px inline-flex min-h-11 items-center gap-2 border-b-2 px-4 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            view === "list"
              ? "border-primary text-foreground font-medium"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          {t("view_list_tab", { total: stats.total })}
        </button>
        <button
          onClick={() => updateFilters({ view: "suggestions" })}
          aria-pressed={view === "suggestions"}
          className={`-mb-px inline-flex min-h-11 items-center gap-2 border-b-2 px-4 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            view === "suggestions"
              ? "border-primary text-foreground font-medium"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          <Lightbulb className="h-3.5 w-3.5" />
          {t("view_suggestions_tab")}
        </button>
      </div>

      {view === "suggestions" ? (
        <SuggestionsPanel
          siteA={siteA}
          siteB={siteB}
          minOverlap={minOverlap}
          onUpdate={updateFilters}
        />
      ) : (
      <>
      {showAdd && <CategoryForm onClose={() => setShowAdd(false)} />}
      {editing && (
        <CategoryForm
          editing={editing}
          onClose={() => setEditing(null)}
        />
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-7 gap-3">
        <Stat label={t("stat_total")} value={stats.total} />
        <Stat label={t("stat_active")} value={stats.active} />
        <Stat label={t("stat_cross2")} value={stats.cross2} highlight />
        <Stat label={t("stat_cross3")} value={stats.cross3} highlight />
        <Stat label="pharmonline" value={stats.pharmonline} />
        <Stat label="aptekonline" value={stats.aptekonline} />
        <Stat label="aloe" value={stats.aloe} />
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-2">
        <input
          type="search"
          placeholder={t("search_placeholder")}
          aria-label={t("search_label")}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            updateFilters({ q: e.target.value || null }, "replace");
          }}
          className="min-h-11 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        />
        <select
          value={siteFilter}
          onChange={(e) => updateFilters({ site: e.target.value || null })}
          aria-label={t("site_filter_label")}
          className="min-h-11 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        >
          <option value="">{t("filter_all")}</option>
          <option value="pharmonline">pharmonline</option>
          <option value="aptekonline">aptekonline</option>
          <option value="aloe">aloe</option>
        </select>
        <select
          value={crossFilter}
          onChange={(e) => updateFilters({ coverage: e.target.value || null })}
          aria-label={t("coverage_filter_label")}
          className="min-h-11 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        >
          <option value="">{t("filter_any_coverage")}</option>
          <option value="cross2">{t("filter_cross2")}</option>
          <option value="cross3">{t("filter_cross3")}</option>
        </select>
        <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-3 text-sm hover:bg-muted/50 md:min-h-9">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => updateFilters({ active: e.target.checked })}
            className="rounded"
          />
          {t("only_active")}
        </label>
      </div>

      {isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}
      {isError && (
        <QueryErrorState
          message={friendlyError(error, locale)}
          retryLabel={tCommon("retry")}
          onRetry={() => refetch()}
        />
      )}

      {/* Mobile-fix 2026-05-28: было overflow-hidden → 7-колонная таблица
          обрезалась на 375px. overflow-x-auto + min-w на table — пользователь
          горизонтально пролистывает на телефоне. */}
      <div className="rounded-lg border border-border overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">{t("th_key")}</th>
              <th className="px-3 py-2 text-left">{t("th_label")}</th>
              <th className="px-3 py-2 text-left">pharmonline</th>
              <th className="px-3 py-2 text-left">aptekonline</th>
              <th className="px-3 py-2 text-left">aloe</th>
              <th className="px-3 py-2 text-center">{t("th_active")}</th>
              <th className="px-3 py-2 text-center"></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((cat) => (
              <CategoryRowDesktop
                key={cat.id}
                cat={cat}
                onEdit={() => setEditing(cat)}
                onChanged={() =>
                  queryClient.invalidateQueries({ queryKey: ["categories"] })
                }
              />
            ))}
          </tbody>
        </table>
      </div>

      {filtered.length === 0 && !isLoading && !isError && (
        <div className="text-muted-foreground text-center py-4">
          {t("empty")}
        </div>
      )}
      </>
      )}
    </div>
  );
}

function SuggestionsPanel({
  siteA,
  siteB,
  minOverlap,
  onUpdate,
}: {
  siteA: CategorySite;
  siteB: CategorySite;
  minOverlap: number;
  onUpdate: (patch: Record<string, string | number | boolean | null>) => void;
}) {
  const t = useTranslations("categories");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const queryClient = useQueryClient();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["category-suggestions", siteA, siteB, minOverlap],
    queryFn: () => api.categorySuggestions({ site_a: siteA, site_b: siteB, min_overlap: minOverlap }),
    enabled: siteA !== siteB,
  });

  const mapMutation = useMutation({
    mutationFn: (payload: {
      site_a: CategorySite;
      site_a_slug: string;
      site_b: CategorySite;
      site_b_slug: string;
    }) => api.categoryMappingCreate(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["category-suggestions"] });
      queryClient.invalidateQueries({ queryKey: ["categories"] });
    },
    onError: (e) => alert(friendlyError(e, locale)),
  });

  const notMapped = (data ?? []).filter((s) => !s.already_mapped);
  const mapped = (data ?? []).filter((s) => s.already_mapped);

  return (
    <div className="space-y-4">
      <OnboardingTip
        id="categories-suggestions-v1"
        title={t("suggestions_tip_title")}
        description={t("suggestions_tip_desc")}
      />
      <div className="rounded-md bg-muted/30 border border-border p-3 text-xs text-muted-foreground">
        <Lightbulb className="inline h-3.5 w-3.5 mr-1 -mt-0.5" />
        {t("suggestions_explainer")}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <SiteSelector
          value={siteA}
          onChange={(value) => onUpdate({ site_a: value === "pharmonline" ? null : value })}
          label={t("site_a_label")}
          disabled={siteB}
        />
        <span className="text-muted-foreground">↔</span>
        <SiteSelector
          value={siteB}
          onChange={(value) => onUpdate({ site_b: value === "aptekonline" ? null : value })}
          label={t("site_b_label")}
          disabled={siteA}
        />
        <label className="ml-auto inline-flex min-h-11 items-center gap-2 text-sm">
          {t("min_overlap_label")}
          <input
            type="number"
            value={minOverlap}
            min={2}
            max={50}
            onChange={(e) => {
              const value = Math.min(50, Math.max(2, Number(e.target.value) || 5));
              onUpdate({ overlap: value === 5 ? null : value });
            }}
            className="min-h-11 w-20 rounded-md border border-input bg-background px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
      </div>

      {isLoading && <div className="text-sm text-muted-foreground py-4">{t("searching_overlaps")}</div>}
      {error && (
        <QueryErrorState
          message={friendlyError(error, locale)}
          retryLabel={tCommon("retry")}
          onRetry={() => refetch()}
        />
      )}
      {!isLoading && data && notMapped.length === 0 && mapped.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {t("no_overlaps", { n: minOverlap })}
        </div>
      )}

      {notMapped.length > 0 && (
        <div className="rounded-lg border border-border bg-card">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-sm font-semibold">
              {t("suggestions_new_title", { count: notMapped.length })}
            </h2>
            <p className="text-xs text-muted-foreground">
              {t("suggestions_new_desc")}
            </p>
          </div>
          {/* Mobile-fix 2026-05-28: 5-колонная таблица overflows на 375px */}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="bg-muted/30 text-muted-foreground text-xs">
                <tr>
                <th className="px-4 py-2 text-left">{siteA}</th>
                <th className="px-4 py-2 text-left">{siteB}</th>
                <th className="px-4 py-2 text-right">{t("suggestions_shared_brands_th")}</th>
                <th className="px-4 py-2 text-left">{t("suggestions_examples_th")}</th>
                <th className="px-4 py-2 w-32"></th>
              </tr>
            </thead>
            <tbody>
              {notMapped.map((s) => (
                <tr
                  key={`${s.site_a_slug}|${s.site_b_slug}`}
                  className="border-t border-border"
                >
                  <td className="px-4 py-2 font-mono text-xs">
                    {s.site_a_slug}
                    <div className="text-[10px] text-muted-foreground/70">
                      {t("suggestions_products_count", { count: s.site_a_products })}
                    </div>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {s.site_b_slug}
                    <div className="text-[10px] text-muted-foreground/70">
                      {t("suggestions_products_count", { count: s.site_b_products })}
                    </div>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums font-semibold">
                    {s.shared_brands_count}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {s.sample_brands.slice(0, 4).join(", ")}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() =>
                        mapMutation.mutate({
                          site_a: siteA,
                          site_a_slug: s.site_a_slug,
                          site_b: siteB,
                          site_b_slug: s.site_b_slug,
                        })
                      }
                      disabled={mapMutation.isPending}
                      className="inline-flex items-center gap-1 rounded bg-primary text-primary-foreground px-2 py-1 text-xs font-medium hover:bg-primary/90 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {t("bind_btn")}
                    </button>
                  </td>
                </tr>
              ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {mapped.length > 0 && (
        <details className="rounded-lg border border-border bg-muted/20">
          <summary className="px-4 py-2 cursor-pointer text-sm text-muted-foreground">
            {t("suggestions_mapped_title", { count: mapped.length })}
          </summary>
          {/* Mobile-fix 2026-05-28: mapped table overflows */}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-xs">
            <tbody>
              {mapped.map((s) => (
                <tr key={`m-${s.site_a_slug}|${s.site_b_slug}`} className="border-t border-border">
                  <td className="px-4 py-2 font-mono text-muted-foreground">
                    {s.site_a_slug}
                  </td>
                  <td className="px-4 py-2 font-mono text-muted-foreground">
                    {s.site_b_slug}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">{s.shared_brands_count}</td>
                  <td className="px-4 py-2 text-success">
                    <CheckCircle2 className="inline h-3.5 w-3.5 mr-1" /> {t("already_mapped_badge")}
                  </td>
                </tr>
              ))}
            </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

function SiteSelector({
  value,
  onChange,
  label,
  disabled,
}: {
  value: CategorySite;
  onChange: (v: CategorySite) => void;
  label: string;
  disabled: string; // имя другого сайта, который нельзя выбрать (запрет site_a == site_b)
}) {
  return (
    <label className="inline-flex min-h-11 items-center gap-2 text-sm">
      <span className="text-xs text-muted-foreground uppercase">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as typeof value)}
        className="min-h-11 rounded-md border border-input bg-background px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {CATEGORY_SITES.map((s) => (
          <option key={s} value={s} disabled={s === disabled}>
            {s}
          </option>
        ))}
      </select>
    </label>
  );
}

function TriggerScrapeButton({ categoryId }: { categoryId?: number } = {}) {
  const t = useTranslations("categories");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const requestsQ = useQuery({
    queryKey: ["scrape-requests"],
    queryFn: () => api.scrapeRequests(5),
    refetchInterval: (q) => {
      const data = q.state.data;
      const hasActive = data?.some((r) => r.status === "pending" || r.status === "running");
      return hasActive ? 5_000 : 30_000;
    },
  });
  const triggerMut = useMutation({
    mutationFn: () =>
      api.scrapeTrigger(
        categoryId
          ? { mode: "category", category_id: categoryId }
          : { mode: "all" }
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scrape-requests"] }),
    onError: (e: Error) => alert(friendlyError(e, locale)),
  });

  // Активный = running ИЛИ pending. Может быть несколько (до 5 в очереди).
  const activeAll = requestsQ.data?.find(
    (r) => (r.status === "pending" || r.status === "running") && r.mode === "all"
  );
  const activeThis = categoryId
    ? requestsQ.data?.find(
        (r) =>
          (r.status === "pending" || r.status === "running") &&
          r.mode === "category" &&
          r.category_id === categoryId,
      )
    : undefined;
  const lastCompleted = !categoryId
    ? requestsQ.data?.find((r) => isTerminalRunStatus(r.status))
    : undefined;
  const completedFreshlyMs = lastCompleted?.completed_at
    ? Date.now() - new Date(lastCompleted.completed_at).getTime()
    : Infinity;
  const showCompleted = lastCompleted && completedFreshlyMs < 10 * 60 * 1000;

  // === Per-row кнопка ====================================================
  if (categoryId) {
    if (activeThis) {
      // Эта же категория уже в очереди — показываем статус
      return (
        <span className="inline-flex items-center gap-1 text-xs text-warning whitespace-nowrap">
          <Play className="h-3 w-3 animate-pulse" />
          {activeThis.status === "pending" ? t("scan_in_queue") : t("scanning")} #{activeThis.id}
        </span>
      );
    }
    const blockedByAll = !!activeAll;
    return (
      <button
        onClick={() => triggerMut.mutate()}
        disabled={triggerMut.isPending || blockedByAll}
        className="inline-flex items-center gap-1 rounded border border-border bg-background px-2 py-1 text-xs font-medium hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
        title={
          blockedByAll
            ? t("scan_category_blocked_title", { id: activeAll.id })
            : t("scan_category_title")
        }
      >
        <Play className="h-3 w-3" />
        {t("scan_per_cat_btn")}
      </button>
    );
  }

  // === Глобальная кнопка (mode=all) ======================================
  if (activeAll) {
    return (
      <div className="inline-flex items-center gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
        <Play className="h-4 w-4 animate-pulse" />
        <span className="font-medium">
          {activeAll.status === "pending" ? t("scanning_queued") : t("scanning_all")}
        </span>
        <span className="text-xs text-muted-foreground">#{activeAll.id}</span>
      </div>
    );
  }
  return (
    <div className="inline-flex flex-col items-end gap-1">
      <button
        onClick={() => triggerMut.mutate()}
        disabled={triggerMut.isPending}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-2 text-sm font-medium hover:bg-secondary disabled:opacity-50"
        title={t("scan_all_title")}
      >
        <Play className="h-4 w-4" />
        {triggerMut.isPending ? t("sending") : t("scan_all_btn")}
      </button>
      {showCompleted && lastCompleted && (
        <ScrapeResultBadge req={lastCompleted} />
      )}
    </div>
  );
}

function ScrapeResultBadge({ req }: { req: import("@/lib/api").ScrapeRequestRow }) {
  const t = useTranslations("categories");
  const locale = useLocale();
  if (req.status === "failed") {
    return (
      <div
        className="text-xs text-destructive max-w-[280px] truncate"
        title={req.error_message ?? t("error_without_message")}
      >
        {t("scan_failed_msg", { id: req.id, error: req.error_message ?? "—" })}
      </div>
    );
  }
  const degraded = req.status === "degraded";
  const total = req.products_scraped ?? 0;
  const perSite = req.products_per_site ?? {};
  const siteParts = Object.entries(perSite)
    .filter(([, n]) => n > 0)
    .map(([site, n]) => `${site}: ${formatNumber(n, locale)}`)
    .join(", ");
  return (
    <div
      className={`text-xs ${scrapeResultTextClass(req.status)}`}
      title={
        degraded
          ? t("degraded_title", {
              id: req.run_id ?? "—",
              error: req.error_message ?? "—",
            })
          : t("completed_title", {
              id: req.run_id ?? "—",
              date: formatTime(req.completed_at, locale),
            })
      }
    >
      {degraded
        ? t("scan_degraded_msg", { id: req.id, total: formatNumber(total, locale) })
        : t("scan_done_msg", { id: req.id, total: formatNumber(total, locale) })}
      {siteParts && <span className="text-muted-foreground"> ({siteParts})</span>}
    </div>
  );
}


function CategoryRowDesktop({
  cat,
  onEdit,
  onChanged,
}: {
  cat: CategoryRow;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const t = useTranslations("categories");
  const queryClient = useQueryClient();

  const toggleActive = useMutation({
    mutationFn: () =>
      api.categoryUpdate(cat.id, {
        key: cat.key,
        label_ru: cat.label_ru,
        label_az: cat.label_az,
        pharmonline_slug: cat.pharmonline_slug,
        aptekonline_slug: cat.aptekonline_slug,
        aloe_slug: cat.aloe_slug,
        is_active: !cat.is_active,
      }),
    onSuccess: onChanged,
  });

  const remove = useMutation({
    mutationFn: () => api.categoryDelete(cat.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["categories"] }),
  });

  return (
    <tr className="border-t border-border hover:bg-muted/30">
      <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{cat.key}</td>
      <td className="px-3 py-2">{cat.label_ru}</td>
      <td className="px-3 py-2 text-xs">
        {cat.pharmonline_slug ? (
          <span className="font-mono text-muted-foreground">{cat.pharmonline_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aptekonline_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aptekonline_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aloe_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aloe_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-center">
        <button
          onClick={() => toggleActive.mutate()}
          disabled={toggleActive.isPending}
          className={`inline-flex rounded px-2 py-0.5 text-xs font-medium transition-opacity ${
            cat.is_active
              ? "bg-success/10 text-success hover:bg-success/20"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          } ${toggleActive.isPending ? "opacity-50" : ""}`}
        >
          {cat.is_active ? "ON" : "OFF"}
        </button>
      </td>
      <td className="px-3 py-2 text-center">
        <div className="inline-flex items-center gap-2">
          {cat.is_active && (cat.pharmonline_slug || cat.aptekonline_slug || cat.aloe_slug) && (
            <TriggerScrapeButton categoryId={cat.id} />
          )}
          <button
            onClick={onEdit}
            className="text-muted-foreground hover:text-foreground"
            title={t("edit_tooltip")}
          >
            <Pencil className="h-4 w-4" />
          </button>
          <button
            onClick={() => {
              if (confirm(t("delete_confirm", { label: cat.label_ru }))) remove.mutate();
            }}
            disabled={remove.isPending}
            className="text-muted-foreground hover:text-destructive disabled:opacity-50"
            title={t("delete_tooltip")}
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </td>
    </tr>
  );
}

function Stat({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <div
      className={`rounded-lg border bg-card px-3 py-2 ${
        highlight ? "border-success/40 bg-success/5" : "border-border"
      }`}
    >
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  );
}

function CategoryForm({
  onClose,
  editing,
}: {
  onClose: () => void;
  editing?: CategoryRow;
}) {
  const t = useTranslations("categories");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const isEdit = Boolean(editing);
  const [form, setForm] = useState({
    label_ru: editing?.label_ru ?? "",
    label_az: editing?.label_az ?? "",
    // В edit-режиме pre-fill голым slug (без URL) — пусть пользователь видит
    // что было записано и при желании поверх вставит новый URL.
    pharmonline_url: editing?.pharmonline_slug ?? "",
    aptekonline_url: editing?.aptekonline_slug ?? "",
    aloe_url: editing?.aloe_slug ?? "",
  });
  const [error, setError] = useState<string | null>(null);

  const phmSlug = extractSlug("pharmonline", form.pharmonline_url);
  const aptSlug = extractSlug("aptekonline", form.aptekonline_url);
  const aloeSlug = extractSlug("aloe", form.aloe_url);
  // В edit оставляем существующий key (его менять рискованно и обычно не нужно).
  // В add — генерируем slug из label_ru.
  const key = isEdit ? editing!.key : slugify(form.label_ru || form.label_az);

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        key,
        label_ru: form.label_ru,
        label_az: form.label_az.trim(),
        pharmonline_slug: phmSlug || null,
        aptekonline_slug: aptSlug || null,
        aloe_slug: aloeSlug || null,
        is_active: editing?.is_active ?? true,
      };
      return isEdit
        ? api.categoryUpdate(editing!.id, payload).then(() => payload)
        : api.categoryCreate(payload).then(() => payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      onClose();
    },
    onError: (e: unknown) => setError(friendlyError(e, locale) || t("form_save_error")),
  });

  const canSubmit =
    form.label_ru.trim().length > 0 &&
    form.label_az.trim().length > 0 &&
    (phmSlug || aptSlug || aloeSlug);

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">
          {isEdit ? t("form_edit_title", { id: editing!.id }) : t("form_new_title")}
        </h3>
        <button
          onClick={onClose}
          className="rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={tCommon("close")}
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        {isEdit ? t("form_desc_edit") : t("form_desc_add")}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field
          label={t("form_name_ru")}
          required
          value={form.label_ru}
          onChange={(v) => setForm({ ...form, label_ru: v })}
          placeholder="Витамины"
        />
        <Field
          label={t("form_name_az")}
          required
          value={form.label_az}
          onChange={(v) => setForm({ ...form, label_az: v })}
          placeholder="Vitaminlər"
        />
      </div>

      <div className="space-y-2 pt-2 border-t border-border">
        <UrlField
          label="pharmonline.az"
          value={form.pharmonline_url}
          extractedSlug={phmSlug}
          onChange={(v) => setForm({ ...form, pharmonline_url: v })}
          placeholder="https://pharmonline.az/products?category=…"
        />
        <UrlField
          label="aptekonline.az"
          value={form.aptekonline_url}
          extractedSlug={aptSlug}
          onChange={(v) => setForm({ ...form, aptekonline_url: v })}
          placeholder="https://www.aptekonline.az/shop/productList?categoryId[]=…"
        />
        <UrlField
          label="aloe.az"
          value={form.aloe_url}
          extractedSlug={aloeSlug}
          onChange={(v) => setForm({ ...form, aloe_url: v })}
          placeholder="https://aloe.az/catalog/filters/?category_slug=…"
        />
      </div>

      {key && (
        <div className="text-xs text-muted-foreground">
          Key {isEdit ? `(${t("form_key_readonly")})` : `(${t("form_key_auto")})`}:{" "}
          <span className="font-mono">{key}</span>
        </div>
      )}

      <div className="flex gap-2 pt-2">
        <button
          onClick={() => save.mutate()}
          disabled={!canSubmit || save.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? t("form_save_pending") : isEdit ? t("form_apply") : t("form_save")}
        </button>
        <button onClick={onClose} className="text-sm text-muted-foreground hover:text-foreground">
          {tCommon("cancel")}
        </button>
      </div>
      {error && <div className="text-sm text-destructive">{error}</div>}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium block mb-1">
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </label>
  );
}

function UrlField({
  label,
  value,
  extractedSlug,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  extractedSlug: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs font-medium">{label}</span>
        {extractedSlug && (
          <span className="text-xs text-success font-mono">→ {extractedSlug}</span>
        )}
      </div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring font-mono"
      />
    </label>
  );
}
