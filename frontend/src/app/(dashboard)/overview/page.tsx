"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Clock,
  Layers,
  Tag,
  UserCheck,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { api, type RunRow } from "@/lib/api";
import { ActionRow } from "@/components/action-row";
import { KpiCard } from "@/components/kpi-card";
import { OnboardingTip } from "@/components/onboarding-tip";
import { QuickActions } from "@/components/quick-actions";
import { formatRelative } from "@/lib/utils";

export default function OverviewPage() {
  const matchQ = useQuery({ queryKey: ["match-quality"], queryFn: api.matchQuality });
  const actionsQ = useQuery({ queryKey: ["roi-actions"], queryFn: () => api.roiActions() });
  const normalizeQ = useQuery({ queryKey: ["normalize-stats"], queryFn: api.normalizeStats });
  const runsQ = useQuery({ queryKey: ["runs"], queryFn: () => api.runs(5) });
  const dqQ = useQuery({ queryKey: ["data-quality"], queryFn: api.dataQuality });
  const [expandedRun, setExpandedRun] = useState<number | null>(null);

  return (
    <div className="space-y-6">
      <OnboardingTip
        id="overview-welcome-v1"
        title="Это — твой главный экран"
        description={
          <>
            4 KPI карточки сверху: сколько cross-site совпадений, какое
            покрытие и AI confidence. Ниже — «Сегодняшние действия» (где
            конкурент бьёт по цене и где ты можешь поднять). Расписание
            прогонов внизу — Mac launchd скрейпит pharm/aptek в 18:00 Baku,
            aloe — direct с прода в 03:00 UTC.
          </>
        }
      />
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Обзор</h1>
          <p className="text-sm text-muted-foreground">
            Сегодняшние действия и ключевые метрики
          </p>
        </div>
        <QuickActions />
      </div>

      {/* KPI cards */}
      <div className="grid gap-4 grid-cols-2 md:grid-cols-4">
        <KpiCard
          label="Cross-site совпадений"
          value={matchQ.data?.total_matches ?? "—"}
          loading={matchQ.isLoading}
          hint="Товары представленные на ≥ 2 сайтах"
        />
        <KpiCard
          label="Покрытие"
          value={
            matchQ.data ? `${matchQ.data.coverage_pct.toFixed(1)}%` : "—"
          }
          loading={matchQ.isLoading}
          hint="% продуктов с cross-site матчем"
        />
        <KpiCard
          label="Всего продуктов"
          value={matchQ.data?.products_total ?? "—"}
          loading={matchQ.isLoading}
          hint="Уникальных SKU во всех 3 сайтах"
        />
        {/*
          P1.3 (PO Audit 2026-05-17): раньше карточка показывала coverage_pct
          (99.1% «AI прошёл хоть как-то») и в hint'е писала «28457 нужно
          проверить» — внутреннее противоречие. Теперь — high-confidence
          процент: (normalized - needs_review) / total. Это даёт честное
          представление качества AI extraction. Например 35.9% реально-уверенно
          извлечённых атрибутов, остальные 64% — нужно review-нуть.
        */}
        <KpiCard
          label="AI confidence (high)"
          value={
            normalizeQ.data
              ? `${(
                  ((normalizeQ.data.products_normalized -
                    normalizeQ.data.needs_review) /
                    Math.max(1, normalizeQ.data.products_total)) *
                  100
                ).toFixed(1)}%`
              : "—"
          }
          loading={normalizeQ.isLoading}
          hint={
            normalizeQ.data
              ? `${normalizeQ.data.needs_review.toLocaleString("ru-RU")} из ${normalizeQ.data.products_total.toLocaleString("ru-RU")} с low-confidence`
              : "Доля продуктов с надёжно извлечёнными active_ingredient/dosage/pack"
          }
        />
      </div>

      {/*
        Match strategy distribution. Раньше показывалось raw tech-labels
        (legacy_fuzzy 2577, ai_attrs_strict 493…). PO Audit отметил это как
        cross-cutting issue («Технические артефакты везде»). Теперь:
          • Russian читаемые лейблы
          • Сортировка по качеству, не по count'у (good сверху)
          • Color tier: high/medium/low quality
          • Процент от общего количества рядом с count'ом
          • Tech name (legacy_fuzzy) в tooltip для аудита
      */}
      {normalizeQ.data &&
        Object.keys(normalizeQ.data.matches_by_strategy).length > 0 && (
          <MatchStrategyRow byStrategy={normalizeQ.data.matches_by_strategy} />
        )}

      {/*
        P2 (PO Audit 2026-05-17): «Метрики, которые хотелось бы видеть, но их
        нет». Brand quality, Cross-3 категорий (где есть все 3 сайта), скорость
        ручного матчинга (за последние 7 дней), и MTTR-индикатор «свежесть
        scrape» — oldest из трёх сайтов. Не раздуваем главный KPI-grid, держим
        отдельной более компактной полосой ниже стратегий.
      */}
      {dqQ.data && (
        <div className="grid gap-2 grid-cols-2 md:grid-cols-4">
          <StatTile
            icon={Tag}
            label="Brand quality"
            value={`${dqQ.data.brand_extraction_rate_pct}%`}
            status={
              dqQ.data.brand_extraction_rate_pct >= 80
                ? "good"
                : dqQ.data.brand_extraction_rate_pct >= 60
                  ? "warn"
                  : "bad"
            }
            hint={`${dqQ.data.products_with_good_brand.toLocaleString("ru-RU")} из ${dqQ.data.products_total.toLocaleString("ru-RU")} с осмысленным брендом`}
          />
          {/*
            P2 recon (2026-05-18): aloe.az имеет ВСЕГО 4 реальных категории,
            значит потолок Cross-3 = 4. Реальный leverage — Cross-2 pharm × apt
            (потолок ~46). Badge `+N ready` зелёной пилюлей — это main CTA
            страницы: 120 suggestions ждут 1-click mapping.
          */}
          <StatTile
            icon={Layers}
            label="Category mappings"
            value={`${dqQ.data.cross_2_count}/${dqQ.data.cross_2_pharm_apt_ceiling}`}
            status={
              dqQ.data.cross_2_pharm_apt_ceiling > 0 &&
              dqQ.data.cross_2_count / dqQ.data.cross_2_pharm_apt_ceiling >= 0.7
                ? "good"
                : "warn"
            }
            badge={
              dqQ.data.cross_2_pending_suggestions > 0
                ? `+${dqQ.data.cross_2_pending_suggestions} ready`
                : undefined
            }
            hint={`Cross-3: ${dqQ.data.cross_3_count}/${dqQ.data.cross_3_ceiling} · клик → suggestions`}
            href="/categories?view=suggestions"
          />
          <StatTile
            icon={UserCheck}
            label="Manual matches (7д)"
            value={dqQ.data.manual_matches_last_7d}
            status={dqQ.data.manual_matches_last_7d >= 3 ? "good" : "neutral"}
            hint="Сколько кластеров создано/привязано вручную за неделю"
          />
          <StatTile
            icon={Clock}
            label="Свежесть scrape"
            value={formatScrapeFreshness(dqQ.data.last_scrape_per_site)}
            status={scrapeFreshnessStatus(dqQ.data.last_scrape_per_site)}
            hint={scrapeFreshnessHint(dqQ.data.last_scrape_per_site)}
          />
        </div>
      )}

      {/* Today's actions */}
      <div>
        <h2 className="text-lg font-semibold mb-3">Сегодняшние действия</h2>
        {actionsQ.isLoading && (
          <div className="rounded-md border border-border bg-card p-3 text-sm text-muted-foreground flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full bg-primary animate-pulse" />
            Анализ pricing-recommendations… может занять до 15 сек на 3K матчей
          </div>
        )}
        {actionsQ.error && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive flex items-center justify-between gap-3">
            <div>
              {actionsQ.error instanceof Error
                ? actionsQ.error.message
                : "Не удалось загрузить рекомендации"}
            </div>
            <button
              onClick={() => actionsQ.refetch()}
              className="rounded border border-destructive/50 px-2 py-1 text-xs hover:bg-destructive/20"
            >
              Повторить
            </button>
          </div>
        )}
        {actionsQ.data && actionsQ.data.length === 0 && (
          <div className="rounded-md border border-dashed border-border bg-card p-6 text-center text-sm text-muted-foreground">
            <div className="text-base font-medium text-foreground mb-1">
              Pricing на уровне 👌
            </div>
            <div>Никаких critical undercut'ов или missing-SKU не найдено.</div>
            <div className="text-xs mt-2 text-muted-foreground/70">
              Когда конкурент опустит цену &gt;3% — действие появится здесь.
            </div>
          </div>
        )}
        <div className="space-y-2 mt-2">
          {actionsQ.data?.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </div>

      {/* Recent runs */}
      <div>
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-lg font-semibold">Последние прогоны</h2>
          {runsQ.data && runsQ.data.length > 0 && <RunsSummary runs={runsQ.data} />}
        </div>
        <p className="text-xs text-muted-foreground mb-2">
          Кликни на строку чтобы увидеть breakdown — сколько товаров на каждом
          сайте по каждой категории. <strong>cancelled</strong> ≠ failure: это
          прогон, который завис и был прибит scheduled cleanup task'ом (обычно
          ручной CLI-запуск который не довели до конца).
        </p>
        <div className="rounded-lg border border-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th className="px-3 py-2 w-6"></th>
                <th className="px-3 py-2 text-left">ID</th>
                <th className="px-3 py-2 text-left">Started</th>
                <th className="px-3 py-2 text-left hidden sm:table-cell">Длительность</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-right">Products</th>
                <th className="px-3 py-2 text-left hidden md:table-cell">Sites</th>
              </tr>
            </thead>
            <tbody>
              {runsQ.data?.map((r) => (
                <RunRowExpandable
                  key={r.id}
                  run={r}
                  isExpanded={expandedRun === r.id}
                  onToggle={() =>
                    setExpandedRun(expandedRun === r.id ? null : r.id)
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function RunRowExpandable({
  run,
  isExpanded,
  onToggle,
}: {
  run: RunRow;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const breakdownQ = useQuery({
    queryKey: ["run-breakdown", run.id],
    queryFn: () => api.runBreakdown(run.id),
    enabled: isExpanded,
  });

  return (
    <>
      <tr
        className="border-t border-border cursor-pointer hover:bg-muted/30"
        onClick={onToggle}
      >
        <td className="px-3 py-2">
          {isExpanded ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
        </td>
        <td className="px-3 py-2 font-mono text-xs">{run.id}</td>
        <td className="px-3 py-2 text-muted-foreground" title={run.started_at ?? ""}>
          {formatRelative(run.started_at)}
        </td>
        <td className="px-3 py-2 text-muted-foreground hidden sm:table-cell tabular-nums">
          {formatDuration(run.started_at, run.finished_at)}
        </td>
        <td className="px-3 py-2">
          <StatusBadge status={run.status} errorMessage={run.error_message} />
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{run.products_scraped}</td>
        <td className="px-3 py-2 text-muted-foreground hidden md:table-cell">
          {run.sites_completed}
        </td>
      </tr>
      {isExpanded && (
        <tr className="border-t border-border bg-muted/10">
          <td colSpan={6} className="px-3 py-3">
            {breakdownQ.isLoading && (
              <div className="text-xs text-muted-foreground">Загружаю breakdown…</div>
            )}
            {breakdownQ.data && (
              <RunBreakdownPanel data={breakdownQ.data} />
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function RunBreakdownPanel({
  data,
}: {
  data: {
    products_per_site: Record<string, number>;
    products_per_site_category: Record<string, Record<string, number>>;
  };
}) {
  // Load categories один раз — нужно для маппинга slug → label_ru.
  // Slug на каждом сайте свой (pharm: 'vitamin-ve-mineral-kompleks',
  // apt: '78', aloe: 'uşaq-qidası'), поэтому строим lookup-table per site.
  const catsQ = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const labelLookup = (site: string, slugOrId: string): string | null => {
    const cats = catsQ.data ?? [];
    const match = cats.find((c) => {
      if (site === "pharmonline") return c.pharmonline_slug === slugOrId;
      if (site === "aptekonline") return c.aptekonline_slug === slugOrId;
      if (site === "aloe") return c.aloe_slug === slugOrId;
      return false;
    });
    return match?.label_ru ?? null;
  };

  const sites = Object.keys(data.products_per_site_category).sort();
  if (sites.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        Этот прогон не содержит per-category breakdown — он был выполнен до
        включения детальной статистики. Свежие прогоны имеют полную разбивку.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {sites.map((site) => (
          <div
            key={site}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs"
          >
            <span className="font-medium">{site}:</span>{" "}
            <span className="tabular-nums font-semibold">
              {data.products_per_site[site] ?? 0}
            </span>{" "}
            <span className="text-muted-foreground">всего</span>
          </div>
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {sites.map((site) => {
          const cats = data.products_per_site_category[site] || {};
          const sorted = Object.entries(cats).sort(([, a], [, b]) => b - a);
          return (
            <div key={site} className="rounded-md border border-border bg-card p-3">
              <div className="font-medium text-sm mb-2">{site}</div>
              <div className="space-y-1.5">
                {sorted.map(([cat, count]) => {
                  const label = labelLookup(site, cat);
                  return (
                    <div
                      key={cat}
                      className="flex items-start justify-between gap-2 text-xs"
                    >
                      <div className="min-w-0 flex-1">
                        {label ? (
                          <>
                            <div className="truncate font-medium" title={label}>
                              {label}
                            </div>
                            <div className="truncate text-muted-foreground/70 font-mono text-[10px]">
                              {cat}
                            </div>
                          </>
                        ) : (
                          <div className="truncate text-muted-foreground" title={cat}>
                            {cat}
                          </div>
                        )}
                      </div>
                      <span className="font-mono tabular-nums shrink-0">{count}</span>
                    </div>
                  );
                })}
                {sorted.length === 0 && (
                  <div className="text-xs text-muted-foreground">
                    Нет данных
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function formatDuration(
  startedAt: string | null,
  finishedAt: string | null,
): string {
  if (!startedAt) return "—";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds}с`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) {
    const s = seconds % 60;
    return s > 0 ? `${mins}м ${s}с` : `${mins}м`;
  }
  const hours = Math.floor(mins / 60);
  const m = mins % 60;
  return `${hours}ч ${m}м`;
}

/**
 * Сводка по последним N прогонам в виде маленьких chip'ов:
 *   «3 ok · 1 cancelled · 0 failed»
 *
 * Без неё PO смотрел на 4/5 красных «failed» и видел production outage,
 * хотя реально fail rate = 0% (все «failed» — это zombie cleanup'ы от
 * manual CLI попыток которые не довели до конца).
 */
function RunsSummary({ runs }: { runs: RunRow[] }) {
  const counts = runs.reduce(
    (acc, r) => {
      if (r.status === "ok") {
        acc.ok += 1;
      } else if (
        r.error_message &&
        /zombie cleanup|cancelled/i.test(r.error_message)
      ) {
        acc.cancelled += 1;
      } else if (r.status === "failed") {
        acc.failed += 1;
      } else {
        acc.other += 1;
      }
      return acc;
    },
    { ok: 0, cancelled: 0, failed: 0, other: 0 },
  );

  return (
    <div className="flex items-center gap-2 text-xs">
      {counts.ok > 0 && (
        <span className="inline-flex items-center gap-1 rounded-full bg-success/10 text-success px-2 py-0.5">
          <span className="font-semibold">{counts.ok}</span> ok
        </span>
      )}
      {counts.cancelled > 0 && (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-muted text-muted-foreground px-2 py-0.5"
          title="Zombie-cleanup'ы — не реальные production failures, а ручные запуски которые не финишировали"
        >
          <span className="font-semibold">{counts.cancelled}</span> cancelled
        </span>
      )}
      {counts.failed > 0 && (
        <span className="inline-flex items-center gap-1 rounded-full bg-destructive/10 text-destructive px-2 py-0.5">
          <span className="font-semibold">{counts.failed}</span> failed
        </span>
      )}
      {counts.other > 0 && (
        <span className="inline-flex items-center gap-1 rounded-full bg-muted text-muted-foreground px-2 py-0.5">
          <span className="font-semibold">{counts.other}</span> прочее
        </span>
      )}
    </div>
  );
}

/**
 * Distinguish three states:
 *   - ok                          → success green
 *   - failed (real failure)       → destructive red
 *   - cancelled / zombie-cleanup  → muted grey
 *
 * Раньше zombie cleanup'ы (вечно-running прогоны, прибитые scheduled task'ом)
 * показывались как `failed` красным — выглядело как production outage хотя на
 * самом деле это были diagnostic runs которые не довели до конца. Теперь они
 * визуально отделены: серый «cancelled» badge + tooltip с пояснением.
 */
function StatusBadge({
  status,
  errorMessage,
}: {
  status: string;
  errorMessage?: string | null;
}) {
  const isCancelled =
    status !== "ok" &&
    errorMessage &&
    /zombie cleanup|cancelled/i.test(errorMessage);

  if (status === "ok") {
    return (
      <span className="inline-flex rounded px-2 py-0.5 text-xs font-medium bg-success/10 text-success">
        ok
      </span>
    );
  }
  if (isCancelled) {
    return (
      <span
        className="inline-flex rounded px-2 py-0.5 text-xs font-medium bg-muted text-muted-foreground"
        title={`Cancelled (zombie cleanup): ${errorMessage}. Это не реальный fail — прогон завис и был прибит scheduled task'ом.`}
      >
        cancelled
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span
        className="inline-flex rounded px-2 py-0.5 text-xs font-medium bg-destructive/10 text-destructive"
        title={errorMessage ?? undefined}
      >
        failed
      </span>
    );
  }
  return (
    <span className="inline-flex rounded px-2 py-0.5 text-xs font-medium bg-muted text-muted-foreground">
      {status}
    </span>
  );
}

/**
 * Метаинформация о стратегии матчинга для UI:
 *  - `label` — человекочитаемый ярлык (Russian)
 *  - `tier` — quality tier для color-кода
 *  - `description` — длинное объяснение в tooltip + tech name для аудита
 *  - `order` — приоритет сортировки (lower = first, лучшее качество вверху)
 */
type StrategyTier = "high" | "medium" | "low";
type StrategyMeta = {
  label: string;
  tier: StrategyTier;
  description: string;
  order: number;
};

const STRATEGY_META: Record<string, StrategyMeta> = {
  manual: {
    label: "Вручную",
    tier: "high",
    description: "manual — подтверждено оператором через UI. Самое надёжное.",
    order: 0,
  },
  ai_attrs_strict: {
    label: "AI строгий",
    tier: "high",
    description:
      "ai_attrs_strict — AI извлёк active_ingredient + dosage + pack + brand, все 4 совпали с другим сайтом. Высокая уверенность.",
    order: 1,
  },
  ai_attrs_partial: {
    label: "AI частичный",
    tier: "medium",
    description:
      "ai_attrs_partial — AI извлёк active_ingredient + 1-2 атрибута. Средняя уверенность, бывают false-positives.",
    order: 2,
  },
  legacy_fuzzy: {
    label: "По названию",
    tier: "low",
    description:
      "legacy_fuzzy — token_set_ratio по сырому названию. Worst quality: бьёт false-positives типа «Friso 3 Gold ↔ Friso Prematures».",
    order: 3,
  },
  unknown: {
    label: "Без метки",
    tier: "low",
    description:
      "unknown — матчи pre-strategy-tracking (старая БД без поля match_strategy). Аудит-trail отсутствует.",
    order: 4,
  },
};

function strategyMeta(s: string): StrategyMeta {
  return (
    STRATEGY_META[s] ?? {
      label: s,
      tier: "low",
      description: s,
      order: 99,
    }
  );
}

function MatchStrategyRow({
  byStrategy,
}: {
  byStrategy: Record<string, number>;
}) {
  const total = Object.values(byStrategy).reduce((sum, n) => sum + n, 0);
  const entries = Object.entries(byStrategy)
    .map(([key, count]) => ({ key, count, meta: strategyMeta(key) }))
    .sort((a, b) => a.meta.order - b.meta.order);

  const tierClass: Record<StrategyTier, string> = {
    high: "bg-success/10 text-success border-success/30",
    medium: "bg-warning/10 text-warning border-warning/30",
    low: "bg-muted text-muted-foreground border-border",
  };

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">Качество матчей:</span>
      {entries.map(({ key, count, meta }) => {
        const pct = total > 0 ? Math.round((count / total) * 100) : 0;
        return (
          <span
            key={key}
            className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 ${tierClass[meta.tier]}`}
            title={`${meta.description} (${count.toLocaleString("ru-RU")} матчей)`}
          >
            <span>{meta.label}</span>
            <span className="font-semibold tabular-nums">
              {count.toLocaleString("ru-RU")}
            </span>
            {pct >= 1 && (
              <span className="text-[10px] opacity-70 tabular-nums">
                {pct}%
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}

/**
 * Маленькая стат-плитка, легче по визуальному весу чем KpiCard. Используется
 * под основным KPI-grid'ом для дополнительных data-quality метрик чтобы не
 * раздувать главную сетку с 4 до 8 карточек.
 *
 * Features:
 * - `icon` — lucide-react иконка для визуальной distinction между тайлами
 * - `status` — цвет числа (good=green, warn=yellow, bad=red, neutral)
 * - `badge` — зелёная пилюля с actionable инсайтом (типа «+120 ready»)
 * - `href` — Link с hover highlight; пассивная метрика → workflow start
 */
type StatStatus = "good" | "warn" | "bad" | "neutral";

function StatTile({
  label,
  value,
  hint,
  href,
  icon: Icon,
  status = "neutral",
  badge,
}: {
  label: string;
  value: string | number;
  hint?: string;
  href?: string;
  icon?: LucideIcon;
  status?: StatStatus;
  badge?: string;
}) {
  const baseClasses =
    "rounded-lg border bg-card px-3.5 py-3 block";
  const borderClass = {
    good: "border-success/30",
    warn: "border-warning/40",
    bad: "border-destructive/40",
    neutral: "border-border",
  }[status];
  const valueColor = {
    good: "text-success",
    warn: "text-warning",
    bad: "text-destructive",
    neutral: "text-foreground",
  }[status];
  const interactive = href
    ? "cursor-pointer transition-colors hover:bg-muted/40 hover:border-primary/50"
    : "";
  const content = (
    <>
      <div className="flex items-center gap-1.5">
        {Icon && (
          <Icon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        )}
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground truncate">
          {label}
        </div>
        {href && (
          <span
            className="ml-auto text-muted-foreground/60 text-xs"
            aria-hidden
          >
            →
          </span>
        )}
      </div>
      <div
        className={`text-xl font-semibold tabular-nums leading-tight mt-1 ${valueColor}`}
      >
        {value}
      </div>
      {badge && (
        <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2 py-0.5 text-[10px] font-medium mt-1.5">
          {badge}
        </span>
      )}
      {hint && (
        <div className="text-[11px] text-muted-foreground/80 mt-1 line-clamp-1">
          {hint}
        </div>
      )}
    </>
  );
  if (href) {
    return (
      <Link
        href={href}
        className={`${baseClasses} ${borderClass} ${interactive}`}
        title={hint}
      >
        {content}
      </Link>
    );
  }
  return (
    <div className={`${baseClasses} ${borderClass}`} title={hint}>
      {content}
    </div>
  );
}

/**
 * Возвращает relative-форматированный «худший» (oldest) timestamp из
 * last_scrape_per_site. Если есть None — это сайт без данных, считаем
 * «застой» и возвращаем «нет данных».
 */
function formatScrapeFreshness(
  perSite: Record<string, string | null>,
): string {
  const values = Object.values(perSite);
  if (values.length === 0) return "—";
  if (values.some((v) => v === null)) return "нет данных";
  const oldestIso = values
    .filter((v): v is string => Boolean(v))
    .reduce((acc, iso) =>
      new Date(iso).getTime() < new Date(acc).getTime() ? iso : acc,
    );
  return formatRelative(oldestIso);
}

function scrapeFreshnessHint(
  perSite: Record<string, string | null>,
): string {
  const parts = Object.entries(perSite).map(([site, iso]) => {
    return `${site}: ${iso ? formatRelative(iso) : "нет"}`;
  });
  return parts.join(" · ");
}

/**
 * Возвращает статус для color-coding плитки «Свежесть scrape».
 * Базово: oldest scrape > 36h = bad, > 24h = warn, иначе good.
 * (Daily timers: pharm+apt в 18:00 Baku, aloe в 03:00 UTC — после 24h задержки
 * один из сайтов застрял или таймер упал.)
 */
function scrapeFreshnessStatus(
  perSite: Record<string, string | null>,
): StatStatus {
  const isoValues = Object.values(perSite).filter(
    (v): v is string => Boolean(v),
  );
  if (isoValues.length === 0) return "bad";
  if (Object.values(perSite).some((v) => v === null)) return "warn";
  const oldestMs = Math.min(...isoValues.map((iso) => new Date(iso).getTime()));
  const hoursAgo = (Date.now() - oldestMs) / 3_600_000;
  if (hoursAgo > 36) return "bad";
  if (hoursAgo > 24) return "warn";
  return "good";
}
