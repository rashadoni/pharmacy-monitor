"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

/**
 * /auth/verify?token=...
 * Calls the FastAPI endpoint which sets the JWT cookie, then redirects to /overview.
 * If token is invalid/expired → shows error.
 */
export default function VerifyPage() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = params.get("token");
    if (!token) {
      setError("Token missing in URL");
      return;
    }
    fetch(`/auth/verify?token=${encodeURIComponent(token)}`, {
      credentials: "include",
    })
      .then((res) => {
        if (res.ok) {
          router.replace("/overview");
        } else {
          setError("Invalid or expired token");
        }
      })
      .catch(() => setError("Network error"));
  }, [params, router]);

  return (
    <div className="min-h-screen flex items-center justify-center">
      {error ? (
        <div className="text-center">
          <div className="text-destructive font-medium mb-2">{error}</div>
          <a href="/login" className="text-sm underline text-primary">
            Запросить новую ссылку
          </a>
        </div>
      ) : (
        <div className="text-muted-foreground text-sm">Verifying…</div>
      )}
    </div>
  );
}
