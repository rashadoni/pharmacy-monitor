"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, CheckCircle2, AlertCircle, AlertTriangle } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";
import {
  api,
  friendlyError,
  isVerifiedScanPendingError,
  type RunBreakdown,
  type RunRow,
  type RoiAction,
  type HealthSite,
  type LatestRunBySite,
} from "@/lib/api";
import { OnboardingTip } from "@/components/onboarding-tip";
import { QuickActions } from "@/components/quick-actions";
import { formatNumber, formatRelative, formatPrice } from "@/lib/utils";
import { categoryDisplayLabel } from "@/lib/category-label";
import { runStatusToneClass } from "@/lib/run-quality";

export default function OverviewPage() {
  const t = useTranslations("overview");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const matchQ = useQuery({ queryKey: ["match-quality"], queryFn: api.matchQuality });
  const normalizeQ = useQuery({ queryKey: ["normalize-stats"], queryFn: api.normalizeStats });
  const recommendationsQ = useQuery({
    queryKey: ["roi-recommendations", "pharmonline", locale],
    queryFn: () => api.roiRecommendations(undefined, locale),
  });
  const runsQ = useQuery({ queryKey: ["runs"], queryFn: () => api.runs(5) });
  const latestBySiteQ = useQuery({
    queryKey: ["runs-latest-by-site"],
    queryFn: api.runsLatestBySite,
  });
  // Phase 5.6: live staleness panel. Refresh every 60s automatically.
  const healthQ = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const [expandedRun, setExpandedRun] = useState<number | null>(null);

  return (
    <div className="space-y-6">
      <OnboardingTip
        id="overview-welcome-v1"
        title={t("onboarding_title")}
        description={t("onboarding_desc")}
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

      {/* Phase 5.6 — Per-site staleness panel */}
      {healthQ.data && <SiteStalenessPanel sites={healthQ.data.sites} />}

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
        <KpiCard
          label={t("kpi_price_review")}
          value={
            normalizeQ.data
              ? `${(
                  (normalizeQ.data.products_needing_review /
                    Math.max(1, normalizeQ.data.products_total)) *
                  100
                ).toFixed(1)}%`
              : "—"
          }
          loading={normalizeQ.isLoading}
          hint={
            normalizeQ.data
              ? t("kpi_price_review_hint", {
                  count: formatNumber(normalizeQ.data.products_needing_review, locale),
                  total: formatNumber(normalizeQ.data.products_total, locale),
                })
              : t("kpi_price_review_empty")
          }
        />
      </div>

      {/* Today's actions */}
      <div>
        <h2 className="text-lg font-semibold mb-3">{t("today_actions")}</h2>
        {recommendationsQ.data?.provenance.run_id != null && (
          <p className="mb-3 text-xs text-muted-foreground">
            {t("recommendations_provenance", {
              run: recommendationsQ.data.provenance.run_id,
              completed: formatRelative(recommendationsQ.data.provenance.run_finished_at, locale),
            })}
          </p>
        )}
        {recommendationsQ.isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}
        {recommendationsQ.isError && (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-3 text-sm text-warning">
            {isVerifiedScanPendingError(recommendationsQ.error)
              ? t("recommendations_waiting_verified")
              : friendlyError(recommendationsQ.error, locale)}
          </div>
        )}
        {recommendationsQ.data && recommendationsQ.data.items.length === 0 && (
          <div className="text-muted-foreground">{t("no_actions")}</div>
        )}
        <div className="space-y-2">
          {recommendationsQ.data?.items.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </div>

      <LatestRunsBySitePanel items={latestBySiteQ.data ?? []} loading={latestBySiteQ.isLoading} />

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

function LatestRunsBySitePanel({
  items,
  loading,
}: {
  items: LatestRunBySite[];
  loading: boolean;
}) {
  const t = useTranslations("overview");
  const tCommon = useTranslations("common");

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">{t("latest_by_site")}</h2>
      <p className="text-xs text-muted-foreground mb-2">{t("latest_by_site_desc")}</p>
      {loading ? (
        <div className="text-sm text-muted-foreground">{tCommon("loading")}</div>
      ) : (
        <div className="rounded-lg border border-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">{t("th_sites")}</th>
                <th className="px-3 py-2 text-left">{t("th_id")}</th>
                <th className="px-3 py-2 text-left">{t("th_started")}</th>
                <th className="px-3 py-2 text-left hidden sm:table-cell">{t("th_duration")}</th>
                <th className="px-3 py-2 text-left">{t("th_status")}</th>
                <th className="px-3 py-2 text-right">{t("th_products")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const run = item.run;
                const siteProducts = run?.products_per_site?.[item.site] ?? run?.products_scraped ?? null;
                return (
                  <tr key={item.site} className="border-t border-border">
                    <td className="px-3 py-2 font-medium">{item.site}</td>
                    <td className="px-3 py-2 font-mono text-xs">{run?.id ?? "—"}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {run?.started_at ? run.started_at.slice(0, 16).replace("T", " ") : "—"}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground hidden sm:table-cell tabular-nums">
                      {run ? formatDuration(run.started_at, run.finished_at, t) : "—"}
                    </td>
                    <td className="px-3 py-2">
                      {run ? <StatusBadge status={run.status} /> : <span className="text-muted-foreground">—</span>}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {siteProducts == null ? "—" : siteProducts}
                    </td>
                  </tr>
                );
              })}
              {items.length === 0 && (
                <tr className="border-t border-border">
                  <td colSpan={6} className="px-3 py-4 text-center text-muted-foreground">
                    {t("no_data")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
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
          {formatDuration(run.started_at, run.finished_at, t)}
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
          <td colSpan={7} className="px-3 py-3">
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
}: { data: RunBreakdown }) {
  const t = useTranslations("overview");
  const locale = useLocale();
  // Load categories один раз — нужно для локализованного маппинга slug → label.
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
    return match ? categoryDisplayLabel(match, locale, slugOrId) : null;
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
      {data.run_quality && (
        <div className="rounded-md border border-warning/40 bg-warning/5 p-3 text-xs">
          <div className="font-medium text-sm">{t("quality_title")}</div>
          <div className="mt-1 text-muted-foreground">
            {t("quality_mode", { mode: data.run_quality.mode })} ·{" "}
            {data.run_quality.financially_eligible
              ? t("quality_financial_yes")
              : t("quality_financial_no")}
          </div>
          <div className="mt-2 grid gap-2 md:grid-cols-2 lg:grid-cols-3">
            {Object.entries(data.run_quality.sites).map(([site, quality]) => (
              <div key={site} className="rounded border border-border bg-card px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{site}</span>
                  <StatusBadge status={quality.status} />
                </div>
                <div className="mt-1 text-muted-foreground">
                  {t("quality_items", {
                    completed: quality.items_completed,
                    expected: quality.items_expected,
                    failed: quality.items_failed,
                  })}
                </div>
                {quality.reasons.length > 0 && (
                  <div className="mt-1 font-mono text-[10px] text-warning">
                    {quality.reasons.join(", ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
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
      <div className="text-sm font-medium text-muted-foreground">{label}</div>
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
  const locale = useLocale();
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
              title={t("unit_gap_hint")}
            >
              {gapPositive ? "+" : ""}
              {formatPrice(action.unit_gap_azn ?? 0, locale)} {t("unit_gap_label")}
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
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  if (!startedAt) return "—";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return t("duration_sec", { n: seconds });
  const mins = Math.floor(seconds / 60);
  if (mins < 60) {
    const s = seconds % 60;
    return s > 0 ? t("duration_min_sec", { n: mins, s }) : t("duration_min", { n: mins });
  }
  const hours = Math.floor(mins / 60);
  const m = mins % 60;
  return t("duration_hour", { h: hours, m });
}

function StatusBadge({ status }: { status: string }) {
  const t = useTranslations("overview");
  const translated = ["ok", "running", "degraded", "failed"].includes(status)
    ? t(`status_${status}` as "status_ok")
    : status;
  return (
    <span
      className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${runStatusToneClass(status)}`}
    >
      {translated}
    </span>
  );
}

/** Phase 5.6 — Per-site freshness panel using /health staleness data. */
function SiteStalenessPanel({ sites }: { sites: HealthSite[] }) {
  const t = useTranslations("staleness");
  if (!sites.length) return null;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-muted-foreground">
          {t("title")}
        </h2>
        <span className="text-xs text-muted-foreground">{t("auto_refresh")}</span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {sites.map((s) => (
          <SiteStalenessCell key={s.site} site={s} />
        ))}
      </div>
    </div>
  );
}

function SiteStalenessCell({ site }: { site: HealthSite }) {
  const t = useTranslations("staleness");
  const hours = site.hours_since;
  const maxAgeHours = site.max_age_hours;
  const warningAfterHours = maxAgeHours <= 30 ? 8 : maxAgeHours * 0.75;
  // Threshold rules:
  //   green:  comfortably inside the site's scrape cadence
  //   yellow: near the backend staleness threshold
  //   red:    beyond the backend staleness threshold, or no data
  let tone: "green" | "yellow" | "red" = "green";
  let Icon = CheckCircle2;
  if (hours === null) {
    tone = "red";
    Icon = AlertCircle;
  } else if (hours > maxAgeHours) {
    tone = "red";
    Icon = AlertCircle;
  } else if (hours >= warningAfterHours) {
    tone = "yellow";
    Icon = AlertTriangle;
  }
  const toneClasses = {
    green: "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20",
    yellow: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20",
    red: "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20",
  };

  const ageText =
    hours === null
      ? t("no_data")
      : hours < 1
      ? t("minutes_ago", { n: Math.round(hours * 60) })
      : hours < 24
      ? t("hours_ago", { n: Math.round(hours) })
      : t("days_ago", { n: Math.round(hours / 24) });

  return (
    <div className={`flex items-center gap-3 rounded-md border p-3 ${toneClasses[tone]}`}>
      <Icon className="h-5 w-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="font-medium text-sm truncate">{site.site}.az</div>
        <div className="text-xs opacity-80">{ageText}</div>
      </div>
    </div>
  );
}
