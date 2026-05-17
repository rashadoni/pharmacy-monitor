"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AlertTriangle, AlertCircle, Info } from "lucide-react";
import { api, type AlertEvent } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

const SEVERITY_CONFIG = {
  critical: {
    icon: AlertCircle,
    color: "text-destructive",
    bg: "bg-destructive/5 border-destructive/30",
    label: "Critical",
  },
  warning: {
    icon: AlertTriangle,
    color: "text-warning",
    bg: "bg-warning/5 border-warning/30",
    label: "Warning",
  },
  info: {
    icon: Info,
    color: "text-muted-foreground",
    bg: "bg-card border-border",
    label: "Info",
  },
} as const;

export default function AlertsPage() {
  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [hoursWindow, setHoursWindow] = useState<number>(24);
  const [ruleTypeFilter, setRuleTypeFilter] = useState<string>("");

  const { data, isLoading } = useQuery({
    queryKey: ["alerts", severityFilter],
    queryFn: () => api.alerts(severityFilter || undefined, 500),
  });

  const filtered = useMemo(() => {
    if (!data) return data;
    const cutoff = Date.now() - hoursWindow * 3_600_000;
    return data.filter((e) => {
      if (hoursWindow > 0 && new Date(e.created_at).getTime() < cutoff)
        return false;
      if (ruleTypeFilter && e.rule_type !== ruleTypeFilter) return false;
      return true;
    });
  }, [data, hoursWindow, ruleTypeFilter]);

  const counts = {
    critical: filtered?.filter((e) => e.severity === "critical").length ?? 0,
    warning: filtered?.filter((e) => e.severity === "warning").length ?? 0,
    info: filtered?.filter((e) => e.severity === "info").length ?? 0,
  };

  // Уникальные типы правил во всём наборе (не filtered, чтобы dropdown был стабильным)
  const ruleTypes = useMemo(() => {
    const types = new Set<string>();
    data?.forEach((e) => e.rule_type && types.add(e.rule_type));
    return Array.from(types).sort();
  }, [data]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Алерты</h1>
        <p className="text-sm text-muted-foreground">События за последние прогоны</p>
      </div>

      {/* Filters: severity chips + date window + rule type */}
      <div className="flex gap-2 flex-wrap items-center">
        <Chip
          active={severityFilter === ""}
          onClick={() => setSeverityFilter("")}
          label={`Все${filtered ? ` (${filtered.length})` : ""}`}
        />
        <Chip
          active={severityFilter === "critical"}
          onClick={() => setSeverityFilter("critical")}
          label={`🔴 Critical (${counts.critical})`}
        />
        <Chip
          active={severityFilter === "warning"}
          onClick={() => setSeverityFilter("warning")}
          label={`🟡 Warning (${counts.warning})`}
        />
        <Chip
          active={severityFilter === "info"}
          onClick={() => setSeverityFilter("info")}
          label={`ℹ️ Info (${counts.info})`}
        />
        <div className="ml-auto flex gap-2">
          <select
            value={hoursWindow}
            onChange={(e) => setHoursWindow(Number(e.target.value))}
            className="text-xs rounded-full px-3 py-1 border border-border bg-card"
          >
            <option value={24}>24 часа</option>
            <option value={72}>3 дня</option>
            <option value={168}>7 дней</option>
            <option value={720}>30 дней</option>
            <option value={0}>Всё время</option>
          </select>
          <select
            value={ruleTypeFilter}
            onChange={(e) => setRuleTypeFilter(e.target.value)}
            className="text-xs rounded-full px-3 py-1 border border-border bg-card"
          >
            <option value="">Все типы</option>
            {ruleTypes.map((rt) => (
              <option key={rt} value={rt}>
                {rt}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isLoading && <div className="text-muted-foreground">Загрузка…</div>}
      {filtered && filtered.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center">
          По фильтрам ничего нет. Расширьте окно времени или сбросьте severity.
        </div>
      )}

      <div className="space-y-2">
        {filtered?.map((event) => (
          <AlertCard key={event.id} event={event} />
        ))}
      </div>
    </div>
  );
}

function AlertCard({ event }: { event: AlertEvent }) {
  const cfg = SEVERITY_CONFIG[event.severity] ?? SEVERITY_CONFIG.info;
  const Icon = cfg.icon;
  return (
    <div className={`rounded-lg border ${cfg.bg} p-3`}>
      <div className="flex items-start gap-3">
        <Icon className={`h-5 w-5 shrink-0 ${cfg.color} mt-0.5`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className="font-medium text-sm">{event.title}</div>
            <div
              className="text-xs text-muted-foreground tabular-nums"
              title={event.created_at}
            >
              {formatRelative(event.created_at)}
            </div>
          </div>
          {event.detail && (
            <div className="text-xs text-muted-foreground mt-1">{event.detail}</div>
          )}
          {event.rule_type && (
            <div className="text-[10px] text-muted-foreground/70 mt-1 uppercase tracking-wide">
              {event.rule_type}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Chip({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-xs rounded-full px-3 py-1 border transition-colors ${
        active
          ? "bg-primary text-primary-foreground border-primary"
          : "bg-card text-muted-foreground border-border hover:bg-secondary"
      }`}
    >
      {label}
    </button>
  );
}
