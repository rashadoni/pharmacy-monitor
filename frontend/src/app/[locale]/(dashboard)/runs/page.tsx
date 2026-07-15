"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";
import { api, type RunRow } from "@/lib/api";
import { formatNumber, formatRelative } from "@/lib/utils";
import { runStatusToneClass } from "@/lib/run-quality";

const PAGE_SIZE = 25;
const STATUSES = ["", "running", "ok", "degraded", "failed"];
const SITES = ["", "pharmonline", "aptekonline", "aloe"];

export default function RunsPage() {
  const t = useTranslations("runs");
  const locale = useLocale();
  const [status, setStatus] = useState("");
  const [site, setSite] = useState("");
  const [offset, setOffset] = useState(0);
  const [requestOffset, setRequestOffset] = useState(0);
  const [auditOffset, setAuditOffset] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);

  const runsQ = useQuery({
    queryKey: ["runs-history", status, site, offset],
    queryFn: () => api.runsHistory({ status, site, offset, limit: PAGE_SIZE }),
  });
  const requestsQ = useQuery({
    queryKey: ["scrape-requests-history", requestOffset],
    queryFn: () => api.scrapeRequestsHistory({ offset: requestOffset, limit: PAGE_SIZE }),
  });
  const auditQ = useQuery({
    queryKey: ["audit-log", auditOffset],
    queryFn: () => api.auditLog({ offset: auditOffset, limit: PAGE_SIZE }),
    retry: false,
  });

  const resetFilters = (nextStatus: string, nextSite: string) => {
    setStatus(nextStatus);
    setSite(nextSite);
    setOffset(0);
  };

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight"><Activity className="h-6 w-6" /> {t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>
        <button type="button" onClick={() => void Promise.all([runsQ.refetch(), requestsQ.refetch(), auditQ.refetch()])} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-border px-3 text-sm hover:bg-secondary md:min-h-9"><RefreshCw className="h-4 w-4" /> {t("refresh")}</button>
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FilterSelect label={t("filter_status")} value={status} onChange={(value) => resetFilters(value, site)} options={STATUSES.map((value) => ({ value, label: value ? t(`status_${value}`) : t("all") }))} />
        <FilterSelect label={t("filter_site")} value={site} onChange={(value) => resetFilters(status, value)} options={SITES.map((value) => ({ value, label: value || t("all") }))} />
      </div>

      <section className="space-y-3">
        <div><h2 className="text-lg font-semibold">{t("runs_title")}</h2><p className="text-xs text-muted-foreground">{t("result_count", { count: runsQ.data?.total ?? 0 })}</p></div>
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="min-w-[760px] w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground"><tr><th className="px-3 py-2">ID</th><th className="px-3 py-2">{t("started")}</th><th className="px-3 py-2">{t("status")}</th><th className="px-3 py-2">{t("sites")}</th><th className="px-3 py-2 text-right">{t("products")}</th><th className="px-3 py-2">{t("financial")}</th></tr></thead>
            <tbody>
              {runsQ.data?.items.map((run) => <RunHistoryRow key={run.id} run={run} locale={locale} expanded={expanded === run.id} onToggle={() => setExpanded(expanded === run.id ? null : run.id)} />)}
              {!runsQ.isLoading && runsQ.data?.items.length === 0 && <tr><td colSpan={6} className="px-3 py-8 text-center text-muted-foreground">{t("empty")}</td></tr>}
            </tbody>
          </table>
        </div>
        <Pager offset={offset} total={runsQ.data?.total ?? 0} onChange={setOffset} />
      </section>

      <section className="space-y-3">
        <div><h2 className="text-lg font-semibold">{t("requests_title")}</h2><p className="text-xs text-muted-foreground">{t("requests_desc")}</p></div>
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="min-w-[680px] w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground"><tr><th className="px-3 py-2">ID</th><th className="px-3 py-2">{t("requested")}</th><th className="px-3 py-2">{t("mode")}</th><th className="px-3 py-2">{t("status")}</th><th className="px-3 py-2">Run</th><th className="px-3 py-2">{t("error")}</th></tr></thead>
            <tbody>
              {requestsQ.data?.items.map((request) => <tr key={request.id} className="border-t border-border align-top"><td className="px-3 py-2 font-mono text-xs">#{request.id}</td><td className="px-3 py-2 text-muted-foreground">{formatRelative(request.requested_at, locale)}</td><td className="px-3 py-2">{request.mode}{request.sites ? ` · ${request.sites}` : ""}</td><td className="px-3 py-2"><Status status={request.status} label={t(`status_${request.status}`)} /></td><td className="px-3 py-2 font-mono text-xs">{request.run_id ? `#${request.run_id}` : "—"}</td><td className="max-w-sm px-3 py-2 text-xs text-destructive">{request.error_message || "—"}</td></tr>)}
            </tbody>
          </table>
        </div>
        <Pager offset={requestOffset} total={requestsQ.data?.total ?? 0} onChange={setRequestOffset} />
      </section>

      {auditQ.data && (
        <section className="space-y-3">
          <div><h2 className="text-lg font-semibold">{t("audit_title")}</h2><p className="text-xs text-muted-foreground">{t("audit_desc")}</p></div>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="min-w-[760px] w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground"><tr><th className="px-3 py-2">{t("requested")}</th><th className="px-3 py-2">{t("actor")}</th><th className="px-3 py-2">{t("action")}</th><th className="px-3 py-2">{t("resource")}</th><th className="px-3 py-2">Request ID</th></tr></thead>
              <tbody>{auditQ.data.items.map((row) => <tr key={row.id} className="border-t border-border"><td className="px-3 py-2 text-muted-foreground">{formatRelative(row.created_at, locale)}</td><td className="px-3 py-2">{row.actor_email ?? `#${row.actor_user_id ?? "—"}`}</td><td className="px-3 py-2 font-mono text-xs">{row.action}</td><td className="px-3 py-2 font-mono text-xs">{row.resource}</td><td className="px-3 py-2 font-mono text-[10px] text-muted-foreground">{row.request_id ?? "—"}</td></tr>)}</tbody>
            </table>
          </div>
          <Pager offset={auditOffset} total={auditQ.data.total} onChange={setAuditOffset} />
        </section>
      )}
    </div>
  );
}

function FilterSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: { value: string; label: string }[] }) {
  return <label className="text-sm"><span className="mb-1 block text-xs text-muted-foreground">{label}</span><select value={value} onChange={(event) => onChange(event.target.value)} className="min-h-11 w-full rounded-md border border-input bg-background px-3 md:min-h-9">{options.map((option) => <option key={option.value || "all"} value={option.value}>{option.label}</option>)}</select></label>;
}

function RunHistoryRow({ run, locale, expanded, onToggle }: { run: RunRow; locale: string; expanded: boolean; onToggle: () => void }) {
  const t = useTranslations("runs");
  const detailQ = useQuery({ queryKey: ["run-breakdown", run.id], queryFn: () => api.runBreakdown(run.id), enabled: expanded });
  const sites = Object.keys(run.products_per_site ?? {});
  return <><tr className="cursor-pointer border-t border-border hover:bg-secondary/30" onClick={onToggle}><td className="px-3 py-2 font-mono text-xs">#{run.id}</td><td className="px-3 py-2 text-muted-foreground">{formatRelative(run.started_at, locale)}</td><td className="px-3 py-2"><Status status={run.status} label={t(`status_${run.status}`)} /></td><td className="px-3 py-2">{sites.join(", ") || run.sites_completed || "—"}</td><td className="px-3 py-2 text-right tabular-nums">{formatNumber(run.products_scraped ?? 0, locale)}</td><td className="px-3 py-2 text-xs">{run.run_quality?.financially_eligible ? t("financial_yes") : t("financial_no")}</td></tr>{expanded && <tr className="border-t border-border bg-muted/20"><td colSpan={6} className="px-4 py-3 text-xs">{detailQ.isLoading ? t("loading") : <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]">{JSON.stringify(detailQ.data?.run_quality ?? { products_per_site: detailQ.data?.products_per_site }, null, 2)}</pre>}</td></tr>}</>;
}

function Status({ status, label }: { status: string; label: string }) { return <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${runStatusToneClass(status)}`}>{label}</span>; }

function Pager({ offset, total, onChange }: { offset: number; total: number; onChange: (next: number) => void }) {
  const t = useTranslations("runs");
  if (total <= PAGE_SIZE) return null;
  return <div className="flex items-center justify-between text-sm"><span className="text-muted-foreground">{t("page", { from: offset + 1, to: Math.min(total, offset + PAGE_SIZE), total })}</span><div className="flex gap-2"><button type="button" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))} className="inline-flex min-h-11 items-center rounded-md border border-border px-3 disabled:opacity-40 md:min-h-9"><ChevronLeft className="h-4 w-4" />{t("previous")}</button><button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)} className="inline-flex min-h-11 items-center rounded-md border border-border px-3 disabled:opacity-40 md:min-h-9">{t("next")}<ChevronRight className="h-4 w-4" /></button></div></div>;
}
