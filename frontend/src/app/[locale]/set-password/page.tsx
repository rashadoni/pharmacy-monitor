"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { linkTokenFromHash } from "@/lib/link-token";
import { Lock, KeyRound } from "lucide-react";
import {
  passwordProblem,
  passwordProblemInResponse,
  type PasswordProblem,
} from "@/lib/password-rule";

// Причина, по которой пароль задать нельзя, → ключ текста в `set_password`.
const PROBLEM_TEXT = {
  password_not_text: "error_unusable",
  password_too_short: "error_short",
  password_too_long: "error_long",
} as const satisfies Record<PasswordProblem, string>;

/**
 * /[locale]/set-password#token=...
 *
 * Приглашённый пользователь задаёт свой пароль по одноразовому magic-token из
 * письма. Токен стоит в ссылке после «#» — серверу эта часть адреса не уходит;
 * страница читает её и шлёт токен телом POST /auth/set-password → backend
 * ставит password_hash + JWT cookie → redirect на /overview. Дальше юзер входит
 * по email+паролю на /login.
 *
 * Пароль, который сервер не примет (короткий, длиннее 72 байт), сюда же и
 * возвращается с причиной — до отправки; ссылка при таком отказе остаётся годной.
 */
export default function SetPasswordPage() {
  const router = useRouter();
  const t = useTranslations("set_password");
  // undefined — адрес ещё не прочитан (первый кадр), null — токена в ссылке нет.
  const [token, setToken] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    setToken(linkTokenFromHash(window.location.hash));
  }, []);

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // Ссылка больше не годится (сервер отказал по ней самой). Отказ по паролю и
  // сбой её не гасят — новую тогда предлагать незачем: она погасила бы эту.
  const [linkSpent, setLinkSpent] = useState(false);
  // Ссылка без токена (письмо старого образца, обрезанный адрес) — сказать сразу,
  // а не после того, как человек придумал пароль.
  const shownError = errorMsg ?? (token === null ? t("token_missing") : null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!token) {
      setStatus("error");
      setErrorMsg(t("token_missing"));
      return;
    }
    const problem = passwordProblem(password);
    if (problem) {
      setStatus("error");
      setErrorMsg(t(PROBLEM_TEXT[problem]));
      return;
    }
    if (password !== confirm) {
      setStatus("error");
      setErrorMsg(t("error_mismatch"));
      return;
    }
    setStatus("loading");
    setErrorMsg(null);
    try {
      const res = await fetch("/auth/set-password", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      });
      if (!res.ok) {
        if (res.status === 401) {
          setLinkSpent(true);
          throw new Error(t("error_invalid"));
        }
        const data = await res.json().catch(() => ({}));
        // 400 — пароль у пользователя уже есть; ссылка при этом погашена.
        if (res.status === 400) setLinkSpent(true);
        const refused = passwordProblemInResponse(data);
        if (refused) throw new Error(t(PROBLEM_TEXT[refused]));
        // `detail` бывает и списком — тогда это не текст для человека.
        throw new Error(
          typeof data?.detail === "string" ? data.detail : t("error_generic"),
        );
      }
      // Cookie set — logged in. Redirect to dashboard.
      router.replace("/overview");
      router.refresh();
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : t("error_generic"));
    }
  }

  return (
    <div className="min-h-screen relative flex items-center justify-center px-4 overflow-hidden bg-gradient-to-br from-slate-50 via-white to-slate-100 dark:from-slate-950 dark:via-slate-900 dark:to-slate-950">
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
        <div className="absolute -top-40 -left-40 h-96 w-96 rounded-full bg-primary/10 blur-3xl" />
        <div className="absolute -bottom-40 -right-40 h-96 w-96 rounded-full bg-blue-400/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        <div className="rounded-2xl border border-border bg-card/95 backdrop-blur-sm shadow-xl shadow-black/5 dark:shadow-black/20 p-8 md:p-10">
          <div className="mb-8 text-center">
            <div className="inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 mb-4">
              <KeyRound className="h-7 w-7 text-primary" />
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
            <p className="text-sm text-muted-foreground mt-1.5">{t("subtitle")}</p>
          </div>

          <form onSubmit={onSubmit} className="space-y-4">
            <div>
              <label htmlFor="password" className="text-sm font-medium block mb-1.5">
                {t("password_label")}
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
                <input
                  id="password"
                  type="password"
                  required
                  autoFocus
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  disabled={status === "loading"}
                  className="w-full rounded-lg border border-input bg-background pl-9 pr-3 py-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-transparent transition-shadow"
                />
              </div>
            </div>

            <div>
              <label htmlFor="confirm" className="text-sm font-medium block mb-1.5">
                {t("confirm_label")}
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
                <input
                  id="confirm"
                  type="password"
                  required
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder="••••••••"
                  disabled={status === "loading"}
                  className="w-full rounded-lg border border-input bg-background pl-9 pr-3 py-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-transparent transition-shadow"
                />
              </div>
            </div>

            {shownError && (
              <div
                role="alert"
                className="rounded-lg bg-destructive/10 border border-destructive/30 px-3 py-2 text-sm text-destructive flex items-start gap-2"
              >
                <span className="leading-tight">{shownError}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={status === "loading" || !password || !confirm}
              className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-150 active:scale-[0.99] shadow-sm"
            >
              {status === "loading" ? t("loading") : t("submit")}
            </button>
          </form>

          {(linkSpent || token === null) && (
            <p className="text-center text-xs text-muted-foreground mt-6">
              <a href="/login" className="underline text-primary">
                {t("request_new")}
              </a>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
