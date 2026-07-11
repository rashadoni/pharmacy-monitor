"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { isVerifiedScanPendingError } from "@/lib/api";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000, // 30s — match Streamlit cache TTL for parity
            refetchOnWindowFocus: false,
            retry: (failureCount, error: any) => {
              // A verified full scan is required; retrying cannot make this request succeed.
              if (isVerifiedScanPendingError(error)) return false;
              // Don't retry 401/403/404
              if (error?.status >= 400 && error?.status < 500) return false;
              return failureCount < 2;
            },
          },
        },
      }),
  );
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
