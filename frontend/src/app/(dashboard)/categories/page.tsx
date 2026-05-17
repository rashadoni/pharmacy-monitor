"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Lightbulb, Pencil, Play, Plus, Trash2, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { api, friendlyError, type CategoryRow, type CategorySuggestion } from "@/lib/api";
import { OnboardingTip } from "@/components/onboarding-tip";

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

type CrossFilter = "" | "cross2" | "cross3" | "missing3";

export default function CategoriesPage() {
  const searchParams = useSearchParams();
  const [search, setSearch] = useState("");
  const [siteFilter, setSiteFilter] = useState<"" | "pharmonline" | "aptekonline" | "aloe">("");
  const [activeOnly, setActiveOnly] = useState(false);
  // P2 (PO Audit 2026-05-17): data-quality strip на /overview ведёт сюда с
  // ?missing=3 чтобы сразу показать категории без полного 3-сайтового маппинга.
  // Это actionable view: PO видит «357 категорий без 3 сайтов» вместо «1 с»,
  // и может за один заход дочистить mapping → Cross-3 от 1 → 30+.
  const initialCross: CrossFilter = (() => {
    const m = searchParams.get("missing");
    if (m === "3") return "missing3";
    const c = searchParams.get("cross");
    if (c === "2") return "cross2";
    if (c === "3") return "cross3";
    return "";
  })();
  const [crossFilter, setCrossFilter] = useState<CrossFilter>(initialCross);
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<CategoryRow | null>(null);
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
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
    if (crossFilter === "missing3" && c.pharmonline_slug && c.aptekonline_slug && c.aloe_slug) return false;
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

  const [view, setView] = useState<"list" | "suggestions">("list");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Категории</h1>
          <p className="text-sm text-muted-foreground">
            Категории для скрейпинга. ON-категории идут в next-run; OFF — пропускаются.
          </p>
        </div>
        <div className="flex gap-2">
          <TriggerScrapeButton />
          <button
            onClick={() => setShowAdd(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm font-medium hover:bg-primary/90"
          >
            <Plus className="h-4 w-4" />
            Добавить
          </button>
        </div>
      </div>

      {/* P1.1 Tabs: Список / Suggested mappings */}
      <div className="flex items-center gap-1 border-b border-border">
        <button
          onClick={() => setView("list")}
          className={`inline-flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            view === "list"
              ? "border-primary text-foreground font-medium"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          Список ({stats.total})
        </button>
        <button
          onClick={() => setView("suggestions")}
          className={`inline-flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            view === "suggestions"
              ? "border-primary text-foreground font-medium"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          <Lightbulb className="h-3.5 w-3.5" />
          Предложенные mapping&apos;и
        </button>
      </div>

      {view === "suggestions" ? <SuggestionsPanel /> : (
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
        <Stat label="Всего" value={stats.total} />
        <Stat label="Active" value={stats.active} />
        <Stat label="Cross-2" value={stats.cross2} highlight />
        <Stat
          label="Cross-3"
          value={stats.cross3}
          highlight
          onClick={() => setCrossFilter("missing3")}
          subValue={`${stats.total - stats.cross3} без полного`}
        />
        <Stat label="pharmonline" value={stats.pharmonline} />
        <Stat label="aptekonline" value={stats.aptekonline} />
        <Stat label="aloe" value={stats.aloe} />
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-2">
        <input
          type="search"
          placeholder="🔎 Поиск по названию / key"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
        <select
          value={siteFilter}
          onChange={(e) => setSiteFilter(e.target.value as any)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          <option value="">Все сайты</option>
          <option value="pharmonline">pharmonline</option>
          <option value="aptekonline">aptekonline</option>
          <option value="aloe">aloe</option>
        </select>
        <select
          value={crossFilter}
          onChange={(e) => setCrossFilter(e.target.value as CrossFilter)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          <option value="">Любой охват</option>
          <option value="cross2">Только Cross-2 (pharm+apt)</option>
          <option value="cross3">Только Cross-3 (все 3 сайта)</option>
          <option value="missing3">Без полного 3-site маппинга</option>
        </select>
        <label className="inline-flex items-center gap-2 px-3 text-sm">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
            className="rounded"
          />
          Только Active
        </label>
      </div>

      {isLoading && <div className="text-muted-foreground">Загрузка…</div>}

      <div className="rounded-lg border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">Key</th>
              <th className="px-3 py-2 text-left">Label</th>
              <th className="px-3 py-2 text-left">pharmonline</th>
              <th className="px-3 py-2 text-left">aptekonline</th>
              <th className="px-3 py-2 text-left">aloe</th>
              <th className="px-3 py-2 text-center">Active</th>
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

      {filtered.length === 0 && !isLoading && (
        <div className="text-muted-foreground text-center py-4">
          По текущему фильтру ничего не найдено.
        </div>
      )}
      </>
      )}
    </div>
  );
}

function SuggestionsPanel() {
  const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
  type Site = (typeof SITES)[number];
  const [siteA, setSiteA] = useState<Site>("pharmonline");
  const [siteB, setSiteB] = useState<Site>("aptekonline");
  const [minOverlap, setMinOverlap] = useState(5);
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["category-suggestions", siteA, siteB, minOverlap],
    queryFn: () => api.categorySuggestions({ site_a: siteA, site_b: siteB, min_overlap: minOverlap }),
    enabled: siteA !== siteB,
  });

  const mapMutation = useMutation({
    mutationFn: (payload: {
      site_a: Site;
      site_a_slug: string;
      site_b: Site;
      site_b_slug: string;
    }) => api.categoryMappingCreate(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["category-suggestions"] });
      queryClient.invalidateQueries({ queryKey: ["categories"] });
    },
    onError: (e) => alert(friendlyError(e)),
  });

  const notMapped = (data ?? []).filter((s) => !s.already_mapped);
  const mapped = (data ?? []).filter((s) => s.already_mapped);

  return (
    <div className="space-y-4">
      <OnboardingTip
        id="categories-suggestions-v1"
        title="Категория-мапер по brand-overlap"
        description={
          <>
            Cross-3 категорий = 1 — это узкое горлышко покрытия. Тут видны
            пары категорий из 2 сайтов где много общих брендов — они скорее
            всего одна категория. Связал → у матчера появляется shared
            контекст → больше cross-site matches. Цель: довести Cross-3 до 30+.
          </>
        }
      />
      <div className="rounded-md bg-muted/30 border border-border p-3 text-xs text-muted-foreground">
        <Lightbulb className="inline h-3.5 w-3.5 mr-1 -mt-0.5" />
        Подсказки на основе <strong>shared brands</strong> между категориями двух сайтов.
        Если у двух slug&apos;ов одни и те же 5+ брендов — это, скорее всего, одна категория.
        Клик «Связать» создаёт Category row (или дополняет существующий, если slug одного из сайтов уже там).
      </div>

      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <SiteSelector value={siteA} onChange={setSiteA} label="Сайт A" disabled={siteB} />
        <span className="text-muted-foreground">↔</span>
        <SiteSelector value={siteB} onChange={setSiteB} label="Сайт B" disabled={siteA} />
        <label className="inline-flex items-center gap-2 text-sm ml-auto">
          Мин. overlap:
          <input
            type="number"
            value={minOverlap}
            min={2}
            max={50}
            onChange={(e) => setMinOverlap(Number(e.target.value) || 3)}
            className="w-16 rounded-md border border-input bg-background px-2 py-1 text-sm"
          />
        </label>
      </div>

      {isLoading && <div className="text-sm text-muted-foreground py-4">Ищу пересечения…</div>}
      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          {friendlyError(error)}
        </div>
      )}
      {!isLoading && data && notMapped.length === 0 && mapped.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          Нет пересечений с overlap ≥ {minOverlap} брендов. Понизь порог или выбери другую пару сайтов.
        </div>
      )}

      {notMapped.length > 0 && (
        <div className="rounded-lg border border-border bg-card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-sm font-semibold">
              Новые предложения ({notMapped.length})
            </h2>
            <p className="text-xs text-muted-foreground">
              Категории-кандидаты на mapping. Сортировка — по убыванию shared brands.
            </p>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-muted-foreground text-xs">
              <tr>
                <th className="px-4 py-2 text-left">{siteA}</th>
                <th className="px-4 py-2 text-left">{siteB}</th>
                <th className="px-4 py-2 text-right">Shared brands</th>
                <th className="px-4 py-2 text-left">Примеры</th>
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
                      {s.site_a_products} prod
                    </div>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {s.site_b_slug}
                    <div className="text-[10px] text-muted-foreground/70">
                      {s.site_b_products} prod
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
                      Связать
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {mapped.length > 0 && (
        <details className="rounded-lg border border-border bg-muted/20">
          <summary className="px-4 py-2 cursor-pointer text-sm text-muted-foreground">
            Уже связанные ({mapped.length})
          </summary>
          <table className="w-full text-xs">
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
                    <CheckCircle2 className="inline h-3.5 w-3.5 mr-1" /> связано
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
  value: "pharmonline" | "aptekonline" | "aloe";
  onChange: (v: "pharmonline" | "aptekonline" | "aloe") => void;
  label: string;
  disabled: string; // имя другого сайта, который нельзя выбрать (запрет site_a == site_b)
}) {
  const SITES = ["pharmonline", "aptekonline", "aloe"] as const;
  return (
    <label className="inline-flex items-center gap-2 text-sm">
      <span className="text-xs text-muted-foreground uppercase">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as typeof value)}
        className="rounded-md border border-input bg-background px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {SITES.map((s) => (
          <option key={s} value={s} disabled={s === disabled}>
            {s}
          </option>
        ))}
      </select>
    </label>
  );
}

function TriggerScrapeButton({ categoryId }: { categoryId?: number } = {}) {
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
    onError: (e: Error) => alert(e.message),
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
    ? requestsQ.data?.find((r) => r.status === "ok" || r.status === "failed")
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
          {activeThis.status === "pending" ? "в очереди" : "сканируем"} #{activeThis.id}
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
            ? `Идёт полное сканирование #${activeAll.id} — оно уже включает эту категорию`
            : "Запустить сканирование только этой категории"
        }
      >
        <Play className="h-3 w-3" />
        Сканировать
      </button>
    );
  }

  // === Глобальная кнопка (mode=all) ======================================
  if (activeAll) {
    return (
      <div className="inline-flex items-center gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
        <Play className="h-4 w-4 animate-pulse" />
        <span className="font-medium">
          {activeAll.status === "pending" ? "В очереди…" : "Сканируем все…"}
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
        title="Запустить scan всех активных категорий"
      >
        <Play className="h-4 w-4" />
        {triggerMut.isPending ? "Отправляем…" : "Сканировать все"}
      </button>
      {showCompleted && lastCompleted && (
        <ScrapeResultBadge req={lastCompleted} />
      )}
    </div>
  );
}

function ScrapeResultBadge({ req }: { req: import("@/lib/api").ScrapeRequestRow }) {
  if (req.status === "failed") {
    return (
      <div className="text-xs text-destructive max-w-[280px] truncate" title={req.error_message ?? "Без сообщения об ошибке"}>
        ✗ Сбой #{req.id}: {req.error_message ?? "—"}
      </div>
    );
  }
  // status === 'ok'
  const total = req.products_scraped ?? 0;
  const perSite = req.products_per_site ?? {};
  const siteParts = Object.entries(perSite)
    .filter(([, n]) => n > 0)
    .map(([site, n]) => `${site}: ${n}`)
    .join(", ");
  return (
    <div className="text-xs text-success" title={`run_id=${req.run_id}, завершено ${req.completed_at}`}>
      ✓ Готово #{req.id} — <span className="font-medium">{total.toLocaleString("ru-RU")}</span> товаров
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
          <span className="text-muted-foreground/70">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aptekonline_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aptekonline_slug}</span>
        ) : (
          <span className="text-muted-foreground/70">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aloe_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aloe_slug}</span>
        ) : (
          <span className="text-muted-foreground/70">—</span>
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
            title="Редактировать"
          >
            <Pencil className="h-4 w-4" />
          </button>
          <button
            onClick={() => {
              if (confirm(`Удалить категорию «${cat.label_ru}»?`)) remove.mutate();
            }}
            disabled={remove.isPending}
            className="text-muted-foreground hover:text-destructive disabled:opacity-50"
            title="Удалить"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </td>
    </tr>
  );
}

function Stat({
  label,
  value,
  highlight,
  onClick,
  subValue,
}: {
  label: string;
  value: number;
  highlight?: boolean;
  onClick?: () => void;
  subValue?: string;
}) {
  const baseClasses = `rounded-lg border bg-card px-3 py-2 ${
    highlight ? "border-success/40 bg-success/5" : "border-border"
  }`;
  const interactive = onClick
    ? "cursor-pointer transition-colors hover:bg-muted/40 hover:border-primary/40"
    : "";
  return (
    <div
      className={`${baseClasses} ${interactive}`}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      title={onClick ? "Кликнуть → фильтр «без полного 3-site маппинга»" : undefined}
    >
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
      {subValue && (
        <div className="text-[11px] text-muted-foreground/80 mt-0.5">
          {subValue}
        </div>
      )}
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
        label_az: form.label_az || null,
        pharmonline_slug: phmSlug || null,
        aptekonline_slug: aptSlug || null,
        aloe_slug: aloeSlug || null,
        is_active: editing?.is_active ?? true,
      };
      return isEdit
        ? api.categoryUpdate(editing!.id, payload).then(() => payload)
        : api.categoryCreate(payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      onClose();
    },
    onError: (e: any) => setError(e?.message || "Не удалось сохранить"),
  });

  const canSubmit =
    form.label_ru.trim().length > 0 && (phmSlug || aptSlug || aloeSlug);

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">
          {isEdit ? `Редактировать категорию #${editing!.id}` : "Новая категория"}
        </h3>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        {isEdit
          ? "Можно поправить название или slug на любом сайте. Изменение slug повлияет только на следующий scrape — существующие продукты не удалятся."
          : "Вставь URL категории с каждого сайта — slug извлечётся автоматически. Можно ввести голый slug, если уже знаешь."}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field
          label="Название (RU)"
          required
          value={form.label_ru}
          onChange={(v) => setForm({ ...form, label_ru: v })}
          placeholder="Витамины"
        />
        <Field
          label="Название (AZ)"
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
          Key {isEdit ? "(read-only при редактировании)" : "(автоматически)"}:{" "}
          <span className="font-mono">{key}</span>
        </div>
      )}

      <div className="flex gap-2 pt-2">
        <button
          onClick={() => save.mutate()}
          disabled={!canSubmit || save.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? "Сохраняем…" : isEdit ? "Применить" : "Сохранить"}
        </button>
        <button onClick={onClose} className="text-sm text-muted-foreground hover:text-foreground">
          Отмена
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
