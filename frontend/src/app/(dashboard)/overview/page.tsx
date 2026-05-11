"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type RoiAction } from "@/lib/api";
import { formatPrice } from "@/lib/utils";

export default function OverviewPage() {
  const matchQ = useQuery({ queryKey: ["match-quality"], queryFn: api.matchQuality });
  const actionsQ = useQuery({ queryKey: ["roi-actions"], queryFn: api.roiActions });
  const runsQ = useQuery({ queryKey: ["runs"], queryFn: () => api.runs(5) });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Обзор</h1>
        <p className="text-sm text-muted-foreground">
          Сегодняшние действия и ключевые метрики
        </p>
      </div>

      {/* KPI cards */}
      <div className="grid gap-4 grid-cols-2 md:grid-cols-4">
        <KpiCard
          label="Cross-site matches"
          value={matchQ.data?.total_matches ?? "—"}
          loading={matchQ.isLoading}
        />
        <KpiCard
          label="Coverage"
          value={
            matchQ.data ? `${matchQ.data.coverage_pct.toFixed(1)}%` : "—"
          }
          loading={matchQ.isLoading}
        />
        <KpiCard
          label="Products"
          value={matchQ.data?.products_total ?? "—"}
          loading={matchQ.isLoading}
        />
        <KpiCard
          label="Manual matches"
          value={matchQ.data?.manual_matches ?? "—"}
          loading={matchQ.isLoading}
        />
      </div>

      {/* Today's actions */}
      <div>
        <h2 className="text-lg font-semibold mb-3">Сегодняшние действия</h2>
        {actionsQ.isLoading && <div className="text-muted-foreground">Загрузка…</div>}
        {actionsQ.data && actionsQ.data.length === 0 && (
          <div className="text-muted-foreground">Нет рекомендаций — pricing на уровне.</div>
        )}
        <div className="space-y-2">
          {actionsQ.data?.slice(0, 10).map((a, i) => (
            <ActionRow key={i} action={a} />
          ))}
        </div>
      </div>

      {/* Recent runs */}
      <div>
        <h2 className="text-lg font-semibold mb-3">Последние прогоны</h2>
        <div className="rounded-lg border border-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">ID</th>
                <th className="px-3 py-2 text-left">Started</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-right">Products</th>
                <th className="px-3 py-2 text-left hidden md:table-cell">Sites</th>
              </tr>
            </thead>
            <tbody>
              {runsQ.data?.map((r) => (
                <tr key={r.id} className="border-t border-border">
                  <td className="px-3 py-2 font-mono text-xs">{r.id}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {r.started_at?.slice(0, 16).replace("T", " ")}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.products_scraped}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground hidden md:table-cell">
                    {r.sites_completed}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function KpiCard({
  label,
  value,
  loading,
}: {
  label: string;
  value: number | string;
  loading: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular-nums">
        {loading ? "…" : value}
      </div>
    </div>
  );
}

function ActionRow({ action }: { action: RoiAction }) {
  const tone =
    action.severity === "critical"
      ? "border-destructive/40 bg-destructive/5"
      : action.severity === "warning"
        ? "border-warning/40 bg-warning/5"
        : action.severity === "opportunity"
          ? "border-success/40 bg-success/5"
          : "border-border bg-card";
  const showImpact =
    action.estimated_monthly_impact_azn != null &&
    Math.abs(action.estimated_monthly_impact_azn) < 100_000; // hide bug-prices
  return (
    <div className={`rounded-lg border ${tone} p-3`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm">{action.title}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{action.detail}</div>
        </div>
        {showImpact && (
          <div className="text-sm font-semibold tabular-nums shrink-0">
            {formatPrice(action.estimated_monthly_impact_azn)}/мес
          </div>
        )}
      </div>
    </div>
  );
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
