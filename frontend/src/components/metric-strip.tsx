export interface MetricStripItem {
  label: string;
  value: number | string;
  loading?: boolean;
  hint?: string;
}

export function MetricStrip({ items }: { items: MetricStripItem[] }) {
  return (
    <dl className="flex flex-col overflow-hidden rounded-lg border border-border bg-card sm:flex-row">
      {items.map((item) => (
        <div
          key={item.label}
          className="min-w-0 flex-1 border-b border-border px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0"
        >
          <dt className="text-sm font-medium text-muted-foreground">{item.label}</dt>
          <dd className="mt-1 text-xl font-semibold tabular-nums">
            {item.loading ? "…" : item.value}
          </dd>
          {item.hint && (
            <dd className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {item.hint}
            </dd>
          )}
        </div>
      ))}
    </dl>
  );
}
