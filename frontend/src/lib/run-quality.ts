export type RunTerminalStatus = "ok" | "degraded" | "failed";

export function isTerminalRunStatus(
  status: string,
): status is RunTerminalStatus {
  return status === "ok" || status === "degraded" || status === "failed";
}

export function runStatusToneClass(status: string): string {
  if (status === "ok") return "bg-success/10 text-success";
  if (status === "degraded") return "bg-warning/10 text-warning";
  if (status === "failed") return "bg-destructive/10 text-destructive";
  return "bg-muted text-muted-foreground";
}

export function scrapeResultTextClass(status: string): string {
  if (status === "failed") return "text-destructive";
  if (status === "degraded") return "text-warning";
  return "text-success";
}

interface SiteRunPair<Run extends { id: number; status: string }> {
  /** Последний годный прогон сайта. */
  run: Run | null;
  /** Последняя попытка сайта, удачная или нет. */
  latest_attempt: Run | null;
}

/**
 * Попытка новее последнего годного прогона, которая не удалась, — о ней обзор
 * предупреждает. Полный сбор с изолированными карточками бэкенд сам отдаёт
 * годным прогоном, поэтому здесь его не встретить: «новая попытка завершилась
 * с проблемой» остаётся только за настоящими неудачами.
 */
export function newerProblemAttempt<Run extends { id: number; status: string }>(
  item: SiteRunPair<Run>,
): Run | null {
  const attempt = item.latest_attempt;
  if (!attempt || attempt.id === item.run?.id) return null;
  return attempt.status === "degraded" || attempt.status === "failed" ? attempt : null;
}
