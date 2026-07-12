"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";
import { CheckCircle2, ExternalLink, XCircle, AlertTriangle } from "lucide-react";

import { api, friendlyError, type MatchSuggestion } from "@/lib/api";
import { formatPrice } from "@/lib/utils";

/**
 * Phase 2.5 — Match Suggestion Review Queue.
 *
 * Lists borderline matches (confidence < threshold OR needs_review flag)
 * so a human can confirm/reject one-click. Goal: drive false-match rate
 * <2% by cleaning the tail that auto-matcher couldn't resolve confidently.
 *
 * Endpoint: GET /api/v1/dash/matches/suggestions
 * Actions:   POST /confirm  (sets is_manual=true)
 *            POST /reject   (breaks cluster + records MatchRejection pairs)
 */
export default function MatchesReviewPage() {
  const t = useTranslations("matches_review");
  const locale = useLocale();
  const qc = useQueryClient();
  const [confidenceMax, setConfidenceMax] = useState(0.85);
  const [onlyNeedsReview, setOnlyNeedsReview] = useState(false);

  const q = useQuery({
    queryKey: ["matches-suggestions", confidenceMax, onlyNeedsReview],
    queryFn: () =>
      api.matchSuggestions({
        confidence_max: confidenceMax,
        only_needs_review: onlyNeedsReview,
        limit: 100,
      }),
  });

  const confirm = useMutation({
    mutationFn: (id: number) => api.matchConfirm(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["matches-suggestions"] }),
  });

  const reject = useMutation({
    mutationFn: (id: number) => api.matchReject(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["matches-suggestions"] }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("title")}
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("subtitle")}
        </p>
      </header>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-4 rounded-lg border border-border bg-card p-4">
        <label className="flex min-h-11 flex-wrap items-center gap-2 text-sm">
          <span>{t("filter_confidence")}</span>
          <input
            type="range"
            min={0.5}
            max={1.0}
            step={0.05}
            value={confidenceMax}
            onChange={(e) => setConfidenceMax(parseFloat(e.target.value))}
            className="w-32"
          />
          <span className="font-mono w-12 text-center">
            {confidenceMax.toFixed(2)}
          </span>
        </label>
        <label className="flex min-h-11 items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={onlyNeedsReview}
            onChange={(e) => setOnlyNeedsReview(e.target.checked)}
          />
          <span>{t("filter_only_flagged")}</span>
        </label>
      </div>

      {/* Loading / Error / Empty */}
      {q.isLoading && (
        <p className="text-sm text-muted-foreground">{t("loading")}</p>
      )}
      {q.error && (
        <p className="text-sm text-destructive">{friendlyError(q.error, locale)}</p>
      )}
      {q.data && q.data.length === 0 && !q.isLoading && (
        <div className="rounded-lg border border-border bg-card p-8 text-center">
          <CheckCircle2 className="mx-auto h-8 w-8 text-green-500 mb-2" />
          <p className="font-medium">{t("empty_title")}</p>
          <p className="text-sm text-muted-foreground mt-1">{t("empty_subtitle")}</p>
        </div>
      )}

      {/* Suggestions list */}
      <div className="space-y-3">
        {q.data?.map((m) => (
          <MatchCard
            key={m.match_id}
            match={m}
            onConfirm={() => confirm.mutate(m.match_id)}
            onReject={() => reject.mutate(m.match_id)}
            disabled={confirm.isPending || reject.isPending}
          />
        ))}
      </div>

      {q.data && q.data.length > 0 && (
        <p className="text-xs text-muted-foreground text-center">
          {t("count", { n: q.data.length })}
        </p>
      )}
    </div>
  );
}

function MatchCard({
  match,
  onConfirm,
  onReject,
  disabled,
}: {
  match: MatchSuggestion;
  onConfirm: () => void;
  onReject: () => void;
  disabled: boolean;
}) {
  const t = useTranslations("matches_review");
  const locale = useLocale();
  const confPct = Math.round(match.confidence * 100);
  // Confidence color: red < 60, yellow 60-79, green ≥ 80
  const confClass =
    confPct >= 80
      ? "bg-green-500/10 text-green-700 dark:text-green-400"
      : confPct >= 60
      ? "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400"
      : "bg-red-500/10 text-red-700 dark:text-red-400";

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex flex-col items-start justify-between gap-4 p-4 border-b border-border sm:flex-row">
        <div className="min-w-0">
          <h3 className="font-semibold text-base truncate" title={match.canonical_name}>
            {match.canonical_name}
          </h3>
          <div className="flex flex-wrap items-center gap-2 mt-1.5">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-mono ${confClass}`}
            >
              {t("confidence_label")}: {confPct}%
            </span>
            {match.needs_review && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-500/10 text-amber-700 dark:text-amber-400 text-xs">
                <AlertTriangle className="h-3 w-3" />
                {t("flagged")}
              </span>
            )}
            {match.spread_pct != null && match.spread_pct >= 30 && (
              <span className="inline-flex items-center px-2 py-0.5 rounded bg-orange-500/10 text-orange-700 dark:text-orange-400 text-xs">
                {t("spread")}: {match.spread_pct}%
              </span>
            )}
          </div>
        </div>

        {/* Action buttons */}
        <div className="flex w-full gap-2 shrink-0 sm:w-auto">
          <button
            onClick={onConfirm}
            disabled={disabled}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-1 px-3 py-1.5 rounded-md bg-green-600 text-white text-sm font-medium hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors sm:flex-none md:min-h-9"
            title={t("confirm_hint")}
          >
            <CheckCircle2 className="h-4 w-4" />
            {t("btn_confirm")}
          </button>
          <button
            onClick={onReject}
            disabled={disabled}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-1 px-3 py-1.5 rounded-md bg-red-600 text-white text-sm font-medium hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors sm:flex-none md:min-h-9"
            title={t("reject_hint")}
          >
            <XCircle className="h-4 w-4" />
            {t("btn_reject")}
          </button>
        </div>
      </div>

      {/* Products grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-px bg-border">
        {match.products.map((p) => (
          <a
            key={p.product_id}
            href={p.url}
            target="_blank"
            rel="noopener noreferrer"
            className="bg-card p-3 hover:bg-secondary/50 transition-colors group"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {p.site}
              </span>
              <ExternalLink className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 shrink-0" />
            </div>
            <p className="text-sm mt-1 line-clamp-2" title={p.name}>
              {p.name}
            </p>
            <div className="flex items-center justify-between mt-2">
              <span className="font-mono font-semibold text-base">
                {p.price != null ? formatPrice(p.price, locale) : "—"}
              </span>
              {p.barcode && (
                <span
                  className="font-mono text-[10px] text-muted-foreground"
                  title={t("barcode_title")}
                >
                  {p.barcode}
                </span>
              )}
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}
