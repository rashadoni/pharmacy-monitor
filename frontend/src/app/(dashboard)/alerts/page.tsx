"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCheck,
  Clock,
  Info,
  Inbox,
  Mail,
  MailOpen,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { api, friendlyError, type AlertEvent } from "@/lib/api";
import { formatRelative } from "@/lib/utils";
import { CardListSkeleton } from "@/components/skeleton";
import { OnboardingTip } from "@/components/onboarding-tip";

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

type TabView = "inbox" | "snoozed" | "read";

export default function AlertsPage() {
  const t = useTranslations("alerts");
  const queryClient = useQueryClient();
  const [view, setView] = useState<TabView>("inbox");
  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [hoursWindow, setHoursWindow] = useState<number>(168); // 7 дней по умолчанию
  const [ruleTypeFilter, setRuleTypeFilter] = useState<string>("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  // Backend фильтры по view
  const includeRead = view === "read";
  const includeSnoozed = view === "snoozed";

  const { data, isLoading } = useQuery({
    queryKey: ["alerts", severityFilter, includeRead, includeSnoozed],
    queryFn: () =>
      api.alerts({
        severity: severityFilter || undefined,
        limit: 500,
        include_read: includeRead,
        include_snoozed: includeSnoozed,
      }),
  });

  const countsQ = useQuery({
    queryKey: ["alerts-counts"],
    queryFn: () => api.alertsCounts(),
    staleTime: 30_000,
  });

  // Дополнительные client-side фильтры
  const filtered = useMemo(() => {
    if (!data) return data;
    const cutoff = Date.now() - hoursWindow * 3_600_000;
    return data.filter((e) => {
      if (hoursWindow > 0 && new Date(e.created_at).getTime() < cutoff)
        return false;
      if (ruleTypeFilter && e.rule_type !== ruleTypeFilter) return false;
      // В inbox view фильтруем по semantics:
      // - inbox = !is_read && (!snoozed || snoozed_until <= now)
      // - snoozed = !is_read && snoozed > now
      // - read = is_read
      if (view === "inbox") {
        if (e.is_read) return false;
        if (e.snoozed_until && new Date(e.snoozed_until).getTime() > Date.now())
          return false;
      } else if (view === "snoozed") {
        if (e.is_read) return false;
        if (
          !e.snoozed_until ||
          new Date(e.snoozed_until).getTime() <= Date.now()
        )
          return false;
      } else if (view === "read") {
        if (!e.is_read) return false;
      }
      return true;
    });
  }, [data, hoursWindow, ruleTypeFilter, view]);

  const ruleTypes = useMemo(() => {
    const types = new Set<string>();
    data?.forEach((e) => e.rule_type && types.add(e.rule_type));
    return Array.from(types).sort();
  }, [data]);

  // Mutations
  const markAllReadMutation = useMutation({
    mutationFn: () => api.alertsMarkAllRead(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alerts-counts"] });
      setSelected(new Set());
    },
    onError: (e) => alert(friendlyError(e)),
  });

  const bulkMutation = useMutation({
    mutationFn: ({
      ids,
      action,
    }: {
      ids: number[];
      action:
        | "mark_read"
        | "mark_unread"
        | "snooze_24h"
        | "snooze_7d"
        | "snooze_clear";
    }) => api.alertsBulk(ids, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alerts-counts"] });
      setSelected(new Set());
    },
    onError: (e) => alert(friendlyError(e)),
  });
  const patchMutation = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: number;
      payload: { is_read?: boolean; snooze_hours?: number };
    }) => api.alertPatch(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alerts-counts"] });
    },
    onError: (e) => alert(friendlyError(e)),
  });

  function toggleSel(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleSelAll() {
    if (!filtered) return;
    if (selected.size === filtered.length) setSelected(new Set());
    else setSelected(new Set(filtered.map((e) => e.id)));
  }

  const counts = countsQ.data;

  return (
    <div className="space-y-4">
      <OnboardingTip
        id="alerts-inbox-v1"
        title="Inbox-стиль: tab'ы и bulk-actions"
        description={
          <>
            «Inbox» — то что требует внимания. «Прочитано» / «Отложено» —
            в отдельных tab'ах. Чекбоксы рядом с каждым алертом → выбери
            несколько и используй sticky toolbar сверху для bulk
            mark-read / snooze. Per-row тоже есть «✓ Прочитано» / «🕐 7д».
          </>
        }
      />
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("page_title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("page_subtitle")}
          </p>
        </div>
        {view === "inbox" && counts?.unread != null && counts.unread > 0 && (
          <button
            onClick={() => markAllReadMutation.mutate()}
            disabled={markAllReadMutation.isPending}
            className="shrink-0 inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            <CheckCheck className="h-3.5 w-3.5" />
            {markAllReadMutation.isPending ? "…" : t("mark_all_read_btn", { count: counts.unread })}
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-border">
        <TabBtn
          active={view === "inbox"}
          onClick={() => {
            setView("inbox");
            setSelected(new Set());
          }}
          icon={Inbox}
          label={t("tab_inbox")}
          count={counts?.unread}
        />
        <TabBtn
          active={view === "snoozed"}
          onClick={() => {
            setView("snoozed");
            setSelected(new Set());
          }}
          icon={Clock}
          label={t("tab_snoozed")}
          count={counts?.snoozed}
        />
        <TabBtn
          active={view === "read"}
          onClick={() => {
            setView("read");
            setSelected(new Set());
          }}
          icon={MailOpen}
          label={t("tab_read")}
          count={counts?.read}
        />
      </div>

      {/* Filters */}
      <div className="flex gap-2 flex-wrap items-center">
        <Chip
          active={severityFilter === ""}
          onClick={() => setSeverityFilter("")}
          label={`${t("filter_all")}${data ? ` (${data.length})` : ""}`}
        />
        <Chip
          active={severityFilter === "critical"}
          onClick={() => setSeverityFilter("critical")}
          label={t("filter_critical")}
        />
        <Chip
          active={severityFilter === "warning"}
          onClick={() => setSeverityFilter("warning")}
          label={t("filter_warning")}
        />
        <Chip
          active={severityFilter === "info"}
          onClick={() => setSeverityFilter("info")}
          label={t("filter_info")}
        />
        <div className="ml-auto flex gap-2">
          <select
            value={hoursWindow}
            onChange={(e) => setHoursWindow(Number(e.target.value))}
            className="text-xs rounded-full px-3 py-1 border border-border bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value={24}>{t("window_24h")}</option>
            <option value={72}>{t("window_3d")}</option>
            <option value={168}>{t("window_7d")}</option>
            <option value={720}>{t("window_30d")}</option>
            <option value={0}>{t("window_all")}</option>
          </select>
          <select
            value={ruleTypeFilter}
            onChange={(e) => setRuleTypeFilter(e.target.value)}
            className="text-xs rounded-full px-3 py-1 border border-border bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">{t("filter_all_types")}</option>
            {ruleTypes.map((rt) => (
              <option key={rt} value={rt}>
                {rt}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Bulk toolbar — виден когда есть selected */}
      {selected.size > 0 && (
        <div className="sticky top-0 z-10 rounded-lg border border-primary/40 bg-primary/5 px-3 py-2 flex items-center gap-2 text-sm">
          <span className="font-medium">{t("selected_count", { count: selected.size })}</span>
          {view !== "read" && (
            <button
              onClick={() =>
                bulkMutation.mutate({
                  ids: [...selected],
                  action: "mark_read",
                })
              }
              disabled={bulkMutation.isPending}
              className="inline-flex items-center gap-1 rounded bg-background border border-border px-2 py-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <CheckCheck className="h-3.5 w-3.5" /> {t("action_mark_read")}
            </button>
          )}
          {view === "read" && (
            <button
              onClick={() =>
                bulkMutation.mutate({
                  ids: [...selected],
                  action: "mark_unread",
                })
              }
              disabled={bulkMutation.isPending}
              className="inline-flex items-center gap-1 rounded bg-background border border-border px-2 py-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Mail className="h-3.5 w-3.5" /> {t("action_mark_unread")}
            </button>
          )}
          {view !== "snoozed" && (
            <>
              <button
                onClick={() =>
                  bulkMutation.mutate({
                    ids: [...selected],
                    action: "snooze_24h",
                  })
                }
                disabled={bulkMutation.isPending}
                className="inline-flex items-center gap-1 rounded bg-background border border-border px-2 py-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Clock className="h-3.5 w-3.5" /> {t("action_snooze_24h")}
              </button>
              <button
                onClick={() =>
                  bulkMutation.mutate({
                    ids: [...selected],
                    action: "snooze_7d",
                  })
                }
                disabled={bulkMutation.isPending}
                className="inline-flex items-center gap-1 rounded bg-background border border-border px-2 py-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Clock className="h-3.5 w-3.5" /> {t("action_snooze_7d")}
              </button>
            </>
          )}
          {view === "snoozed" && (
            <button
              onClick={() =>
                bulkMutation.mutate({
                  ids: [...selected],
                  action: "snooze_clear",
                })
              }
              disabled={bulkMutation.isPending}
              className="inline-flex items-center gap-1 rounded bg-background border border-border px-2 py-1 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {t("action_unsnooze")}
            </button>
          )}
          <button
            onClick={() => setSelected(new Set())}
            className="ml-auto text-muted-foreground hover:text-foreground"
          >
            {t("action_deselect")}
          </button>
        </div>
      )}

      {/* Select-all checkbox */}
      {filtered && filtered.length > 0 && (
        <label className="inline-flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={selected.size > 0 && selected.size === filtered.length}
            onChange={toggleSelAll}
            className="h-3.5 w-3.5"
          />
          {t("select_all_on_screen", { count: filtered.length })}
        </label>
      )}

      {isLoading && <CardListSkeleton count={6} />}
      {filtered && filtered.length === 0 && !isLoading && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center">
          {view === "inbox" && t("empty_inbox")}
          {view === "snoozed" && t("empty_snoozed")}
          {view === "read" && t("empty_read")}
        </div>
      )}

      <div className="space-y-2">
        {filtered?.map((event) => (
          <AlertCard
            key={event.id}
            event={event}
            selected={selected.has(event.id)}
            onToggleSel={() => toggleSel(event.id)}
            onMarkRead={() =>
              patchMutation.mutate({
                id: event.id,
                payload: { is_read: !event.is_read },
              })
            }
            onSnooze7d={() =>
              patchMutation.mutate({
                id: event.id,
                payload: { snooze_hours: 24 * 7 },
              })
            }
            onSnoozeClear={() =>
              patchMutation.mutate({
                id: event.id,
                payload: { snooze_hours: 0 },
              })
            }
          />
        ))}
      </div>
    </div>
  );
}

function AlertCard({
  event,
  selected,
  onToggleSel,
  onMarkRead,
  onSnooze7d,
  onSnoozeClear,
}: {
  event: AlertEvent;
  selected: boolean;
  onToggleSel: () => void;
  onMarkRead: () => void;
  onSnooze7d: () => void;
  onSnoozeClear: () => void;
}) {
  const t = useTranslations("alerts");
  const cfg = SEVERITY_CONFIG[event.severity] ?? SEVERITY_CONFIG.info;
  const Icon = cfg.icon;
  const dimmed = event.is_read;
  return (
    <div
      className={`rounded-lg border ${cfg.bg} p-3 ${dimmed ? "opacity-60" : ""}`}
    >
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSel}
          className="h-4 w-4 mt-1 shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={t("aria_select_for_bulk")}
        />
        <Icon className={`h-5 w-5 shrink-0 ${cfg.color} mt-0.5`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className={`font-medium text-sm ${event.is_read ? "line-through" : ""}`}>
              {event.title}
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground tabular-nums">
              {event.snoozed_until &&
                new Date(event.snoozed_until).getTime() > Date.now() && (
                  <span
                    className="inline-flex items-center gap-1 rounded bg-muted/50 px-1.5 py-0.5"
                    title={`Snoozed до ${new Date(event.snoozed_until).toLocaleString("ru-RU")}`}
                  >
                    <Clock className="h-3 w-3" />
                    {t("snoozed_badge")}
                  </span>
                )}
              <span title={event.created_at}>{formatRelative(event.created_at)}</span>
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
          <div className="flex items-center gap-2 mt-2 text-[11px]">
            <button
              onClick={onMarkRead}
              className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded px-1 py-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title={event.is_read ? "Пометить непрочитанным" : "Пометить прочитанным"}
            >
              {event.is_read ? (
                <>
                  <Mail className="h-3 w-3" /> {t("action_mark_unread_short")}
                </>
              ) : (
                <>
                  <CheckCheck className="h-3 w-3" /> {t("action_mark_read_short")}
                </>
              )}
            </button>
            {event.snoozed_until &&
            new Date(event.snoozed_until).getTime() > Date.now() ? (
              <button
                onClick={onSnoozeClear}
                className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded px-1 py-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t("action_unsnooze")}
              </button>
            ) : (
              <button
                onClick={onSnooze7d}
                className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded px-1 py-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                title="Скрыть на 7 дней"
              >
                <Clock className="h-3 w-3" /> {t("snooze_7d_short")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function TabBtn({
  active,
  onClick,
  icon: Icon,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof Inbox;
  label: string;
  count: number | undefined;
}) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        active
          ? "border-primary text-foreground font-medium"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      <Icon className="h-4 w-4" />
      {label}
      {count != null && (
        <span
          className={`text-[10px] tabular-nums font-mono ${
            active ? "text-primary-foreground/80" : "text-muted-foreground"
          }`}
        >
          ({count})
        </span>
      )}
    </button>
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
      className={`text-xs rounded-full px-3 py-1 border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        active
          ? "bg-primary text-primary-foreground border-primary"
          : "bg-card text-muted-foreground border-border hover:bg-secondary"
      }`}
    >
      {label}
    </button>
  );
}
