export function KpiCard({
  label,
  value,
  loading,
  hint,
}: {
  label: string;
  value: number | string;
  loading?: boolean;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular-nums">
        {loading ? "…" : value}
      </div>
      {hint && (
        <div className="text-[11px] text-muted-foreground mt-1">{hint}</div>
      )}
    </div>
  );
}
