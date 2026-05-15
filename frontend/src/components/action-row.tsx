import type { RoiAction } from "@/lib/api";
import { formatPrice } from "@/lib/utils";

export function ActionRow({ action }: { action: RoiAction }) {
  const tone =
    action.severity === "critical"
      ? "border-destructive/40 bg-destructive/5"
      : action.severity === "warning"
        ? "border-warning/40 bg-warning/5"
        : action.severity === "opportunity"
          ? "border-success/40 bg-success/5"
          : "border-border bg-card";
  const hasGap = action.unit_gap_azn != null && action.spread_pct != null;
  const gapPositive = (action.unit_gap_azn ?? 0) > 0;
  return (
    <div className={`rounded-lg border ${tone} p-3`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm">{action.title}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{action.detail}</div>
        </div>
        {hasGap && (
          <div className="shrink-0 text-right">
            <div
              className={`text-sm font-semibold tabular-nums ${
                gapPositive ? "text-success" : "text-destructive"
              }`}
              title="Разница цены за единицу товара — реально проверяемая величина"
            >
              {gapPositive ? "+" : ""}
              {formatPrice(action.unit_gap_azn ?? 0)} ₼/ед
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {(action.spread_pct ?? 0).toFixed(1)}% спред
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
