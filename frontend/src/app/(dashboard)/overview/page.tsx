"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { api, type RunRow, type RoiAction } from "@/lib/api";
import { OnboardingTip } from "@/components/onboarding-tip";
import { QuickActions } from "@/components/quick-actions";
import { formatRelative, formatPrice } from "@/lib/utils";

export default function OverviewPage() {
  const t = useTranslations("overview");
  const tCommon = useTranslations("common");
  const matchQ = useQuery({ queryKey: ["match-quality"], queryFn: api.matchQuality });
  const normalizeQ = useQuery({ queryKey: ["normalize-stats"], queryFn: api.normalizeStats });
  const actionsQ = useQuery({ queryKey: ["roi-actions"], queryFn: () => api.roiActions() });
  const runsQ = useQuery({ queryKey: ["runs"], queryFn: () => api.runs(5) });
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
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("subtitle")}
          </p>
        </div>
        <QuickActions />
      </div>

      {/* KPI cards */}
      <div className="grid gap-4 grid-cols-2 md:grid-cols-4">
        <KpiCard
          label={t("kpi_matches")}
          value={matchQ.data?.total_matches ?? "—"}
          loading={matchQ.isLoading}
        />
        <KpiCard
          label={t("kpi_coverage")}
          value={
            matchQ.data ? `${matchQ.data.coverage_pct.toFixed(1)}%` : "—"
          }
          loading={matchQ.isLoading}
        />
        <KpiCard
          label={t("kpi_products")}
          value={matchQ.data?.products_total ?? "—"}
          loading={matchQ.isLoading}
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
          label={t("kpi_ai_confidence")}
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
              ? t("kpi_ai_low_confidence", {
                  count: normalizeQ.data.needs_review.toLocaleString("ru-RU"),
                  total: normalizeQ.data.products_total.toLocaleString("ru-RU"),
                })
              : t("kpi_ai_hint")
          }
        />
      </div>

      {/* Today's actions */}
      <div>
        <h2 className="text-lg font-semibold mb-3">{t("today_actions")}</h2>
        {actionsQ.isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}
        {actionsQ.data && actionsQ.data.length === 0 && (
          <div className="text-muted-foreground">{t("no_actions")}</div>
        )}
        <div className="space-y-2">
          {actionsQ.data?.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </div>

      {/* Recent runs */}
      <div>
        <h2 className="text-lg font-semibold mb-3">{t("recent_runs")}</h2>
        <p className="text-xs text-muted-foreground mb-2">
          {t("runs_desc")}
        </p>
        <div className="rounded-lg border border-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th className="px-3 py-2 w-6"></th>
                <th className="px-3 py-2 text-left">{t("th_id")}</th>
                <th className="px-3 py-2 text-left">{t("th_started")}</th>
                <th className="px-3 py-2 text-left hidden sm:table-cell">{t("th_duration")}</th>
                <th className="px-3 py-2 text-left">{t("th_status")}</th>
                <th className="px-3 py-2 text-right">{t("th_products")}</th>
                <th className="px-3 py-2 text-left hidden md:table-cell">{t("th_sites")}</th>
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
  const t = useTranslations("overview");
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
        <td className="px-3 py-2 text-muted-foreground">
          {run.started_at?.slice(0, 16).replace("T", " ")}
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
              <div className="text-xs text-muted-foreground">{t("loading_breakdown")}</div>
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
  const t = useTranslations("overview");
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
        {t("breakdown_legacy")}
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
            <span className="text-muted-foreground">{t("total_label")}</span>
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
                    {t("no_data")}
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

function KpiCard({
  label,
  value,
  loading,
  hint,
}: {
  label: string;
  value: number | string;
  loading: boolean;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular-nums">
        {loading ? "…" : value}
      </div>
      {hint && (
        <div className="text-xs text-muted-foreground mt-1">{hint}</div>
      )}
    </div>
  );
}

function ActionRow({ action }: { action: RoiAction }) {
  const t = useTranslations("overview");
  const tone =
    action.severity === "critical"
      ? "border-destructive/40 bg-destructive/5"
      : action.severity === "warning"
        ? "border-warning/40 bg-warning/5"
        : action.severity === "opportunity"
          ? "border-success/40 bg-success/5"
          : "border-border bg-card";
  // Показываем РЕАЛЬНЫЕ цифры: разница на единицу + % спред.
  // Раньше тут было «{impact}/мес», но impact = unit_gap × 30 (placeholder
  // volume без основания) — вводило в заблуждение. Объёмов продаж у нас нет.
  const hasGap = action.unit_gap_azn != null && action.spread_pct != null;
  const gapPositive = (action.unit_gap_azn ?? 0) > 0;
  return (
    <div className={`rounded-lg border ${tone} p-3`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm">{action.title}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{action.detail}</div>
        </div>
        {hasGap && (
          <div className="shrink-0 text-right">
            <div
              className={`text-sm font-semibold tabular-nums ${
                gapPositive ? "text-success" : "text-destructive"
              }`}
              title="Разница цены за единицу товара — реально проверяемая величина"
            >
              {gapPositive ? "+" : ""}
              {formatPrice(action.unit_gap_azn ?? 0)} {t("unit_gap_label")}
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {(action.spread_pct ?? 0).toFixed(1)} {t("spread_label")}
            </div>
          </div>
        )}
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
