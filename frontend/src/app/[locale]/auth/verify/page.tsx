"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";

/**
 * /[locale]/auth/verify?token=...
 *
 * Calls FastAPI endpoint, sets JWT cookie, redirects to /<locale>/overview.
 *
 * Phase 6.1 retry: Suspense boundary обязательна для useSearchParams при
 * SSG из [locale]/ dynamic segment. Иначе prerender падает.
 */
function VerifyInner() {
  const t = useTranslations("auth");
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = params.get("token");
    if (!token) {
      setError(t("verify_missing"));
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
          setError(t("verify_invalid"));
        }
      })
      .catch(() => setError(t("verify_network")));
  }, [params, router, t]);

  return (
    <div className="min-h-screen flex items-center justify-center">
      {error ? (
        <div className="text-center" role="alert" aria-live="polite">
          <div className="text-destructive font-medium mb-2">{error}</div>
          <Link href="/login" className="text-sm underline text-primary">
            {t("verify_request_new")}
          </Link>
        </div>
      ) : (
        <div className="text-muted-foreground text-sm">{t("verify_loading")}</div>
      )}
    </div>
  );
}

export default function VerifyPage() {
  const t = useTranslations("auth");
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center">
          <div className="text-muted-foreground text-sm">{t("verify_loading")}</div>
        </div>
      }
    >
      <VerifyInner />
    </Suspense>
  );
}
