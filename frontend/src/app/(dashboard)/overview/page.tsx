"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
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

      {/* Match strategy distribution — компактная пилюля под KPI */}
      {normalizeQ.data &&
        Object.keys(normalizeQ.data.matches_by_strategy).length > 0 && (
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="text-muted-foreground">Стратегии матчей:</span>
            {Object.entries(normalizeQ.data.matches_by_strategy)
              .sort(([, a], [, b]) => b - a)
              .map(([strategy, count]) => (
                <span
                  key={strategy}
                  className="inline-flex items-center gap-1 rounded bg-muted/50 px-2 py-0.5 font-mono"
                  title={strategyLabel(strategy)}
                >
                  <span className="text-muted-foreground">{strategy}</span>
                  <span className="font-semibold">{count}</span>
                </span>
              ))}
          </div>
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
            label="Brand quality"
            value={`${dqQ.data.brand_extraction_rate_pct}%`}
            hint={`${dqQ.data.products_with_good_brand.toLocaleString("ru-RU")} из ${dqQ.data.products_total.toLocaleString("ru-RU")} с осмысленным брендом`}
          />
          <StatTile
            label="Cross-3 категорий"
            value={`${dqQ.data.cross_3_count}/${dqQ.data.total_categories}`}
            hint={`Категорий со всеми 3 сайтами (Cross-2: ${dqQ.data.cross_2_count})`}
          />
          <StatTile
            label="Manual matches (7д)"
            value={dqQ.data.manual_matches_last_7d}
            hint="Сколько кластеров создано/привязано вручную за неделю"
          />
          <StatTile
            label="Свежесть scrape"
            value={formatScrapeFreshness(dqQ.data.last_scrape_per_site)}
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
        <h2 className="text-lg font-semibold mb-3">Последние прогоны</h2>
        <p className="text-xs text-muted-foreground mb-2">
          Кликни на строку чтобы увидеть breakdown — сколько товаров на каждом сайте по каждой категории.
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
          <StatusBadge status={run.status} />
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

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "ok"
      ? "bg-success/10 text-success"
      : status === "failed"
        ? "bg-destructive/10 text-destructive"
        : "bg-muted text-muted-foreground";
  return (
    <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${cls}`}>
      {status}
    </span>
  );
}

function strategyLabel(s: string): string {
  switch (s) {
    case "ai_attrs_strict":
      return "AI: совпали активное вещество, дозировка, упаковка и бренд";
    case "ai_attrs_partial":
      return "AI: активное вещество + 1-2 атрибута";
    case "legacy_fuzzy":
      return "Legacy: token_set_ratio по названию";
    case "manual":
      return "Подтверждено вручную";
    default:
      return s;
  }
}

/**
 * Маленькая стат-плитка, легче по визуальному весу чем KpiCard. Используется
 * под основным KPI-grid'ом для дополнительных data-quality метрик чтобы не
 * раздувать главную сетку с 4 до 8 карточек.
 */
function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div
      className="rounded-md border border-border bg-card px-3 py-2"
      title={hint}
    >
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="text-lg font-semibold tabular-nums leading-tight mt-0.5">
        {value}
      </div>
      {hint && (
        <div className="text-[11px] text-muted-foreground/80 mt-0.5 line-clamp-1">
          {hint}
        </div>
      )}
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
