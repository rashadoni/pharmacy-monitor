"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { linkTokenFromHash } from "@/lib/link-token";

/**
 * /[locale]/auth/verify#token=...
 *
 * Сюда ведёт ссылка на вход из письма. Токен стоит после «#» — серверу эта
 * часть адреса не уходит; страница читает её и шлёт токен телом POST
 * /auth/verify. Тот ставит JWT cookie, дальше — /<locale>/overview.
 */
export default function VerifyPage() {
  const t = useTranslations("auth");
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  // Токен одноразовый: второй запрос с ним получил бы отказ уже после входа.
  const sent = useRef(false);

  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    const token = linkTokenFromHash(window.location.hash);
    if (!token) {
      setError(t("verify_missing"));
      return;
    }
    fetch("/auth/verify", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ token }),
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
  }, [router, t]);

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
