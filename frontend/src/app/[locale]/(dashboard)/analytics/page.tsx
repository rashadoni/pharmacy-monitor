"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";

const SITE_COLORS: Record<string, string> = {
  pharmonline: "#3b82f6",
  aptekonline: "#22c55e",
  aloe: "#f59e0b",
};

export default function AnalyticsPage() {
  const t = useTranslations("analytics");
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("subtitle")}
        </p>
      </div>

      <MatchQualitySection />
      <BrandShareSection />
      <PriceIndexSection />
      <ForecastSection />
    </div>
  );
}

function MatchQualitySection() {
  const t = useTranslations("analytics");
  const { data, isLoading } = useQuery({
    queryKey: ["match-quality"],
    queryFn: api.matchQuality,
  });

  return (
    <Card title={t("match_quality")}>
      {isLoading && <Skeleton />}
      {data && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label={t("stat_total")} value={data.total_matches} />
          <Stat label={t("stat_auto")} value={data.auto_matches} sub={t("stat_manual", { n: data.manual_matches })} />
          <Stat label={t("stat_coverage")} value={`${data.coverage_pct.toFixed(1)}%`}
            sub={`${data.products_matched} / ${data.products_total}`} />
          <Stat label={t("stat_rejected")} value={data.rejected_pairs} />
        </div>
      )}
    </Card>
  );
}

function BrandShareSection() {
  const t = useTranslations("analytics");
  const { data, isLoading } = useQuery({
    queryKey: ["brand-share"],
    queryFn: () => api.brandShare({ top_n: 15 }),
  });

  const chartData = data?.map((b) => ({
    brand: b.brand,
    pharmonline: b.counts.pharmonline ?? 0,
    aptekonline: b.counts.aptekonline ?? 0,
    aloe: b.counts.aloe ?? 0,
  }));

  return (
    <Card title={t("brand_share")}>
      {isLoading && <Skeleton />}
      {chartData && chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={Math.max(320, chartData.length * 28)}>
          <BarChart data={chartData} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} horizontal={false} />
            <XAxis type="number" fontSize={11} />
            <YAxis dataKey="brand" type="category" width={140} fontSize={11} tick={{ fill: "var(--foreground)" }} />
            <Tooltip formatter={(v: number, name: string) => [v, name]} />
            <Legend />
            <Bar dataKey="pharmonline" stackId="a" fill={SITE_COLORS.pharmonline} />
            <Bar dataKey="aptekonline" stackId="a" fill={SITE_COLORS.aptekonline} />
            <Bar dataKey="aloe" stackId="a" fill={SITE_COLORS.aloe} />
          </BarChart>
        </ResponsiveContainer>
      )}
      {chartData && chartData.length === 0 && (
        <div className="text-muted-foreground text-center py-8">
          {t("no_brand_data")}
        </div>
      )}
    </Card>
  );
}

function PriceIndexSection() {
  const t = useTranslations("analytics");
  const { data, isLoading } = useQuery({
    queryKey: ["price-index"],
    queryFn: () =>
      fetch("/api/v1/dash/price-index", { credentials: "include" }).then((r) => r.json()),
  });

  return (
    <Card title={t("price_index")}>
      {isLoading && <Skeleton />}
      {data && data.length === 0 && (
        <div className="text-muted-foreground text-center py-8">
          {t("no_categories")}
        </div>
      )}
      {data && data.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-muted-foreground">
            <tr className="border-b border-border">
              <th className="px-3 py-2 text-left">{t("th_category")}</th>
              <th className="px-3 py-2 text-right">{t("th_avg_client")}</th>
              <th className="px-3 py-2 text-right">{t("th_avg_competitor")}</th>
              <th className="px-3 py-2 text-right">{t("th_index")}</th>
              <th className="px-3 py-2 text-right">{t("th_matched_sku")}</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row: any, i: number) => (
              <tr key={i} className="border-b border-border last:border-0">
                <td className="px-3 py-2">{row.category}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.avg_client_price?.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.avg_competitor_price?.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums font-semibold">
                  {row.index?.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.matched_skus}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

interface ForecastMover {
  product_id: number;
  site: string;
  name: string;
  n_points: number;
  first_price: number;
  last_price: number;
  change_pct: number;
  direction: "rising" | "falling" | "stable";
  forecast_7d_price: number;
  confidence: "low" | "medium" | "high";
}

function ForecastSection() {
  const t = useTranslations("analytics");
  const { data, isLoading } = useQuery<ForecastMover[]>({
    queryKey: ["forecast"],
    queryFn: () =>
      fetch("/api/v1/dash/forecast/movers", { credentials: "include" }).then((r) => r.json()),
  });

  // Фильтр аномальных движений: ±50% за 30 дней — крайний предел разумного для
  // аптечных товаров. Всё что вышло за этот диапазон практически всегда либо
  // артефакт парсера (concat AZN-bug, e.g. "11.35 AZN 88 AZN" → 1135.88) либо
  // ошибочный matching. Показывать клиенту нет смысла — будет вопросы.
  const clean = (data ?? []).filter(
    (m) => Math.abs(m.change_pct) <= 50 && m.first_price > 0 && m.last_price > 0,
  );

  // Если данных вовсе нет — не рендерим раздел, чтобы не было placeholder'а
  // «появится через N дней». Forecast вернётся когда наберётся ≥3 прогона.
  if (!isLoading && clean.length === 0) {
    return null;
  }

  return (
    <Card title={t("forecast_section_title")}>
      <div className="text-xs text-muted-foreground mb-3">
        {t("forecast_desc")}
      </div>
      {isLoading && <Skeleton />}
      {clean.length > 0 && (
        <div className="space-y-2">
          {clean.slice(0, 10).map((m) => (
            <ForecastRow key={m.product_id} mover={m} />
          ))}
        </div>
      )}
    </Card>
  );
}

function ForecastRow({ mover }: { mover: ForecastMover }) {
  const t = useTranslations("analytics");
  const dirColor =
    mover.direction === "falling"
      ? "text-destructive"
      : mover.direction === "rising"
        ? "text-success"
        : "text-muted-foreground";
  const arrow =
    mover.direction === "falling" ? "↓" : mover.direction === "rising" ? "↑" : "→";

  return (
    <div className="rounded-md border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium truncate" title={mover.name}>
            {mover.name}
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {t("forecast_row_meta", { site: mover.site, n: mover.n_points, conf: mover.confidence })}
          </div>
        </div>
        <div className={`text-right shrink-0 ${dirColor}`}>
          <div className="text-base font-semibold tabular-nums">
            {arrow} {mover.change_pct.toFixed(1)}%
          </div>
          <div className="text-xs text-muted-foreground tabular-nums">
            {mover.first_price.toFixed(2)} → {mover.last_price.toFixed(2)} ₼
          </div>
        </div>
      </div>
      {mover.forecast_7d_price > 0 && (
        <div className="text-xs text-muted-foreground mt-1.5 pt-1.5 border-t border-border/50">
          {t("forecast_7d")} <span className="font-mono">{mover.forecast_7d_price.toFixed(2)} ₼</span>
        </div>
      )}
    </div>
  );
}

// ─── helpers ────────────────────────────────────────────────────────────────

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 md:p-6">
      <h2 className="font-semibold mb-4">{title}</h2>
      {children}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <div>
      <div className="text-xs text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular-nums">{value}</div>
      {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="animate-pulse space-y-2">
      <div className="h-4 bg-muted rounded w-full"></div>
      <div className="h-4 bg-muted rounded w-3/4"></div>
    </div>
  );
}
