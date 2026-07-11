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
