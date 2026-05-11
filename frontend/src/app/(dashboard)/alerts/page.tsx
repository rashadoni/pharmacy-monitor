"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { AlertTriangle, AlertCircle, Info } from "lucide-react";
import { api, type AlertEvent } from "@/lib/api";
import { formatTime } from "@/lib/utils";

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

  const { data, isLoading } = useQuery({
    queryKey: ["alerts", severityFilter],
    queryFn: () => api.alerts(severityFilter || undefined, 100),
  });

  const counts = {
    critical: data?.filter((e) => e.severity === "critical").length ?? 0,
    warning: data?.filter((e) => e.severity === "warning").length ?? 0,
    info: data?.filter((e) => e.severity === "info").length ?? 0,
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🔔 Алерты</h1>
        <p className="text-sm text-muted-foreground">События за последние прогоны</p>
      </div>

      {/* Severity filter chips */}
      <div className="flex gap-2 flex-wrap">
        <Chip
          active={severityFilter === ""}
          onClick={() => setSeverityFilter("")}
          label={`Все${data ? ` (${data.length})` : ""}`}
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
      </div>

      {isLoading && <div className="text-muted-foreground">Загрузка…</div>}
      {data && data.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center">
          Алертов нет. Хорошие новости — pricing на уровне.
        </div>
      )}

      <div className="space-y-2">
        {data?.map((event) => (
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
            <div className="text-xs text-muted-foreground tabular-nums">
              {formatTime(event.created_at)}
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
