"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useState, useEffect, useRef } from "react";
import { Save, Upload, CheckCircle2, AlertCircle, ArrowLeft } from "lucide-react";

import { api, friendlyError, type PricingConfig, type CostImportResult } from "@/lib/api";
import { formatClock } from "@/lib/utils";

/**
 * Phase 4.1 + 4.3 + 4.6 — Pricing intelligence settings.
 *
 * Two sections on one page:
 *   1. Thresholds form — raise_pct, undercut_pct, max_spread_pct, min_margin_pct, max_per_type.
 *      Updates `pricing_config` table; invalidates ROI cache so next /roi/actions
 *      re-computes.
 *   2. Cost CSV upload — POST to /api/v1/dash/settings/costs/import. Imports
 *      purchase prices into `supplier_prices`; margin-aware undercut logic
 *      uses these via inventory.get_min_purchase_price.
 */
export default function PricingSettingsPage() {
  const t = useTranslations("pricing");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const qc = useQueryClient();

  const cfgQ = useQuery({ queryKey: ["pricing-config"], queryFn: api.pricingGet });

  // Local form state — initialised from server response.
  const [form, setForm] = useState<PricingConfig | null>(null);
  useEffect(() => {
    if (cfgQ.data && !form) setForm(cfgQ.data);
  }, [cfgQ.data, form]);

  const updateM = useMutation({
    mutationFn: (cfg: PricingConfig) => api.pricingUpdate(cfg),
    onSuccess: (data) => {
      setForm(data);
      qc.invalidateQueries({ queryKey: ["pricing-config"] });
      qc.invalidateQueries({ queryKey: ["roi-actions"] });
    },
  });

  function setField<K extends keyof PricingConfig>(key: K, value: PricingConfig[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
  }

  // CSV upload state
  const fileRef = useRef<HTMLInputElement>(null);
  const [importResult, setImportResult] = useState<CostImportResult | null>(null);
  const importM = useMutation({
    mutationFn: (file: File) => api.costsCsvImport(file),
    onSuccess: (data) => {
      setImportResult(data);
      qc.invalidateQueries({ queryKey: ["roi-actions"] });
    },
  });

  return (
    <div className="space-y-6 max-w-3xl">
      <header className="flex items-start justify-between gap-3">
        <div>
          <Link
            href="/settings"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors mb-2"
          >
            <ArrowLeft className="h-3 w-3" /> {t("back_to_settings")}
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("subtitle")}</p>
        </div>
      </header>

      {cfgQ.isLoading && <p className="text-sm text-muted-foreground">{tCommon("loading")}</p>}
      {cfgQ.error && (
        <p className="text-sm text-destructive">{friendlyError(cfgQ.error, locale)}</p>
      )}

      {/* === Thresholds form === */}
      {form && (
        <section className="rounded-lg border border-border bg-card p-5 space-y-4">
          <h2 className="text-base font-semibold">{t("thresholds_title")}</h2>
          <p className="text-xs text-muted-foreground">{t("thresholds_desc")}</p>

          <ThresholdRow
            label={t("raise_label")}
            hint={t("raise_hint")}
            value={form.raise_threshold_pct}
            onChange={(v) => setField("raise_threshold_pct", v)}
            min={0} max={50} step={0.5}
            suffix="%"
          />
          <ThresholdRow
            label={t("undercut_label")}
            hint={t("undercut_hint")}
            value={form.undercut_threshold_pct}
            onChange={(v) => setField("undercut_threshold_pct", v)}
            min={0} max={50} step={0.5}
            suffix="%"
          />
          <ThresholdRow
            label={t("max_spread_label")}
            hint={t("max_spread_hint")}
            value={form.max_spread_pct}
            onChange={(v) => setField("max_spread_pct", v)}
            min={20} max={100} step={5}
            suffix="%"
          />
          <ThresholdRow
            label={t("min_margin_label")}
            hint={t("min_margin_hint")}
            value={form.min_margin_pct}
            onChange={(v) => setField("min_margin_pct", v)}
            min={0} max={50} step={1}
            suffix="%"
          />
          <ThresholdRow
            label={t("max_per_type_label")}
            hint={t("max_per_type_hint")}
            value={form.max_per_type}
            onChange={(v) => setField("max_per_type", Math.round(v))}
            min={1} max={50} step={1}
            suffix=""
          />

          <div className="flex items-center gap-3 pt-2 border-t border-border">
            <button
              onClick={() => form && updateM.mutate(form)}
              disabled={updateM.isPending}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              <Save className="h-4 w-4" />
              {updateM.isPending ? tCommon("save_view") : t("save_btn")}
            </button>
            {updateM.isSuccess && (
              <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                <CheckCircle2 className="h-3 w-3" />{" "}
                {t("saved_at", { time: formatClock(new Date(), locale) })}
              </span>
            )}
            {updateM.error && (
              <span className="text-xs text-destructive">
                {friendlyError(updateM.error, locale)}
              </span>
            )}
          </div>
        </section>
      )}

      {/* === CSV upload === */}
      <section className="rounded-lg border border-border bg-card p-5 space-y-4">
        <h2 className="text-base font-semibold">{t("costs_title")}</h2>
        <p className="text-xs text-muted-foreground whitespace-pre-line">{t("costs_desc")}</p>
        <pre className="text-[11px] bg-secondary p-3 rounded-md overflow-x-auto font-mono">
{`sku,supplier_name,purchase_price,currency,name
PRODUCT-001,Vendor A,2.50,AZN,Aspirin 500mg
PRODUCT-002,Vendor A,12.40,AZN,Lipanthyl 100mg
PRODUCT-003,Vendor B,3.10,AZN,Diazolin 100mg N10`}
        </pre>

        <div className="flex items-center gap-3">
          <input
            type="file"
            accept=".csv,text/csv"
            ref={fileRef}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) importM.mutate(f);
            }}
            className="text-sm file:mr-3 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-primary file:text-primary-foreground hover:file:bg-primary/90 file:cursor-pointer"
            disabled={importM.isPending}
          />
          {importM.isPending && <span className="text-xs text-muted-foreground">{t("uploading")}</span>}
        </div>

        {importM.error && (
          <p className="text-sm text-destructive flex items-start gap-2">
            <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
            {friendlyError(importM.error, locale)}
          </p>
        )}

        {importResult && (
          <div className="rounded-md border border-border bg-secondary/50 p-3 space-y-2 text-sm">
            <div className="flex items-center gap-2 font-medium">
              <CheckCircle2 className="h-4 w-4 text-green-500" />
              {t("import_result", {
                imported: importResult.rows_imported,
                processed: importResult.rows_processed,
              })}
            </div>
            {importResult.rows_skipped > 0 && (
              <div className="text-xs text-amber-700 dark:text-amber-400">
                {t("skipped_count", { n: importResult.rows_skipped })}
              </div>
            )}
            {importResult.errors.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                  {t("show_errors", { n: importResult.errors.length })}
                </summary>
                <ul className="mt-1 space-y-0.5 list-disc list-inside text-muted-foreground">
                  {importResult.errors.map((e, i) => (
                    <li key={i} className="font-mono">{e}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

function ThresholdRow({
  label, hint, value, onChange, min, max, step, suffix,
}: {
  label: string; hint: string;
  value: number; onChange: (v: number) => void;
  min: number; max: number; step: number;
  suffix: string;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3 mb-1.5">
        <label className="text-sm font-medium">{label}</label>
        <span className="font-mono text-sm tabular-nums">
          {value}{suffix}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-primary"
      />
      <p className="text-xs text-muted-foreground mt-1">{hint}</p>
    </div>
  );
}
