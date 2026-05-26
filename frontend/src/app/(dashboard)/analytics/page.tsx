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
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Аналитика</h1>
        <p className="text-sm text-muted-foreground">
          Match quality, brand share, price index, forecast
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
  const { data, isLoading } = useQuery({
    queryKey: ["match-quality"],
    queryFn: api.matchQuality,
  });

  return (
    <Card title="Match Quality">
      {isLoading && <Skeleton />}
      {data && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Total matches" value={data.total_matches} />
          <Stat label="Auto" value={data.auto_matches} sub={`${data.manual_matches} manual`} />
          <Stat label="Coverage" value={`${data.coverage_pct.toFixed(1)}%`}
            sub={`${data.products_matched} / ${data.products_total}`} />
          <Stat label="Rejected pairs" value={data.rejected_pairs} />
        </div>
      )}
    </Card>
  );
}

function BrandShareSection() {
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
    <Card title="Brand Share по сайтам (top 15)">
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
          Брендов с известным распределением пока нет.
        </div>
      )}
    </Card>
  );
}

function PriceIndexSection() {
  const { data, isLoading } = useQuery({
    queryKey: ["price-index"],
    queryFn: () =>
      fetch("/api/v1/dash/price-index", { credentials: "include" }).then((r) => r.json()),
  });

  return (
    <Card title="Price Index по категориям">
      {isLoading && <Skeleton />}
      {data && data.length === 0 && (
        <div className="text-muted-foreground text-center py-8">
          Категорий с подсчитанным индексом пока нет (нужны matched products в каждой).
        </div>
      )}
      {data && data.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-muted-foreground">
            <tr className="border-b border-border">
              <th className="px-3 py-2 text-left">Категория</th>
              <th className="px-3 py-2 text-right">Avg client</th>
              <th className="px-3 py-2 text-right">Avg competitor</th>
              <th className="px-3 py-2 text-right">Index</th>
              <th className="px-3 py-2 text-right">Matched SKU</th>
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
        Товары с самыми большими движениями цены. Отфильтрованы артефакты
        парсинга (изменения вне ±50%).
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
            {mover.site} · {mover.n_points} замеров за 30д · confidence: {mover.confidence}
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
          Прогноз через 7д: <span className="font-mono">{mover.forecast_7d_price.toFixed(2)} ₼</span>
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
