"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useRouter } from "@/i18n/navigation";

/**
 * /[locale]/auth/verify?token=...
 *
 * Calls FastAPI endpoint, sets JWT cookie, redirects to /<locale>/overview.
 *
 * Phase 6.1 retry: Suspense boundary обязательна для useSearchParams при
 * SSG из [locale]/ dynamic segment. Иначе prerender падает.
 */
function VerifyInner() {
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
          // i18n/navigation router → автоматом подставит locale префикс
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

export default function VerifyPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center">
          <div className="text-muted-foreground text-sm">Verifying…</div>
        </div>
      }
    >
      <VerifyInner />
    </Suspense>
  );
}
