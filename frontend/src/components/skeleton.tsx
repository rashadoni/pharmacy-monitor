/**
 * Skeleton loading placeholders. Заменяют generic «Загрузка…» на серые
 * блоки той же формы что реальный контент — пользователь видит layout
 * сразу, не пустую страницу.
 */

/** Базовый прямоугольник со shimmer-эффектом. */
export function Skeleton({
  className = "",
  width,
  height,
}: {
  className?: string;
  width?: string | number;
  height?: string | number;
}) {
  return (
    <div
      className={`animate-pulse rounded bg-muted/60 ${className}`}
      style={{ width, height }}
    />
  );
}

/** 4 KPI карточки в строку. */
export function KpiSkeletonGrid() {
  return (
    <div className="grid gap-4 grid-cols-2 md:grid-cols-4">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-4 space-y-2">
          <Skeleton height={10} width="60%" />
          <Skeleton height={28} width="40%" />
          <Skeleton height={10} width="80%" />
        </div>
      ))}
    </div>
  );
}

/** Skeleton рядов для таблицы. */
export function TableSkeleton({ rows = 8, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <div className="divide-y divide-border">
        {Array.from({ length: rows }).map((_, ri) => (
          <div
            key={ri}
            className="flex items-center gap-3 px-3 py-3"
          >
            {Array.from({ length: cols }).map((_, ci) => (
              <Skeleton
                key={ci}
                height={14}
                className="flex-1"
                width={ci === 0 ? "30%" : ci === cols - 1 ? "10%" : undefined}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Skeleton для списка карточек (например /alerts). */
export function CardListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-3 space-y-2">
          <Skeleton height={14} width="40%" />
          <Skeleton height={10} width="80%" />
          <Skeleton height={10} width="20%" />
        </div>
      ))}
    </div>
  );
}
