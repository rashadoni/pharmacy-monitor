import React from "react";
import { AlertTriangle } from "lucide-react";

export function QueryErrorState({
  message,
  retryLabel,
  onRetry,
}: {
  message: string;
  retryLabel: string;
  onRetry: () => void;
}) {
  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between"
      role="alert"
    >
      <div className="flex min-w-0 items-start gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>{message}</span>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="min-h-11 shrink-0 rounded-md border border-destructive/40 px-3 py-1.5 font-medium hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
      >
        {retryLabel}
      </button>
    </div>
  );
}
